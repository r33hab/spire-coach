"""Tests for coach/autopilot.py: whole-run orchestration against the ScriptedMod fake,
a stub move player and a stub engine (only its `strategy` method matters here --
MovePlayer's own analyze/check loop is covered in test_autoplay.py)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fakes.fake_mod import ScriptedMod

from coach import autopilot, autoplay
from coach.autoplay import FightOutcome, GameJamError
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


def stuck(hp=30, floor=7) -> FightOutcome:
    """A fight MovePlayer gave up on (no progress, auto-kill off or refused): still
    mid-combat, not game_over -- _play_run must abandon it rather than loop forever."""
    return FightOutcome(
        state={"state_type": "monster", "player": {"hp": hp}, "run": {"act": 1, "floor": floor}},
        ok=False,
        gave_up=True,
        last_hp=hp,
    )


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


def test_run_abandons_a_fight_that_gave_up_and_still_records_the_run(tmp_path):
    # Task 2 ("stop the HP top-ups and abandon"): a fight MovePlayer gave up on (a wall
    # like The Insatiable, auto-kill off or refused) must not spin -- _play_run abandons
    # the run from wherever it got stuck, and it still counts as a real, recorded run
    # (a death), not a discarded one.
    mod = make_mod()
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(mod, [(stuck(hp=30, floor=7), 5)])
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    summary = autopilot.run(mod, engine, runlog, runs=1, move_player=player)

    assert summary.runs_played == 1
    assert summary.deaths == 1
    assert summary.records[0].won is False
    assert summary.records[0].floor == 7
    assert summary.records[0].moves == 5
    # abandoned via leave_run's own return_to_main_menu, not left spinning on "monster"
    assert {"action": "return_to_main_menu"} in mod.posted
    assert mod.state_type == "menu"


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
    # Task 5: the buffer, auto-kill and debug settings are all recorded, defaults included.
    assert first["payload"]["auto_kill"] is True
    assert first["payload"]["auto_kill_no_progress_rounds"] == (
        autoplay.DEFAULT_AUTO_KILL_NO_PROGRESS_ROUNDS
    )
    assert first["payload"]["allow_debug_actions"] is False


def test_session_header_records_auto_kill_and_debug_settings_when_overridden(tmp_path):
    mod = make_mod()
    mod.after_map_choice = "monster"
    player = ScriptedFightPlayer(mod, [(death(), 1)])
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    autopilot.run(
        mod,
        engine,
        runlog,
        runs=1,
        move_player=player,
        auto_kill=False,
        auto_kill_no_progress_rounds=3,
        allow_debug_actions=True,
    )

    first = json.loads(runlog.path.read_text(encoding="utf-8").splitlines()[0])
    assert first["payload"]["auto_kill"] is False
    assert first["payload"]["auto_kill_no_progress_rounds"] == 3
    assert first["payload"]["allow_debug_actions"] is True


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
        runs=2,  # a game jam still consumes one of the runs asked for, unlike a failed
        # start (task 4): only _start_run's own retries are free
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

    # _start_run retries this (a NavigatorError, task 4) up to its own default cap of 5
    # consecutive failed starts -- all against the same always-stuck mod -- before
    # giving up on the whole autopilot; runs_played stays 0 either way.
    summary = autopilot.run(mod, engine, runlog, runs=1, move_player=player)

    assert summary.runs_played == 0  # the stuck run was recovered, not counted
    assert mod.state_type == "menu"  # _recover left it at the main menu


# --- run counting (task 4): a failed start must not count, and must not spend the runs
# budget, but 5 consecutive failures in a row still stops the whole autopilot ----------


def test_run_retries_a_phantom_start_and_does_not_count_it(tmp_path, monkeypatch):
    # Live 2026-09-29 (autopilot-20260929-*.jsonl): a start that silently did not really
    # embark returns cleanly but with nothing to show for it -- "died on floor None, 0
    # moves". That must not count as a played run, and the start must be retried for
    # free (not spending any of the single run asked for here).
    calls: list[dict | None] = []

    def fake_play_run(client, player, engine, runlog, *, ascension, buffer_hp, stop, resume_state):
        calls.append(resume_state)
        if len(calls) == 1:
            return autopilot.RunRecord(won=False, floor=None, moves=0, acts_reached=1, buffered=False)
        return autopilot.RunRecord(won=False, floor=5, moves=3, acts_reached=1, buffered=False)

    monkeypatch.setattr(autopilot, "_play_run", fake_play_run)
    mod = make_mod()
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")

    summary = autopilot.run(mod, engine, runlog, runs=1)

    assert summary.runs_played == 1
    assert summary.records[0].floor == 5
    assert len(calls) == 2  # the phantom start was retried once, for free
    error_records = [
        json.loads(line)
        for line in runlog.path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] == "error"
    ]
    assert any("start failed" in r["payload"]["error"] for r in error_records)


def test_run_stops_after_five_consecutive_failed_starts(tmp_path, monkeypatch):
    calls = []

    def fake_play_run(client, player, engine, runlog, *, ascension, buffer_hp, stop, resume_state):
        calls.append(1)
        return autopilot.RunRecord(won=False, floor=None, moves=0, acts_reached=1, buffered=False)

    monkeypatch.setattr(autopilot, "_play_run", fake_play_run)
    mod = make_mod()
    engine = StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="autopilot-")
    said = []

    summary = autopilot.run(mod, engine, runlog, runs=3, say=said.append)

    assert summary.runs_played == 0
    assert len(calls) == 5  # the spec's default: "about 5 consecutive failed starts"
    assert any("5" in line and "failed start" in line.lower() for line in said)


def test_fresh_run_leaves_a_saved_run_before_starting(tmp_path, monkeypatch):
    # Live (2026-09-29): restarts resumed the same saved run, straight back into the
    # same stuck Lagavulin Matriarch fight.
    from coach import navigator

    left = []
    monkeypatch.setattr(
        navigator, "leave_run", lambda client, state, **kw: left.append(state) or {"state_type": "menu"}
    )
    played = []
    record = autopilot.RunRecord(won=False, floor=1, moves=1, acts_reached=1, buffered=False)
    monkeypatch.setattr(
        autopilot,
        "_start_run",
        lambda *a, **kw: (played.append(kw.get("resume_state")), (record, False))[1],
    )

    class Client:
        def get_state(self):
            return {"state_type": "monster", "run": {"act": 1, "floor": 17}}

    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")
    autopilot.run(Client(), object(), runlog, runs=1, move_player=object(), fresh_run=True)
    assert left and played == [None]  # left the saved run, then embarked fresh


# --- an event page that keeps coming back (Abyssal Baths, live 2026-09-29) --------------


def _baths_page(description: str) -> dict:
    return {
        "state_type": "event",
        "event": {
            "event_id": "ABYSSAL_BATHS",
            "description": description,
            "options": [
                {"index": 0, "title": "Linger", "is_locked": False,
                 "text_key": "ABYSSAL_BATHS.pages.ALL.options.LINGER"},
                {"index": 1, "title": "Exit Baths", "is_locked": False,
                 "text_key": "ABYSSAL_BATHS.pages.ALL.options.EXIT_BATHS"},
            ],
        },
    }


def test_event_loop_guard_takes_the_way_out_of_a_page_that_keeps_coming_back():
    # Live: Linger / Exit Baths came back after every Linger, damage rising by one, and
    # Linger kept being picked (an 8,000 hp buffer makes it look like free max hp) until
    # it killed the run. The third identical page in a row is a loop: take the way out.
    from coach.autopilot import EventLoopGuard

    guard = EventLoopGuard(limit=3)
    assert guard.way_out(_baths_page("LINGER1")) is None
    assert guard.way_out(_baths_page("LINGER2")) is None
    assert guard.way_out(_baths_page("DEATH_WARNING"))["title"] == "Exit Baths"
    assert guard.way_out({"state_type": "map"}) is None  # any other screen starts over
    assert guard.way_out(_baths_page("LINGER1")) is None


def test_play_screen_leaves_a_looping_event_without_asking_the_strategy(tmp_path):
    from coach.autopilot import EventLoopGuard, _play_screen
    from coach.runlog import RunLog

    class Poster:
        def __init__(self) -> None:
            self.posted: list[dict] = []

        def post_action(self, action, **kwargs):
            self.posted.append(action)
            return {"status": "ok"}

    guard = EventLoopGuard(limit=3)
    guard.way_out(_baths_page("LINGER1"))
    guard.way_out(_baths_page("LINGER2"))
    client, engine = Poster(), StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    _play_screen(client, engine, None, _baths_page("LINGER3"), runlog, guard)

    assert client.posted == [{"action": "choose_event_option", "index": 1}]
    assert engine.calls == []


# --- card rewards are opened (live 2026-10-05: 0 of 1,090 taken since 09-29) -----------


def _rewards_screen(floor: int = 5) -> dict:
    return {
        "state_type": "rewards",
        "run": {"act": 1, "floor": floor},
        "rewards": {
            "items": [
                {"index": 0, "type": "gold"},
                {"index": 1, "type": "card"},
            ],
            "can_proceed": True,
        },
    }


def test_play_screen_claims_gold_and_opens_a_card_reward_once_before_anything_else(tmp_path):
    # The card reward is an item on the rewards screen that opens the three-card choice.
    # Neither the strategy's reward options (potion, relic, proceed) nor the fallback's
    # takeable types (gold, potion, relic) ever opened it, so every autopilot run since
    # 09-29 skipped every card. Opened once per floor: after a skip it is not reopened.
    from coach.autopilot import RewardClaimer, _play_screen
    from coach.runlog import RunLog

    class Poster:
        def __init__(self) -> None:
            self.posted: list[dict] = []

        def post_action(self, action, **kwargs):
            self.posted.append(action)
            return {"status": "ok"}

    rewards = RewardClaimer()
    client, engine = Poster(), StubEngine()
    runlog = RunLog(tmp_path, subdir="runs", prefix="test-")

    _play_screen(client, engine, None, _rewards_screen(), runlog, None, rewards)
    _play_screen(client, engine, None, _rewards_screen(), runlog, None, rewards)
    assert client.posted == [
        {"action": "claim_reward", "index": 0},  # the gold
        {"action": "claim_reward", "index": 1},  # the card reward
    ]
    assert engine.calls == []
    assert rewards.to_claim(_rewards_screen()) is None  # both tried on this floor
    assert rewards.to_claim(_rewards_screen(floor=6))["index"] == 0  # a new floor's gold


def test_a_card_the_thief_stole_is_claimed_back():
    # Killing the Thieving Hopper puts the stolen card on the rewards screen as a
    # "special_card" item; left unclaimed, the card is gone for good.
    from coach.autopilot import RewardClaimer

    screen = {
        "state_type": "rewards",
        "run": {"act": 1, "floor": 7},
        "rewards": {"items": [{"index": 0, "type": "special_card"}, {"index": 1, "type": "potion"}]},
    }
    rewards = RewardClaimer()
    assert rewards.to_claim(screen) == {"index": 0, "type": "special_card"}

    # Once the mod says which card it is, taking it back is the model's call
    # (`reward.card`): a weak card left behind is a free removal.
    screen["rewards"]["items"][0]["card"] = {"id": "STRIKE_IRONCLAD", "index": 0}
    assert RewardClaimer().to_claim(screen) == {"index": 1, "type": "potion"}


def test_a_potion_is_taken_whenever_a_slot_is_free():
    # Live 2026-10-05: a Flex Potion left behind with two empty slots. Holding one costs
    # nothing (it can be thrown away for a better one); a full belt is the strategy's.
    from coach.autopilot import RewardClaimer

    def screen(held: int) -> dict:
        return {
            "state_type": "rewards",
            "run": {"act": 1, "floor": 4},
            "player": {"max_potion_slots": 2, "potions": [{"id": "FIRE_POTION"}] * held},
            "rewards": {"items": [{"index": 0, "type": "potion", "potion_id": "FLEX_POTION"}]},
        }

    assert RewardClaimer().to_claim(screen(held=1))["index"] == 0
    assert RewardClaimer().to_claim(screen(held=2)) is None
