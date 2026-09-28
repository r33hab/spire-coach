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
from coach.autoplay import FightOutcome, GameJamError
from coach.bridge.client import BridgeError
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


def test_run_with_exactly_one_map_option_chooses_it_without_asking_the_strategy(tmp_path):
    # Bug 4/7: a map with a single next_options entry never needs the strategy
    # service at all -- one less round trip that can fail (NotReadyError, seen live
    # on an Act 2 map) for a decision that was never really a choice.
    mod = make_mod()
    mod.next_map_options = [{"index": 0, "type": "Unknown"}]
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(mod, [(death(), 1)])
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    autopilot.run(mod, engine, runlog, runs=1, move_player=player)

    assert {"action": "choose_map_node", "index": 0} in mod.posted
    assert engine.calls == []  # never asked


def test_run_falls_back_when_the_engine_has_no_model(tmp_path):
    mod = make_mod()
    mod.after_map_choice = "monster"
    # Two options (bug 4/7 made a single-option map skip the strategy call entirely,
    # which would make this test pass for the wrong reason): the fallback's own "first
    # Monster/Elite" rule must still pick index 0 here once the engine has no model.
    mod.next_map_options = [{"index": 0, "type": "Monster"}, {"index": 1, "type": "Shop"}]
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


def test_run_started_mid_run_plays_from_the_current_screen_without_embarking(tmp_path):
    # Bug 5/7: started mid-run (e.g. at Neow, or -- as here -- the map after a fight),
    # it used to call embark_run unconditionally regardless of the mod's actual
    # state_type, which only records the session header and then waits for a main
    # menu that a live run is nowhere near. Must play from whatever screen is
    # current instead: no menu_select at all, straight to the map's own fallback.
    mod = make_mod()
    mod.state_type = "map"  # already mid-run when autopilot starts, not "menu"
    mod.next_map_options = [{"index": 0, "type": "Monster"}]
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(mod, [(death(), 1)])
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    summary = autopilot.run(mod, engine, runlog, runs=1, move_player=player)

    # No embark_run at all: none of its own menu clicks (leave_run's own
    # menu_select("main_menu") once the run legitimately ends is fine and expected).
    embark_options = {"singleplayer", "standard", "Ironclad", "confirm", "embark"}
    assert not any(
        a.get("action") == "menu_select" and a.get("option") in embark_options
        for a in mod.posted
    )
    assert {"action": "choose_map_node", "index": 0} in mod.posted
    assert summary.runs_played == 1


def test_run_only_the_first_of_several_runs_can_resume_mid_run(tmp_path):
    # A second run always follows a real ending (game_over or the main menu), so it
    # must embark normally even though the first one skipped it.
    mod = make_mod()
    mod.state_type = "map"
    mod.next_map_options = [{"index": 0, "type": "Monster"}]
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(mod, [(death(), 1), (death(), 1)])
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    summary = autopilot.run(mod, engine, runlog, runs=2, move_player=player)

    assert summary.runs_played == 2
    assert any(a.get("option") == "Ironclad" for a in mod.posted)  # the second run embarked


def test_buffer_hp_applies_debug_gain_max_hp_before_each_fight_and_logs_it(tmp_path):
    # ScriptedMod's default max_hp is 80 (fake_mod.py), so a target of 500 tops up by
    # 420 -- debug_gain_max_hp's own "amount" is a delta (bug 2/7), not the target.
    mod = make_mod()
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(mod, [(death(), 1)])
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    autopilot.run(mod, engine, runlog, runs=1, buffer_hp=500, move_player=player)

    assert {"action": "debug_gain_max_hp", "amount": 420} in mod.posted
    buffer_records = [
        json.loads(line)
        for line in runlog.path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] == "buffer"
    ]
    assert buffer_records and buffer_records[0]["payload"] == {"target_hp": 500, "delta": 420}


