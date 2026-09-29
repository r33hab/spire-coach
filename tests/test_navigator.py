"""Tests for coach/navigator.py: preconditions, run lifecycle and the generic
non-combat fallback, against the in-process ScriptedMod fake."""

from __future__ import annotations

import json
import time

import pytest
from fakes.fake_mod import ScriptedMod

from coach import navigator
from coach.bridge.client import BridgeError
from coach.config import CONF_FILE_NAME, Config
from coach.navigator import (
    NavigatorError,
    act_and_await_change,
    act_of,
    advance_one_step,
    apply_hp_buffer,
    check_preconditions,
    embark_run,
    enter_act,
    hp_of,
    leave_run,
    max_hp_of,
)
from coach.runlog import RunLog


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # enter_act waits out RunManager's own async act generation between steps; nothing
    # here should ever need that to be a real wait against ScriptedMod.
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def make_config(tmp_path, *, allow_debug_actions: bool | None = True) -> Config:
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    if allow_debug_actions is not None:
        (mods_dir / CONF_FILE_NAME).write_text(
            json.dumps({"token": "t", "allow_debug_actions": allow_debug_actions}),
            encoding="utf-8",
        )
    return Config(
        base_url="http://127.0.0.1:15526",
        token="t",
        data_dir=tmp_path / "data",
        poll_interval_s=0.1,
        mods_dir=mods_dir,
    )


# --- preconditions -------------------------------------------------------------------


def test_check_preconditions_all_clear(tmp_path):
    mod = ScriptedMod(current_profile_id=2)
    assert check_preconditions(mod, make_config(tmp_path)) == []


def test_check_preconditions_mod_unreachable_short_circuits(tmp_path):
    mod = ScriptedMod(hello_ok=False)
    problems = check_preconditions(mod, make_config(tmp_path))
    assert len(problems) == 1
    assert "not running with the mod" in problems[0]


def test_check_preconditions_debug_actions_disabled_names_the_conf_file(tmp_path):
    mod = ScriptedMod(current_profile_id=2)
    config = make_config(tmp_path, allow_debug_actions=False)
    problems = check_preconditions(mod, config)
    assert any("allow_debug_actions" in p and str(config.mods_dir) in p for p in problems)


def test_check_preconditions_missing_conf_counts_as_disabled(tmp_path):
    mod = ScriptedMod(current_profile_id=2)
    config = make_config(tmp_path, allow_debug_actions=None)
    problems = check_preconditions(mod, config)
    assert any("allow_debug_actions" in p for p in problems)


def test_check_preconditions_wrong_profile(tmp_path):
    mod = ScriptedMod(current_profile_id=1)
    problems = check_preconditions(mod, make_config(tmp_path))
    assert any("modded profile 2" in p for p in problems)


# --- run lifecycle -------------------------------------------------------------------


def test_embark_run_reaches_the_map_with_the_asked_ascension():
    mod = ScriptedMod()
    state = embark_run(mod, ascension=10)
    assert state["state_type"] == "map"
    assert act_of(state) == 1
    character_pick = next(a for a in mod.posted if a.get("option") == "Ironclad")
    assert character_pick["ascension"] == 10


def test_embark_run_waits_until_the_game_has_left_character_select():
    # Confirmed live (autopilot-20260929-183924.jsonl, 5 failed starts in a row):
    # character_select already carries the new run's `run.act` while state_type is
    # still "menu", so "the run has an act" passed before the game left the menu.
    # embark_run returned that menu state, the autopilot read it as the run already
    # over (0 moves, no floor), and the game was left sitting on Neow's pick.
    mod = ScriptedMod()
    mod.embark_menu_reads = 3
    state = embark_run(mod, ascension=10)
    assert state["state_type"] == "map"


def test_embark_run_can_leave_neow_to_the_caller():
    # The autopilot's strategy picks Neow's relic itself (strategyplay "neow.relic");
    # only the fight lab needs embark_run to click straight through to the map.
    mod = ScriptedMod()
    mod.run_start_state_type = "event"
    mod.event_options = [{"index": 0, "title": "Pomander"}, {"index": 1, "title": "Lost Coffer"}]
    state = embark_run(mod, ascension=10, through_neow=False)
    assert state["state_type"] == "event"
    assert not any(a.get("action") == "choose_event_option" for a in mod.posted)


