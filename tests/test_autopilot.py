"""Tests for coach/autopilot.py: whole-run orchestration against the ScriptedMod fake,
a stub move player and a stub engine (only its `strategy` method matters here --
MovePlayer's own analyze/check loop is covered in test_autoplay.py)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fakes.fake_mod import ScriptedMod

from coach import autopilot
from coach.autoplay import FightOutcome
from coach.engine import EngineError, StrategyResult
from coach.runlog import RunLog


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


class StubEngine:
    """Only `strategy` is used by coach.autopilot (MovePlayer is stubbed out)."""

    def __init__(self, answers=None) -> None:
        self._answers = list(answers or [])
        self.calls: list[tuple] = []

    def strategy(self, live_run, screen):
        self.calls.append((live_run, screen))
        if not self._answers:
            return None
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class ScriptedFightPlayer:
    """Plays exactly like coach.autoplay.MovePlayer from coach.autopilot's point of
    view (a `stats.moves` counter and a `play_fight(state) -> FightOutcome`), and also
    applies its scripted outcome onto the fake mod, the way a real fight leaves the mod
    in a new state once it is over."""

    def __init__(self, mod: ScriptedMod, script: list[tuple[FightOutcome, int]]) -> None:
        self._mod = mod
        self._script = list(script)
        self.stats = SimpleNamespace(moves=0)
        self.fights_seen = 0

    def play_fight(self, state: dict, **kwargs) -> FightOutcome:
        outcome, moves = self._script[min(self.fights_seen, len(self._script) - 1)]
        self.fights_seen += 1
        self.stats.moves += moves
        self._mod.state_type = outcome.state["state_type"]
        self._mod.hp = outcome.last_hp if outcome.last_hp is not None else self._mod.hp
        return outcome


def win_state(state_type="map", hp=50) -> FightOutcome:
    return FightOutcome(state={"state_type": state_type, "player": {"hp": hp}}, ok=True, last_hp=hp)


def death() -> FightOutcome:
    return FightOutcome(state={"state_type": "game_over", "player": {"hp": 0}}, ok=True, last_hp=0)


def make_mod() -> ScriptedMod:
    mod = ScriptedMod(current_profile_id=2)
    mod.encounter_tiers = {}  # autopilot never calls debug_start_encounter
    return mod


def test_run_plays_map_to_combat_to_death_with_the_generic_fallback(tmp_path):
    mod = make_mod()
    mod.after_map_choice = "monster"  # the fallback's chosen node leads into a fight
    player = ScriptedFightPlayer(mod, [(death(), 4)])
    engine = StubEngine()  # no strategy answers: every screen uses the generic fallback
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    summary = autopilot.run(mod, engine, runlog, runs=1, ascension=10, move_player=player)

    assert summary.runs_played == 1
    assert summary.deaths == 1
    assert summary.wins == 0
    assert summary.moves_recorded == 4
    assert summary.records[0].won is False


def test_run_counts_a_win_when_hp_remains_at_game_over(tmp_path):
    mod = make_mod()
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(
        mod, [(FightOutcome(state={"state_type": "game_over"}, ok=True, last_hp=42), 2)]
    )
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    summary = autopilot.run(mod, engine, runlog, runs=1, move_player=player)

    assert (summary.wins, summary.deaths) == (1, 0)


def test_run_prefers_the_strategy_champions_mapped_option_over_the_fallback(tmp_path):
    mod = make_mod()
    mod.next_map_options = [{"index": 0, "col": 1}, {"index": 1, "col": 7}]
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(mod, [(death(), 1)])
    champion_option = {
        "option": {"kind": "map.node", "node": [7, 0, 1]},
        "label": "x",
        "value": 1.0,
        "p_boss": None,
    }
    champion_pick = StrategyResult(options=[champion_option], model={"generation": 1})
    engine = StubEngine(answers=[champion_pick])
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    autopilot.run(mod, engine, runlog, runs=1, move_player=player)

    # The fallback would have taken the FIRST Monster/Elite option (there is none typed
    # here, so it would fall through to next_map_options[0], col=1); the champion asked
    # for col=7 (index 1) instead, so seeing that index proves its pick won.
    assert {"action": "choose_map_node", "index": 1} in mod.posted


def test_run_falls_back_when_the_engine_has_no_model(tmp_path):
    mod = make_mod()
    mod.after_map_choice = "monster"
    mod.next_map_options = [{"index": 0, "type": "Monster"}]
    player = ScriptedFightPlayer(mod, [(death(), 1)])
    engine = StubEngine(answers=[EngineError("no_model", error_type="no_model")])
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    autopilot.run(mod, engine, runlog, runs=1, move_player=player)

    assert {"action": "choose_map_node", "index": 0} in mod.posted
    strategy_errors = [
        json.loads(line)
        for line in runlog.path.read_text(encoding="utf-8").splitlines()
        if '"strategy"' in line
    ]
    assert strategy_errors and "error" in strategy_errors[0]["payload"]


def test_buffer_hp_applies_debug_gain_max_hp_before_each_fight_and_logs_it(tmp_path):
    mod = make_mod()
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(mod, [(death(), 1)])
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    autopilot.run(mod, engine, runlog, runs=1, buffer_hp=25, move_player=player)

    assert {"action": "debug_gain_max_hp", "amount": 25} in mod.posted
    buffer_records = [
        json.loads(line)
        for line in runlog.path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] == "buffer"
    ]
    assert buffer_records and buffer_records[0]["payload"]["amount"] == 25


def test_no_buffer_by_default(tmp_path):
    mod = make_mod()
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(mod, [(death(), 1)])
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    autopilot.run(mod, engine, runlog, runs=1, move_player=player)

    assert not any(a.get("action") == "debug_gain_max_hp" for a in mod.posted)


def test_session_header_names_the_autopilot_command_and_settings(tmp_path):
    mod = make_mod()
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(mod, [(death(), 1)])
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    autopilot.run(mod, engine, runlog, runs=1, ascension=15, buffer_hp=10, move_player=player)

    first = json.loads(runlog.path.read_text(encoding="utf-8").splitlines()[0])
    assert first["kind"] == "session"
    assert first["payload"]["command"] == "autopilot"
    assert first["payload"]["ascension"] == 15
    assert first["payload"]["buffer_hp"] == 10


def test_stuck_run_is_recovered_not_crashed(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "_MAX_SCREENS_PER_RUN", 3)
    mod = make_mod()
    mod.after_map_choice = "map"  # never reaches combat or a menu: stays stuck on "map"
    player = ScriptedFightPlayer(mod, [])
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    summary = autopilot.run(mod, engine, runlog, runs=1, move_player=player)

    assert summary.runs_played == 0  # the stuck run was recovered, not counted
    assert mod.state_type == "menu"  # _recover left it at the main menu
