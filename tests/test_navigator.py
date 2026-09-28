"""Tests for coach/navigator.py: preconditions, run lifecycle and the generic
non-combat fallback, against the in-process ScriptedMod fake."""

from __future__ import annotations

import json
import time

import pytest
from fakes.fake_mod import ScriptedMod

from coach.config import CONF_FILE_NAME, Config
from coach.navigator import (
    NavigatorError,
    act_of,
    advance_one_step,
    check_preconditions,
    embark_run,
    enter_act,
    hp_of,
    leave_run,
)


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


def test_advance_one_step_treasure_claims_and_proceeds():
    mod = ScriptedMod()
    mod.state_type = "treasure"
    advance_one_step(mod, mod.get_state())
    assert {"action": "claim_treasure_relic", "index": 0} in mod.posted
    assert {"action": "proceed"} in mod.posted


def test_advance_one_step_game_over_returns_to_menu():
    mod = ScriptedMod()
    mod.state_type = "game_over"
    advance_one_step(mod, mod.get_state())
    assert {"action": "menu_select", "option": "main_menu"} in mod.posted