def test_embark_run_retries_while_character_select_options_are_still_empty(tmp_path):
    # Bug 6/7, confirmed live right after a game restart: character_select is reached
    # (menu_screen alone) before its roster has populated, so a single read raises "no
    # Ironclad option on character select: []" (autopilot-20260929-002138.jsonl).
    # embark_run must keep polling instead of raising on the first empty read.
    mod = ScriptedMod()
    mod.character_select_delay_reads = 3
    state = embark_run(mod, ascension=10)
    assert state["state_type"] == "map"


def test_embark_run_raises_once_character_select_options_stay_empty_past_the_timeout():
    mod = ScriptedMod()
    mod.character_select_delay_reads = 10**9  # never populates within the timeout
    with pytest.raises(NavigatorError, match="Ironclad"):
        embark_run(mod, ascension=10, timeout_s=0.2)


def test_embark_run_raises_when_the_character_is_not_offered():
    mod = ScriptedMod()
    with pytest.raises(NavigatorError, match="Ironclad"):
        embark_run(mod, ascension=0, character="Silent")


def test_enter_act_calls_debug_enter_next_act_the_right_number_of_times():
    mod = ScriptedMod()
    embark_run(mod, ascension=0)
    state = enter_act(mod, 3)
    assert act_of(state) == 3
    assert sum(1 for a in mod.posted if a.get("action") == "debug_enter_next_act") == 2


def test_enter_act_retries_past_a_transient_bridge_error():
    # Task item 6 (fight-lab act jump): a hard mod-side failure on one
    # debug_enter_next_act call (STS2MCP had a NullReferenceException here) must not
    # spend the whole retry budget on that single attempt -- the same "keep calling
    # until it actually reads the wanted act" tolerance enter_act already gives a call
    # that silently did not take.
    mod = ScriptedMod()
    embark_run(mod, ascension=0)
    real_post = ScriptedMod.post_action
    calls = {"n": 0}

    def flaky_post(self, action, **kwargs):
        if action.get("action") == "debug_enter_next_act":
            calls["n"] += 1
            if calls["n"] == 1:
                raise BridgeError("mod sent a non-JSON response (500 from /api/v1/singleplayer)")
        return real_post(self, action, **kwargs)

    mod.post_action = flaky_post.__get__(mod, ScriptedMod)

    state = enter_act(mod, 2)

    assert act_of(state) == 2
    assert calls["n"] == 2  # 1 failed and was retried, 1 succeeded


def test_enter_act_gives_up_after_max_attempts_of_persistent_bridge_errors():
    mod = ScriptedMod()
    embark_run(mod, ascension=0)

    def always_raises(self, action, **kwargs):
        if action.get("action") == "debug_enter_next_act":
            raise BridgeError("mod is busy")
        return ScriptedMod.post_action(self, action, **kwargs)

    mod.post_action = always_raises.__get__(mod, ScriptedMod)

    with pytest.raises(NavigatorError, match="stuck at 1"):
        enter_act(mod, 2, max_attempts=3)


def test_enter_act_no_run_raises():
    mod = ScriptedMod()
    with pytest.raises(NavigatorError):
        enter_act(mod, 2)


def test_leave_run_from_mid_fight_uses_return_to_main_menu():
    mod = ScriptedMod()
    mod.state_type = "monster"
    mod.act = 2
    state = leave_run(mod, mod.get_state())
    assert state["state_type"] == "menu"
    assert {"action": "return_to_main_menu"} in mod.posted


def test_leave_run_from_game_over_selects_main_menu():
    mod = ScriptedMod()
    mod.state_type = "game_over"
    state = leave_run(mod, mod.get_state())
    assert state["state_type"] == "menu"
    assert {"action": "menu_select", "option": "main_menu"} in mod.posted


def test_hp_of_and_act_of_read_the_fields():
    assert hp_of({"player": {"hp": 42}}) == 42
    assert hp_of({}) is None
    assert act_of({"run": {"act": 2}}) == 2
    assert act_of({}) is None