def test_buffer_hp_does_not_stack_across_repeated_fights(tmp_path):
    # The bug: --buffer-hp H added H at the start of EVERY fight (reached 110,080
    # with H=10000 over repeated fights). Three fights in one run, with a fake mod
    # that applies debug_gain_max_hp's delta the way the real one does (current AND
    # max HP both raised): the second and third fights must send nothing, since
    # max_hp already meets the target the first fight reached.
    class BufferTrackingMod(ScriptedMod):
        def post_action(self, action, **kwargs):
            result = super().post_action(action, **kwargs)
            if action.get("action") == "debug_gain_max_hp":
                self.max_hp += action["amount"]
                self.hp += action["amount"]
            return result

    mod = BufferTrackingMod(current_profile_id=2)
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(
        mod, [(win_state(hp=500), 1), (win_state(hp=500), 1), (death(), 1)]
    )
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    autopilot.run(mod, engine, runlog, runs=1, buffer_hp=500, move_player=player)

    buffer_actions = [a for a in mod.posted if a.get("action") == "debug_gain_max_hp"]
    assert buffer_actions == [{"action": "debug_gain_max_hp", "amount": 420}]
    assert mod.max_hp == 500


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


class JammingThenNormalPlayer:
    """Raises GameJamError on its first `jam_count` calls to play_fight, then behaves
    like ScriptedFightPlayer (delegated to) for the rest."""

    def __init__(self, mod, script, *, jam_count=1):
        self._delegate = ScriptedFightPlayer(mod, script)
        self._jam_count = jam_count
        self._calls = 0
        self.stats = self._delegate.stats

    def play_fight(self, state, **kwargs):
        self._calls += 1
        if self._calls <= self._jam_count:
            raise GameJamError("jam: is_resolving stuck true with no state change")
        return self._delegate.play_fight(state, **kwargs)


def test_run_stops_on_a_game_jam_by_default(tmp_path):
    # Spec: by default, stop the autopilot with a clear message -- not silently loop
    # retrying the same move forever, and not restart the game unasked.
    mod = make_mod()
    mod.after_map_choice = "monster"
    player = JammingThenNormalPlayer(mod, [(death(), 1)], jam_count=1)
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")
    said = []

    summary = autopilot.run(mod, engine, runlog, runs=3, move_player=player, say=said.append)

    assert summary.runs_played == 0
    assert any("jam" in line.lower() for line in said)
    assert any("stopping" in line.lower() for line in said)
    error_lines = [
        json.loads(line)
        for line in runlog.path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] == "error"
    ]
    assert any("jam" in line["payload"]["error"].lower() for line in error_lines)


def test_run_restarts_on_jam_when_opted_in(tmp_path):
    mod = make_mod()
    mod.after_map_choice = "monster"
    player = JammingThenNormalPlayer(mod, [(death(), 1)], jam_count=1)
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")
    calls = {"quit": 0, "launch": 0}

    summary = autopilot.run(
        mod,
        engine,
        runlog,
        runs=2,  # the jam consumes one attempt (as any other recovered failure does)
        move_player=player,
        restart_on_jam=True,
        quit_game=lambda: calls.__setitem__("quit", calls["quit"] + 1),
        launch_game=lambda: calls.__setitem__("launch", calls["launch"] + 1),
    )

    assert calls == {"quit": 1, "launch": 1}
    assert summary.runs_played == 1  # continued with the next run and completed it


def test_run_restart_on_jam_gives_up_if_the_mod_never_answers_again(tmp_path):
    mod = make_mod()
    mod.hello_ok = False  # the "relaunch" never actually comes back up
    mod.after_map_choice = "monster"
    player = JammingThenNormalPlayer(mod, [(death(), 1)], jam_count=1)
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")
    said = []

    summary = autopilot.run(
        mod,
        engine,
        runlog,
        runs=2,
        move_player=player,
        say=said.append,
        restart_on_jam=True,
        quit_game=lambda: None,
        launch_game=lambda: None,
        restart_hello_timeout_s=0.05,
    )

    assert summary.runs_played == 0
    assert any("could not recover" in line.lower() for line in said)


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
