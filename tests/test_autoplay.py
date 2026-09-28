"""Tests for coach/autoplay.py: MovePlayer plays a fight with a fake mod and a fake
engine, and must record `state`/`analysis`/`action`/`check` entries in the coach's
normal session format -- the whole point of the fight lab (spec: "so the simulator's
fidelity checker gets thousands of real transitions").
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coach.autoplay import MovePlayer, in_combat, move_to_action, wait_settled
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

    _ADVANCING = frozenset({"play_card", "use_potion", "end_turn", "combat_select_card"})

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


def test_play_fight_select_then_confirm_posts_both_actions(tmp_path):
    bridge = ScriptedBridge([combat(), rewards()])
    engine = ScriptedEngine([select_move(1)])
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    player = MovePlayer(bridge, engine, runlog, game_version="v0.107.1")

    outcome = player.play_fight(bridge.get_state())

    assert outcome.ok is True
    assert bridge.posted == [
        {"action": "combat_select_card", "card_index": 1},
        {"action": "combat_confirm_selection"},
    ]


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


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # play_fight/wait_settled never need a real sleep against a fake bridge that is
    # always "ready" on its first read; this just keeps a mistaken retry loop fast.
    import time

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