def test_max_hp_of_reads_the_field():
    assert max_hp_of({"player": {"max_hp": 80}}) == 80
    assert max_hp_of({}) is None


# --- HP safety buffer (bug 2/7: --buffer-hp stacked, reaching 110,080) ---------------


def test_apply_hp_buffer_tops_up_to_the_target_when_below_it(tmp_path):
    mod = ScriptedMod()
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    apply_hp_buffer(mod, runlog, 500, 80, source="autopilot")

    assert mod.posted == [{"action": "debug_gain_max_hp", "amount": 420}]
    lines = [json.loads(line) for line in runlog.path.read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["kind"] == "buffer"
    assert lines[-1]["payload"] == {"target_hp": 500, "delta": 420}


def test_apply_hp_buffer_never_grows_past_the_target_once_reached(tmp_path):
    # The bug: --buffer-hp H used to ADD H every fight (reached 110,080 with H=10000
    # over repeated fights). Once max HP already meets the target, nothing is sent.
    mod = ScriptedMod()
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    apply_hp_buffer(mod, runlog, 500, 500, source="autopilot")

    assert mod.posted == []
    # Nothing sent means nothing logged either: RunLog creates its file lazily, on
    # the first append, so no call at all means the file was never written.
    assert not runlog.path.exists()


def test_apply_hp_buffer_does_nothing_once_max_hp_already_exceeds_the_target(tmp_path):
    mod = ScriptedMod()
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    apply_hp_buffer(mod, runlog, 100, 500, source="autopilot")

    assert mod.posted == []


def test_apply_hp_buffer_across_repeated_fights_settles_at_the_target(tmp_path):
    # The real stacking scenario end to end (bug 2/7): buffering is applied at the
    # start of several fights in a row, each time passed the max HP the previous
    # fight actually left behind -- it must reach the target once and then stay
    # there, not keep climbing (old behaviour with H=10000: 80 -> 10080 -> 20080 ->
    # ... -> 110,080 over 11 fights).
    mod = ScriptedMod()
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    apply_hp_buffer(mod, runlog, 500, 80, source="autopilot")  # fight 1: 80 -> 500
    apply_hp_buffer(mod, runlog, 500, 500, source="autopilot")  # fight 2: already 500
    apply_hp_buffer(mod, runlog, 500, 500, source="autopilot")  # fight 3: already 500

    assert mod.posted == [{"action": "debug_gain_max_hp", "amount": 420}]


def test_apply_hp_buffer_treats_a_missing_max_hp_as_zero(tmp_path):
    mod = ScriptedMod()
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    apply_hp_buffer(mod, runlog, 50, None, source="autopilot")

    assert mod.posted == [{"action": "debug_gain_max_hp", "amount": 50}]


def test_apply_hp_buffer_logs_a_clear_error_and_does_not_raise_on_a_bridge_error(tmp_path):
    class RaisingMod(ScriptedMod):
        def post_action(self, action, **kwargs):
            raise BridgeError("mod is busy")

    mod = RaisingMod()
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    apply_hp_buffer(mod, runlog, 500, 80, source="autopilot")  # must not raise

    lines = [json.loads(line) for line in runlog.path.read_text(encoding="utf-8").splitlines()]
    assert any(
        line["kind"] == "error" and "safety buffer failed" in line["payload"]["error"]
        for line in lines
    )


def test_apply_hp_buffer_includes_extra_log_fields(tmp_path):
    mod = ScriptedMod()
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    apply_hp_buffer(
        mod, runlog, 500, 80, source="fightlab", log_extra={"encounter": "KaiserCrabBoss"}
    )

    lines = [json.loads(line) for line in runlog.path.read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["payload"] == {"target_hp": 500, "delta": 420, "encounter": "KaiserCrabBoss"}


# --- act_and_await_change (bug 3&4/7: the stale-read race that jams the game) -------


class _SequencedClient:
    """`get_state()` returns each of `reads` in turn, repeating the last one once
    exhausted; `post_action` just records what was posted."""

    def __init__(self, reads: list[dict]) -> None:
        self._reads = reads
        self._i = 0
        self.posted: list[dict] = []

    def get_state(self) -> dict:
        state = self._reads[min(self._i, len(self._reads) - 1)]
        self._i += 1
        return state

    def post_action(self, action: dict, **kwargs) -> dict:
        self.posted.append(action)
        return {"status": "ok"}


def test_act_and_await_change_waits_past_a_stale_read_after_the_click():
    # Confirmed live (autopilot-20260929-000347.jsonl, the newest autopilot-*.jsonl):
    # the read right after choose_map_node can still be the pre-click snapshot --
    # is_resolving false does not by itself prove the click was seen, since it does
    # not cover a room transition at all. Acting on that stale read is what sent a
    # second navigation action into a transition already under way and jammed the
    # game. Must keep waiting past it rather than trusting the first read.
    baseline = {"state_type": "map", "action_log_seq": 5, "is_resolving": False}
    stale = {"state_type": "map", "action_log_seq": 5, "is_resolving": False}  # == baseline
    settled = {"state_type": "monster", "action_log_seq": 6, "is_resolving": False}
    client = _SequencedClient([stale, settled])

    result = act_and_await_change(client, {"action": "choose_map_node", "index": 0}, baseline)

    assert result == settled
    assert client.posted == [{"action": "choose_map_node", "index": 0}]  # never double-clicked


def test_act_and_await_change_waits_out_is_resolving_even_after_the_seq_changes():
    baseline = {"state_type": "map", "action_log_seq": 5, "is_resolving": False}
    mid_transition = {"state_type": "map", "action_log_seq": 6, "is_resolving": True}
    settled = {"state_type": "monster", "action_log_seq": 6, "is_resolving": False}
    client = _SequencedClient([mid_transition, settled])

    result = act_and_await_change(client, {"action": "choose_map_node", "index": 0}, baseline)

    assert result == settled


def test_act_and_await_change_gives_up_after_timeout_and_returns_the_last_read():
    stale = {"state_type": "map", "action_log_seq": 5, "is_resolving": False}
    client = _SequencedClient([stale])

    result = act_and_await_change(
        client,
        {"action": "choose_map_node", "index": 0},
        {"state_type": "map", "action_log_seq": 5},
        timeout_s=0.05,
        poll_s=0.01,
    )

    assert result == stale


def test_act_and_await_change_propagates_a_bridge_error_from_the_post_itself():
    class RaisingClient:
        def post_action(self, action, **kwargs):
            raise BridgeError("mod is busy")

    with pytest.raises(BridgeError):
        act_and_await_change(RaisingClient(), {"action": "choose_map_node"}, {})


# --- generic fallback ----------------------------------------------------------------


def test_advance_one_step_map_prefers_monster_or_elite():
    mod = ScriptedMod()
    mod.state_type = "map"
    mod.next_map_options = [
        {"index": 0, "type": "Shop"},
        {"index": 1, "type": "Elite"},
    ]
    advance_one_step(mod, mod.get_state())
    assert {"action": "choose_map_node", "index": 1} in mod.posted


def test_advance_one_step_map_waits_for_the_click_before_returning():
    mod = ScriptedMod()
    mod.state_type = "map"
    mod.next_map_options = [{"index": 0, "type": "Monster"}]
    mod.after_map_choice = "monster"

    result = advance_one_step(mod, mod.get_state())

    assert mod.posted == [{"action": "choose_map_node", "index": 0}]
    assert result["state_type"] == "monster"


def test_advance_one_step_rewards_claims_a_takeable_item():
    mod = ScriptedMod()
    mod.state_type = "rewards"
    mod.rewards_items = [{"index": 0, "type": "card"}, {"index": 1, "type": "gold"}]
    advance_one_step(mod, mod.get_state())
    assert {"action": "claim_reward", "index": 1} in mod.posted


def test_advance_one_step_rewards_with_nothing_takeable_proceeds():
    mod = ScriptedMod()
    mod.state_type = "rewards"
    mod.rewards_items = []
    advance_one_step(mod, mod.get_state())
    assert {"action": "proceed"} in mod.posted


def test_advance_one_step_card_reward_skips():
    mod = ScriptedMod()
    mod.state_type = "card_reward"
    advance_one_step(mod, mod.get_state())
    assert {"action": "skip_card_reward"} in mod.posted


def test_advance_one_step_rest_site_skips_a_disabled_option():
    # Real capture shape (data/runs/20260926-220853.jsonl): options carry their own
    # is_enabled (HEAL is disabled at full HP; SMITH stays enabled). Picking index 0
    # unconditionally sends choose_rest_option at a disabled option, which the mod
    # no-ops (McpMod.Actions.cs ExecuteChooseRestOption: "option ... is disabled"), so
    # the rest site was left unused every time HP was already full.
    mod = ScriptedMod()
    mod.state_type = "rest_site"
    mod.rest_site_options = [
        {"index": 0, "id": "HEAL", "name": "Rest", "is_enabled": False, "has_button": True},
        {"index": 1, "id": "SMITH", "name": "Smith", "is_enabled": True, "has_button": True},
    ]
    advance_one_step(mod, mod.get_state())
    assert {"action": "choose_rest_option", "index": 1} in mod.posted
    assert {"action": "proceed"} in mod.posted


def test_advance_one_step_rest_site_prefers_the_first_enabled_option():
    mod = ScriptedMod()
    mod.state_type = "rest_site"
    mod.rest_site_options = [
        {"index": 0, "id": "HEAL", "name": "Rest", "is_enabled": True, "has_button": True},
        {"index": 1, "id": "SMITH", "name": "Smith", "is_enabled": True, "has_button": True},
    ]
    advance_one_step(mod, mod.get_state())
    assert {"action": "choose_rest_option", "index": 0} in mod.posted


def test_advance_one_step_rest_site_with_nothing_enabled_still_proceeds():
    mod = ScriptedMod()
    mod.state_type = "rest_site"
    mod.rest_site_options = [
        {"index": 0, "id": "HEAL", "name": "Rest", "is_enabled": False, "has_button": True},
    ]
    advance_one_step(mod, mod.get_state())
    assert not any(a.get("action") == "choose_rest_option" for a in mod.posted)
    assert {"action": "proceed"} in mod.posted


def test_advance_one_step_card_select_picks_the_first_card_and_confirms():
    # Real capture (data/runs/20260928-190511.jsonl): a rest-site "Choose a card to
    # Upgrade" grid, but the same state_type/schema also serves a shop's card removal,
    # an event's card transform and Neow's transform-a-card bonus (task: "upgrade,
    # remove and transform choices from rest sites, shops, events and Neow"). Before
    # this fix, card_select fell into the same branch as card_reward and sent
    # skip_card_reward -- an action the grid screen has no handler for (STS2MCP's
    # skip_card_reward needs the post-combat reward screen, not the grid) -- which left
    # it up forever. select_card + confirm_selection is the grid's own pair
    # (McpMod.Actions.cs ExecuteSelectCard/ExecuteConfirmSelection).
    mod = ScriptedMod()
    mod.state_type = "card_select"
    mod.card_select_cards = [
        {"index": 0, "id": "STRIKE_IRONCLAD", "uid": "STRIKE_IRONCLAD#5"},
        {"index": 1, "id": "STRIKE_IRONCLAD", "uid": "STRIKE_IRONCLAD#6"},
    ]
    advance_one_step(mod, mod.get_state())
    assert {"action": "select_card", "index": 0} in mod.posted
    assert {"action": "confirm_selection"} in mod.posted
    assert not any(a.get("action") == "skip_card_reward" for a in mod.posted)


def _two_pick_grid(selected: set[int]) -> dict:
    cards = [{"index": i, "id": f"CARD_{i}", "selected": i in selected} for i in range(4)]
    return {"state_type": "card_select", "card_select": {"cards": cards, "min_select": 2,
            "max_select": 2, "required_count": 2, "selected_count": len(selected)}}


def test_a_two_pick_grid_picks_a_second_unselected_card_then_confirms():
    # Live (the user, 2026-09-29): "Choose 2 Common Cards to Add to Your Deck" with card
    # 0 already selected; re-clicking card 0 unselected it and looped forever.
    client = _SequencedClient([])
    navigator._select_first_card(client, _two_pick_grid({0}))
    assert client.posted == [{"action": "select_card", "index": 1}, {"action": "confirm_selection"}]


def test_a_two_pick_grid_does_not_confirm_after_the_first_pick():
    client = _SequencedClient([])
    navigator._select_first_card(client, _two_pick_grid(set()))
    assert client.posted == [{"action": "select_card", "index": 0}]


def test_a_full_two_pick_grid_only_confirms():
    client = _SequencedClient([])
    navigator._select_first_card(client, _two_pick_grid({0, 2}))
    assert client.posted == [{"action": "confirm_selection"}]


def test_advance_one_step_card_select_with_no_cards_does_not_crash():
    mod = ScriptedMod()
    mod.state_type = "card_select"
    mod.card_select_cards = []
    advance_one_step(mod, mod.get_state())  # must not raise
    assert not any(a.get("action") == "select_card" for a in mod.posted)


def test_advance_one_step_treasure_claims_and_proceeds():
    mod = ScriptedMod()
    mod.state_type = "treasure"
    advance_one_step(mod, mod.get_state())
    assert {"action": "claim_treasure_relic", "index": 0} in mod.posted
    assert {"action": "proceed"} in mod.posted


def test_advance_one_step_shop_leaves_without_buying():
    # Task item 4 ("check the autopilot's handling of shop buys"): confirmed no
    # shop.buy_* kind is mapped (coach.strategyplay), so every shop visit falls to
    # this generic branch -- it must at least leave cleanly. ExecuteProceed's merchant
    # branch (McpMod.Actions.cs) closes the inventory first if open, then leaves.
    mod = ScriptedMod()
    mod.state_type = "shop"
    advance_one_step(mod, mod.get_state())
    assert mod.posted == [{"action": "proceed"}]


def test_advance_one_step_relic_select_picks_the_first_relic():
    # Boss relics and any other "choose a relic" moment share this state_type
    # (STS2MCP McpMod.StateBuilder.cs: NChooseARelicSelection -> "relic_select").
    mod = ScriptedMod()
    mod.state_type = "relic_select"
    advance_one_step(mod, mod.get_state())
    assert {"action": "select_relic", "index": 0} in mod.posted


def test_advance_one_step_event_picks_the_first_option():
    mod = ScriptedMod()
    mod.state_type = "event"
    mod.event_options = [{"index": 0, "title": "Leave"}, {"index": 1, "title": "Fight"}]
    advance_one_step(mod, mod.get_state())
    assert {"action": "choose_event_option", "index": 0} in mod.posted


def test_advance_one_step_event_with_no_options_yet_does_not_crash():
    # Real capture (data/runs/20260926-220853.jsonl): an event screen can settle with
    # body null / options [] for one read while it is still opening (2 of 21 real event
    # reads in that session). Nothing to act on yet; the next poll re-reads fresh, so
    # this must stay a no-op rather than guess at an action the mod has no handler for
    # on a bare event room (ExecuteProceed has no generic event-room case).
    mod = ScriptedMod()
    mod.state_type = "event"
    mod.event_options = []
    advance_one_step(mod, mod.get_state())  # must not raise
    assert mod.posted == []


def test_advance_one_step_game_over_returns_to_menu():
    mod = ScriptedMod()
    mod.state_type = "game_over"
    advance_one_step(mod, mod.get_state())
    assert {"action": "menu_select", "option": "main_menu"} in mod.posted


# --- generic fallback: bundle_select, Crystal Sphere, Fake Merchant, unknown --------


def test_advance_one_step_bundle_select_picks_the_first_bundle_and_confirms():
    mod = ScriptedMod()
    mod.state_type = "bundle_select"
    mod.extra_state = {"bundle_select": {"bundles": [{"index": 0}, {"index": 1}]}}
    advance_one_step(mod, mod.get_state())
    assert {"action": "select_bundle", "index": 0} in mod.posted
    assert {"action": "confirm_bundle_selection"} in mod.posted


def test_advance_one_step_crystal_sphere_sets_a_tool_before_the_first_click():
    # Confirmed stuck live (Act 2, floor 30): state_type crystal_sphere, no event
    # block, options empty -- nothing before this handled it at all.
    mod = ScriptedMod()
    mod.state_type = "crystal_sphere"
    mod.extra_state = {
        "crystal_sphere": {
            "tool": "none",
            "can_use_small_tool": True,
            "can_use_big_tool": True,
            "clickable_cells": [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
        }
    }
    advance_one_step(mod, mod.get_state())
    assert {"action": "crystal_sphere_set_tool", "tool": "small"} in mod.posted
    assert {"action": "crystal_sphere_click_cell", "x": 1, "y": 2} in mod.posted


def test_advance_one_step_crystal_sphere_proceeds_once_no_cells_are_clickable():
    mod = ScriptedMod()
    mod.state_type = "crystal_sphere"
    mod.extra_state = {
        "crystal_sphere": {"tool": "small", "can_use_small_tool": True, "clickable_cells": []}
    }
    advance_one_step(mod, mod.get_state())
    assert {"action": "crystal_sphere_proceed"} in mod.posted
    assert not any(a.get("action") == "crystal_sphere_click_cell" for a in mod.posted)


def test_advance_one_step_fake_merchant_proceeds():
    mod = ScriptedMod()
    mod.state_type = "fake_merchant"
    advance_one_step(mod, mod.get_state())
    assert {"action": "proceed"} in mod.posted


def test_advance_one_step_logs_an_unrecognized_state_type_when_given_a_runlog(tmp_path):
    mod = ScriptedMod()
    mod.state_type = "some_new_screen_this_module_has_never_seen"
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    advance_one_step(mod, mod.get_state(), runlog=runlog)

    lines = [json.loads(line) for line in runlog.path.read_text(encoding="utf-8").splitlines()]
    assert any(
        line["kind"] == "error" and "some_new_screen_this_module_has_never_seen" in line["payload"]["error"]
        for line in lines
    )


def test_advance_one_step_does_not_log_a_recognized_state_type(tmp_path):
    mod = ScriptedMod()
    mod.state_type = "shop"
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    advance_one_step(mod, mod.get_state(), runlog=runlog)

    assert not runlog.path.exists()


def test_advance_one_step_without_a_runlog_still_works(tmp_path):
    # runlog is optional (existing callers, and every other test in this file, never
    # pass one) -- must not raise for an unrecognized state_type either.
    mod = ScriptedMod()
    mod.state_type = "some_new_screen_this_module_has_never_seen"
    advance_one_step(mod, mod.get_state())  # must not raise
    assert {"action": "proceed"} in mod.posted


def test_rewards_skip_a_potion_when_the_belt_is_full_and_proceed():
    state = {"state_type": "rewards", "player": {"potions": ["FOUL_POTION", "FOUL_POTION"], "max_potion_slots": 2},
             "rewards": {"items": [{"index": 0, "type": "potion"}], "can_proceed": True}}
    client = _SequencedClient([state])
    navigator.advance_one_step(client, state)
    assert {"action": "proceed"} in client.posted
    assert not any(a.get("action") == "claim_reward" for a in client.posted)


def test_an_unknown_screen_is_waited_out_not_proceeded(monkeypatch):
    # Live (2026-09-29): after End turn in an elite fight the game reported "unknown"
    # for a moment; the fallback's proceed opened the map mid-fight and jammed the game.
    clock = [1000.0]
    monkeypatch.setattr(navigator.time, "monotonic", lambda: clock[0])
    navigator._UNKNOWN_SINCE.clear()
    state = {"state_type": "unknown"}
    client = _SequencedClient([state])
    navigator.advance_one_step(client, state)
    assert client.posted == []
    clock[0] += navigator.UNKNOWN_PROCEED_AFTER_S + 1
    navigator.advance_one_step(client, state)
    assert client.posted == [{"action": "proceed"}]
    navigator._UNKNOWN_SINCE.clear()


def test_an_event_skips_a_locked_option():
    # Live (2026-09-29): the Endless Conveyor's "Broke" (out of Gold) is locked; picking
    # option 0 again and again stalled the autopilot. Take the open "Leave".
    state = {"state_type": "event", "event": {"event_id": "ENDLESS_CONVEYOR", "options": [
        {"index": 0, "title": "Broke", "is_locked": True},
        {"index": 1, "title": "Leave", "is_locked": False}]}}
    client = _SequencedClient([state])
    navigator.advance_one_step(client, state)
    assert {"action": "choose_event_option", "index": 1} in client.posted
    assert {"action": "choose_event_option", "index": 0} not in client.posted
