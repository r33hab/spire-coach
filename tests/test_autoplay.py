"""Tests for coach/autoplay.py: MovePlayer plays a fight with a fake mod and a fake
engine, and must record `state`/`analysis`/`action`/`check` entries in the coach's
normal session format -- the whole point of the fight lab (spec: "so the simulator's
fidelity checker gets thousands of real transitions").
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from coach.autoplay import GameJamError, MovePlayer, in_combat, move_to_action, wait_settled
from coach.engine import Analysis, CheckResult, EngineError, Move
from coach.runlog import RunLog

FIXTURES = Path(__file__).parent / "fixtures" / "mod"

CLEAN_CHECK = CheckResult(
    comparable=True, deterministic_mismatches=[], random_fields=[], implausible=[], warnings=[]
)


def combat(seq=12, *, hp=61, round_=2):
    state = json.loads((FIXTURES / "combat_full.json").read_text(encoding="utf-8"))
    state["action_log_seq"] = seq
    state["player"]["hp"] = hp
    state["battle"]["round"] = round_
    return state


def combat_round(round_: int, hp: int, *, seq: int = 12) -> dict:
    """`combat_full.json` with `battle.round` and the one alive enemy's (Jaw Worm) hp
    overridden, for the no-progress detector's round-by-round tracking. The fixture's
    other two enemies are already dead (hp 0, alive False), so this is the fight's
    whole total-enemy-hp reading."""
    state = json.loads((FIXTURES / "combat_full.json").read_text(encoding="utf-8"))
    state["action_log_seq"] = seq
    state["battle"]["round"] = round_
    state["battle"]["enemies"][0]["hp"] = hp
    return state


def rewards():
    return {
        "state_type": "rewards",
        "rewards": {"items": [], "can_proceed": True},
        "player": {"hp": 61},
    }


def game_over(hp=0):
    return {
        "state_type": "game_over",
        "game_over": {"message": "Run ended.", "options": ["main_menu"]},
        "player": {"hp": hp},
    }


END_TURN = Move(
    kind="end_turn",
    card_index=None,
    card_uid=None,
    card_id=None,
    potion_slot=None,
    potion_id=None,
    target=None,
    select_index=None,
    label="End turn",
)


def card_move(index=0, target="JAW_WORM_0"):
    return Move(
        kind="card",
        card_index=index,
        card_uid="c1",
        card_id="STRIKE_IRONCLAD",
        potion_slot=None,
        potion_id=None,
        target=target,
        select_index=None,
        label="Strike -> Jaw Worm",
    )


def enemy_choose():
    """Real capture (fightlab-20260928-213922.jsonl): Knowledge Demon's forced curse
    pick mid-fight. `pending_selection.source` is "unknown" (an enemy power, not a
    card/potion/relic), which the engine has no model for."""
    return json.loads(
        (FIXTURES / "state_card_select_choose_enemy_source.json").read_text(encoding="utf-8")
    )


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def hand_select_burning_pact(*, selected: list[dict] | None = None) -> dict:
    """Real capture: Burning Pact's "select a card in hand to exhaust" (HAND kind,
    min 1, max 1). The fixture as recorded already has one card selected (ready to
    confirm); `selected=[]` gives the moment before anything is picked instead."""
    state = _fixture("combat_hand_select_with_selection.json")
    if selected is not None:
        state["hand_select"]["selection"]["selected"] = selected
    return state


def hand_select_any_number_skippable() -> dict:
    """A skippable, uncapped HAND pick (real shape seen live: min 0, max 999999999,
    can_skip true, relic-sourced) with nothing chosen yet."""
    state = hand_select_burning_pact(selected=[])
    selection = state["hand_select"]["selection"]
    selection["min"] = 0
    selection["max"] = 999999999
    selection["can_skip"] = True
    return state


def card_select_grid_headbutt(*, selected: list[dict] | None = None, max_: int | None = None) -> dict:
    """Real capture: Headbutt's "select a card from discard" (GRID kind, min 1,
    max 1 as recorded). `max_` overrides max (an "up to N" grid); `selected`
    overrides how many are already picked."""
    state = _fixture("combat_card_select_grid.json")
    selection = state["card_select"]["selection"]
    if selected is not None:
        selection["selected"] = selected
    if max_ is not None:
        selection["max"] = max_
    return state


def card_select_choose_potion() -> dict:
    """Real capture: a Colorless/Attack Potion's "choose a card" (CHOOSE kind, min 0,
    max 1, can_skip true) -- the exact shape that broke the first fight tonight
    (autopilot-20260929-002615.jsonl: a Colorless Potion's CHOOSE screen looped 42
    times because combat_select_card/combat_confirm_selection never touch it)."""
    return _fixture("combat_card_select_choose.json")


def select_move(index=0):
    return Move(
        kind="select",
        card_index=None,
        card_uid=None,
        card_id=None,
        potion_slot=None,
        potion_id=None,
        target=None,
        select_index=index,
        label="Card selection",
    )


class ScriptedBridge:
    """`get_state()` returns `states[min(index, len-1)]`; `post_action` advances
    `index` by one for every action that moves the fight forward a position
    (`_ADVANCING`), so a select+confirm pair -- two posts, one transition -- only
    advances once."""

    _ADVANCING = frozenset(
        {
            "play_card",
            "use_potion",
            "end_turn",
            "combat_select_card",
            "combat_confirm_selection",
            "select_card",
            "confirm_selection",
            "cancel_selection",
        }
    )

    def __init__(self, states: list[dict]) -> None:
        self._states = states
        self._index = 0
        self.posted: list[dict] = []
        self.raise_on: dict[str, Exception] = {}

    def get_state(self) -> dict:
        return copy.deepcopy(self._states[min(self._index, len(self._states) - 1)])

    def post_action(self, action: dict, **kwargs) -> dict:
        self.posted.append(action)
        name = action.get("action")
        if name in self.raise_on:
            raise self.raise_on[name]
        if name in self._ADVANCING:
            self._index += 1
        return {"status": "ok"}


class ScriptedEngine:
    def __init__(self, moves: list[Move], *, check_result=None) -> None:
        self._moves = moves
        self._i = 0
        self.analyze_calls: list[dict] = []
        self.check_calls: list[tuple] = []
        self._check_result = check_result
        self.raise_analyze: Exception | None = None

    def analyze(self, live: dict, **kwargs) -> Analysis:
        self.analyze_calls.append(live)
        if self.raise_analyze is not None:
            raise self.raise_analyze
        move = self._moves[min(self._i, len(self._moves) - 1)]
        self._i += 1
        return Analysis(
            reliable=True,
            warnings=[],
            best=move,
            line=[move],
            line_truncated_at_draw=False,
            moves=[],
            sims=10,
            elapsed_s=0.01,
            budget_exhausted=False,
            select_mapping_failures=0,
        )

    def check(self, pre: dict, move: Move, post: dict) -> CheckResult:
        self.check_calls.append((pre, move, post))
        result = self._check_result() if callable(self._check_result) else self._check_result
        if isinstance(result, Exception):
            raise result
        return result or CLEAN_CHECK


def records(runlog: RunLog) -> list[dict]:
    return [json.loads(line) for line in runlog.path.read_text(encoding="utf-8").splitlines()]


def kinds(runlog: RunLog) -> list[str]:
    return [record["kind"] for record in records(runlog)]


# --- move_to_action ----------------------------------------------------------------


def test_move_to_action_end_turn():
    assert move_to_action(END_TURN) == {"action": "end_turn"}


def test_move_to_action_card_with_target():
    action = move_to_action(card_move())
    assert action == {"action": "play_card", "card_index": 0, "target": "JAW_WORM_0"}


def test_move_to_action_select():
    assert move_to_action(select_move(2)) == {"action": "combat_select_card", "card_index": 2}


def test_move_to_action_unknown_kind_returns_none():
    weird = Move(
        kind="mystery",
        card_index=None,
        card_uid=None,
        card_id=None,
        potion_slot=None,
        potion_id=None,
        target=None,
        select_index=None,
        label="?",
    )
    assert move_to_action(weird) is None


def test_in_combat_covers_combat_selection_screens():
    assert in_combat({"state_type": "hand_select", "in_combat": True})
    assert not in_combat({"state_type": "hand_select", "in_combat": False})
    assert in_combat({"state_type": "monster"})
    assert not in_combat({"state_type": "rewards"})


# --- MovePlayer ----------------------------------------------------------------------


def test_play_fight_one_move_win_records_state_action_and_skipped_check(tmp_path):
    bridge = ScriptedBridge([combat(), rewards()])
    engine = ScriptedEngine([END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert outcome.state["state_type"] == "rewards"
    assert player.stats.moves == 1
    assert bridge.posted == [{"action": "end_turn"}]
    kind_list = kinds(runlog)
    assert kind_list.count("state") == 1
    assert kind_list.count("analysis") == 1
    assert kind_list.count("action") == 1
    assert kind_list.count("check") == 1
    check_record = next(r for r in records(runlog) if r["kind"] == "check")
    assert check_record["payload"]["skipped"] == "the fight ended on this move"
    assert player.stats.checks == 0


def test_play_fight_two_moves_death_checks_the_first_move_only(tmp_path):
    second_state = combat(seq=13, hp=55, round_=2)
    bridge = ScriptedBridge([combat(), second_state, game_over(hp=0)])
    engine = ScriptedEngine([card_move(), END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert outcome.state["state_type"] == "game_over"
    assert outcome.last_hp == 0
    assert player.stats.moves == 2
    assert player.stats.checks == 1  # the first move's transition was comparable
    assert engine.check_calls[0][1] == card_move()
    check_kinds = [r for r in records(runlog) if r["kind"] == "check"]
    assert len(check_kinds) == 2
    assert "result" in check_kinds[0]["payload"]
    assert check_kinds[1]["payload"]["skipped"] == "the fight ended on this move"


def test_play_fight_records_last_hp_even_on_a_death_that_leaves_no_pending_check(tmp_path):
    bridge = ScriptedBridge([combat(hp=1), game_over(hp=0)])
    engine = ScriptedEngine([END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.last_hp == 0  # the settled game_over state's own hp, not the pre-move 1


def test_play_fight_stops_and_logs_when_the_engine_fails(tmp_path):
    bridge = ScriptedBridge([combat()])
    engine = ScriptedEngine([END_TURN])
    engine.raise_analyze = EngineError("boom")
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is False
    assert bridge.posted == []  # never got as far as acting
    analysis_records = [r for r in records(runlog) if r["kind"] == "analysis"]
    assert analysis_records and "error" in analysis_records[-1]["payload"]


def test_play_fight_stops_and_logs_when_the_move_has_no_mod_action(tmp_path):
    bridge = ScriptedBridge([combat()])
    weird = Move(
        kind="mystery",
        card_index=None,
        card_uid=None,
        card_id=None,
        potion_slot=None,
        potion_id=None,
        target=None,
        select_index=None,
        label="?",
    )
    engine = ScriptedEngine([weird])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is False
    assert bridge.posted == []
    action_records = [r for r in records(runlog) if r["kind"] == "action"]
    assert action_records and "error" in action_records[-1]["payload"]


def test_play_fight_forces_a_choice_on_an_enemy_sourced_combat_select(tmp_path):
    # Knowledge Demon's forced curse pick (real capture): pending_selection.source is
    # "unknown", so the engine's own analyze() has nothing to search and its "best"
    # degrades to a no-op ("end_turn", confirmed live) that the mod ignores on this
    # screen -- previously this replayed the same card_select 300 times until the
    # move cap gave up on the fight. MovePlayer must force the first offered candidate
    # instead, the same "first option" doctrine as the non-combat generic fallback
    # (coach.navigator.advance_one_step), so the screen actually resolves. This
    # screen's own kind is CHOOSE (an enemy-opened "pick a curse" overlay, min 1 max 1
    # can_skip false), so the forced pick is select_card, not combat_select_card --
    # and CHOOSE needs no confirm at all (bug 1&7/7: a HAND-only action pair can never
    # reach this screen, the same reason the Colorless Potion CHOOSE screen looped).
    bridge = ScriptedBridge([combat(), enemy_choose(), rewards()])
    engine = ScriptedEngine([END_TURN, END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert outcome.state["state_type"] == "rewards"
    assert bridge.posted == [
        {"action": "end_turn"},
        {"action": "select_card", "index": 0},
    ]


# --- in-fight card selection: HAND vs GRID vs CHOOSE (bug 1&7/7) --------------------


def test_play_fight_grid_selection_uses_select_card_not_combat_select_card(tmp_path):
    # Headbutt (real capture): a GRID pick from the discard pile. combat_select_card
    # only ever looks at NPlayerHand and has no path to this overlay at all. Starts
    # from a normal combat decision (like every real fight): build_live_snapshot needs
    # an established room_type before a card_select/hand_select screen, which only a
    # prior monster/elite/boss decision provides (MovePlayer._room_type_for).
    bridge = ScriptedBridge([combat(), card_select_grid_headbutt(), rewards()])
    engine = ScriptedEngine([END_TURN, select_move(0)])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert bridge.posted == [{"action": "end_turn"}, {"action": "select_card", "index": 0}]


def test_play_fight_choose_selection_sends_select_card_and_never_confirms(tmp_path):
    # The exact bug from tonight's first fight (autopilot-20260929-002615.jsonl): a
    # Colorless Potion's CHOOSE screen, min 0 max 1 can_skip true. STS2MCP's
    # ExecuteConfirmSelection explicitly errors "Choose-a-card screen requires no
    # confirmation" for this screen -- confirming it is not just unnecessary, it is
    # the very thing that left the screen (and combat_select_card's failed press)
    # looping 42 times before this fix.
    bridge = ScriptedBridge([combat(), card_select_choose_potion(), rewards()])
    engine = ScriptedEngine([END_TURN, select_move(0)])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert bridge.posted == [{"action": "end_turn"}, {"action": "select_card", "index": 0}]


def test_play_fight_hand_selection_below_max_selects_without_confirming(tmp_path):
    # Burning Pact (real capture, HAND kind, min 1 max 1), nothing picked yet: must
    # select, and must not confirm in the same breath -- confirming is its own later
    # decision, once the screen actually shows the pick landed.
    bridge = ScriptedBridge([combat(), hand_select_burning_pact(selected=[]), rewards()])
    engine = ScriptedEngine([END_TURN, select_move(1)])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert bridge.posted == [
        {"action": "end_turn"},
        {"action": "combat_select_card", "card_index": 1},
    ]


def test_play_fight_hand_selection_at_max_confirms_instead_of_selecting_again(tmp_path):
    # Burning Pact's fixture as recorded: one card already selected, max 1 reached.
    # Must confirm now, never send another combat_select_card -- even if the engine's
    # own best move still looks like a pick (an eager/stale analyze), reaching max
    # always wins (spec: "never try to pick above max").
    bridge = ScriptedBridge([combat(), hand_select_burning_pact(), rewards()])
    engine = ScriptedEngine([END_TURN, select_move(0)])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert bridge.posted == [{"action": "end_turn"}, {"action": "combat_confirm_selection"}]


def test_play_fight_grid_up_to_n_confirms_once_min_is_met_and_engine_stops_wanting_more(
    tmp_path,
):
    # An "up to 2" grid (constructed from Headbutt's real fixture) with 1 of 2 already
    # picked (min 1 already met) and the engine's best move now something else (it has
    # nothing more worth taking): confirm the partial pick rather than forcing another.
    already = [{"uid": "IRON_WAVE#0", "id": "IRON_WAVE"}]
    bridge = ScriptedBridge(
        [combat(), card_select_grid_headbutt(selected=already, max_=2), rewards()]
    )
    engine = ScriptedEngine([END_TURN, END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert bridge.posted == [{"action": "end_turn"}, {"action": "confirm_selection"}]


def test_play_fight_grid_up_to_n_at_max_confirms_even_if_the_engine_still_wants_to_pick(
    tmp_path,
):
    # Both of 2 already picked: must confirm regardless of what the engine's best move
    # is, including a "select" move -- never pick past max.
    already = [
        {"uid": "IRON_WAVE#0", "id": "IRON_WAVE"},
        {"uid": "SHRUG_IT_OFF#0", "id": "SHRUG_IT_OFF"},
    ]
    bridge = ScriptedBridge(
        [combat(), card_select_grid_headbutt(selected=already, max_=2), rewards()]
    )
    # engine still "wants" to pick on the second decision -- must be ignored
    engine = ScriptedEngine([END_TURN, select_move(0)])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert bridge.posted == [{"action": "end_turn"}, {"action": "confirm_selection"}]


def test_play_fight_choose_skips_when_nothing_picked_and_engine_wants_out(tmp_path):
    # The Colorless Potion CHOOSE screen (min 0, can_skip true) with nothing picked and
    # the engine's best move not a pick at all (nothing offered is worth taking):
    # decline through cancel_selection (the Skip button), not a forced pick.
    bridge = ScriptedBridge([combat(), card_select_choose_potion(), rewards()])
    engine = ScriptedEngine([END_TURN, END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert bridge.posted == [{"action": "end_turn"}, {"action": "cancel_selection"}]


def test_play_fight_hand_any_number_skip_confirms_with_nothing_selected(tmp_path):
    # A skippable, uncapped HAND pick (min 0, max 999999999, can_skip true) with
    # nothing chosen and nothing worth choosing: HAND has no cancel_selection path
    # (STS2MCP's ExecuteCancelSelection never looks at NPlayerHand at all), so
    # declining is confirming with zero selected.
    bridge = ScriptedBridge([combat(), hand_select_any_number_skippable(), rewards()])
    engine = ScriptedEngine([END_TURN, END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert bridge.posted == [{"action": "end_turn"}, {"action": "combat_confirm_selection"}]


def test_play_fight_gives_up_on_no_progress_past_the_round_cap(tmp_path):
    # Item 7 ("no hopeless repeats"): a wall like The Insatiable never lets total enemy
    # hp reach a new low. With no_progress_rounds=3, a new low must land by round 4 or
    # this gives up instead of burning the full move cap; here hp sits at 50 the whole
    # way (rounds 1-5), which must trigger during round 5 (stalled_rounds: 0,1,2,3,4).
    states = [combat_round(r, 50, seq=r) for r in range(1, 6)]
    bridge = ScriptedBridge(states)
    engine = ScriptedEngine([END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1", no_progress_rounds=3)

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is False
    error_records = [r for r in records(runlog) if r["kind"] == "error"]
    assert any("no progress" in r["payload"]["error"] for r in error_records)
    # gave up mid-round 5, well short of exhausting engine.analyze_calls on every move
    assert engine.analyze_calls  # some analysis happened before giving up
    assert bridge.posted == [{"action": "end_turn"}] * 4  # 4 turns played, 5th aborted


def test_play_fight_does_not_give_up_while_enemy_hp_keeps_dropping(tmp_path):
    states = [combat_round(r, 50 - 10 * r, seq=r) for r in range(1, 5)]  # 40,30,20,10
    bridge = ScriptedBridge([*states, rewards()])
    engine = ScriptedEngine([END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1", no_progress_rounds=3)

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert not any(
        "no progress" in r["payload"].get("error", "") for r in records(runlog) if r["kind"] == "error"
    )


def test_play_fight_tolerates_a_plateau_shorter_than_the_cap_like_the_decimillipede(tmp_path):
    # Real shape (fightlab-20260928-213922.jsonl): total enemy hp can sit flat or even
    # rise for a few rounds (segments reattaching) before dropping to a new low again --
    # a slow win, not a stall. Plateau length 2 here must survive a cap of 3.
    states = [
        combat_round(1, 100),
        combat_round(2, 90),  # new low (0)
        combat_round(3, 95),  # stalled 1
        combat_round(4, 92),  # stalled 2
        combat_round(5, 80),  # new low again (resets to 0) -- must not have tripped yet
    ]
    bridge = ScriptedBridge([*states, rewards()])
    engine = ScriptedEngine([END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1", no_progress_rounds=3)

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert not any(
        "no progress" in r["payload"].get("error", "") for r in records(runlog) if r["kind"] == "error"
    )


def test_play_fight_gives_up_after_max_moves(tmp_path):
    # Every posted action leaves the fixture's own fingerprint unchanged (the fake
    # bridge only advances on request), so the fight never settles anywhere new.
    bridge = ScriptedBridge([combat()])
    engine = ScriptedEngine([END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state(), max_moves=3)

    assert outcome.ok is False
    assert player.stats.moves == 3
    error_records = [r for r in records(runlog) if r["kind"] == "error"]
    assert any("3 moves" in r["payload"]["error"] for r in error_records)


def test_wait_settled_returns_last_state_on_timeout():
    class NeverSettles:
        def get_state(self):
            return {"state_type": "monster", "is_resolving": True, "battle": {}}

    state = wait_settled(NeverSettles(), timeout_s=0.05, poll_s=0.01)
    assert state["state_type"] == "monster"


# --- game jam detection (bug 3/7) ---------------------------------------------------


def _stuck_state():
    state = combat_round(1, 50)
    state["is_resolving"] = True  # confirmed live: stuck true, no state change at all
    return state


def test_play_fight_raises_game_jam_error_once_stuck_past_the_timeout(tmp_path, monkeypatch):
    # Confirmed live twice tonight (Ovicopter, and again later the same session):
    # is_resolving stayed true, the play phase reported true, cards marked can_play,
    # and the game ignored all input, even a manual click -- only a full restart
    # cleared it. The autopilot must detect this instead of retrying the same move
    # forever (which is what it did: the real logs show two "safety buffer failed"
    # errors about a minute apart, one per outer retry, never actually recovering).
    bridge = ScriptedBridge([_stuck_state()])
    engine = ScriptedEngine([END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(
        bridge, engine, runlog, game_version="v0.107.1", jam_timeout_s=60.0, settle_timeout_s=1.0
    )
    clock = {"t": 1000.0}

    def fake_monotonic():
        clock["t"] += 10.0
        return clock["t"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    outcome = player.play_fight(bridge.get_state())
    assert outcome.ok is False  # first attempt: stuck, but not yet timed out

    with pytest.raises(GameJamError, match="60"):
        for _ in range(20):  # the outer autopilot loop retrying play_fight
            player.play_fight(bridge.get_state())

    error_records = [r for r in records(runlog) if r["kind"] == "error"]
    assert any("jam" in r["payload"]["error"] for r in error_records)


def test_play_fight_does_not_raise_a_jam_while_the_position_keeps_changing(tmp_path, monkeypatch):
    # A slow but genuinely progressing transition (a long enemy-turn animation, say)
    # must not be mistaken for a jam: a changing fingerprint resets the clock.
    states = [combat_round(r, 50, seq=r) for r in range(1, 6)]
    for state in states:
        state["is_resolving"] = True
    bridge = ScriptedBridge(states)
    engine = ScriptedEngine([END_TURN])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(
        bridge, engine, runlog, game_version="v0.107.1", jam_timeout_s=60.0, settle_timeout_s=1.0
    )
    clock = {"t": 1000.0}

    def fake_monotonic():
        clock["t"] += 10.0
        return clock["t"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    for i in range(len(states)):
        outcome = player.play_fight(bridge.get_state())
        assert outcome.ok is False
        bridge._index = min(i + 1, len(states) - 1)  # advance to the next distinct state

    # No jam ever logged; RunLog creates its file lazily, on the first append, so no
    # error at all means the file was never written.
    assert not runlog.path.exists()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # play_fight/wait_settled never need a real sleep against a fake bridge that is
    # always "ready" on its first read; this just keeps a mistaken retry loop fast.
    import time

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def test_a_card_the_game_marks_unplayable_is_never_sent():
    # Live (2026-09-29, the Queen): Bound left Strike can_play false; the search, which
    # doesn't model Bound, kept picking it and the game kept refusing.
    from types import SimpleNamespace as NS

    from coach.autoplay import playable_choice

    strike = NS(kind="card", card_index=0, visits=50)
    defend = NS(kind="card", card_index=3, visits=30)
    end = NS(kind="end_turn", card_index=None, visits=10)
    state = {"player": {"hand": [{"index": 0, "can_play": False}, {"index": 3, "can_play": True}]}}
    analysis = NS(best=strike, moves=[strike, defend, end])
    assert playable_choice(state, analysis) is defend
    state["player"]["hand"][1]["can_play"] = False
    assert playable_choice(state, analysis) is end
