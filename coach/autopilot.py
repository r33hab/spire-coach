"""spire-coach autopilot: whole real runs, played unattended on modded profile 2, built
on the fight lab's own move player, recording and recovery (`coach.autoplay`,
`coach.navigator`) rather than a second copy of either. No Claude, LLM or network while
it runs.

Fights are the fight lab's own: the coach's engine service picks every move
(`coach.autoplay.MovePlayer`), executed through the mod's API, recorded the same way.
Between fights, this asks the engine service's `strategy` method for the best option on
whatever non-combat screen is up, and executes it (`coach.strategyplay`); the coach's
own Act 1 model refuses past Act 1 by design, so the autopilot's engine service is
started with `STS2_STRATEGY_MODEL_DIR` pointing at the full-run champion instead
(`coach.engine.EngineClient`'s `extra_env`; the coach's own `coach` command is
untouched). When the champion has no answer (no export, or an option kind
`coach.strategyplay` does not map with confidence), the generic "first non-skip
option, then proceed" rule takes over (`coach.navigator.advance_one_step`).

Safety buffer (`--buffer-hp`, 0 = off): `debug_gain_max_hp` at the start of every fight
raises current and max HP together and rolls nothing else (STS2MCP McpMod.Debug.cs
`ExecuteDebugGainMaxHp` -> `CreatureCmd.GainMaxHp`) -- no RNG, no rewards, no enemies --
which is what keeps a buffered run's seed-exact replay checks meaningful. The action
already existed in the installed mod, so autopilot needed no mod change for this.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from coach import autoplay, navigator, saves, strategyplay
from coach.autoplay import MovePlayer
from coach.bridge.client import BridgeError, ModClient
from coach.bridge.live_run import LiveRunTracker
from coach.engine import EngineClient, EngineError
from coach.runlog import RunLog

DEFAULT_TIME_SCALE = 3.0
FULL_RUN_MODEL_DIR = (
    Path.home() / "Documents" / "git" / "sts2-wt" / "training" / "runs" / "full" / "export"
)
_STEP_TIMEOUT_S = 20.0
_MAX_SCREENS_PER_RUN = 4000  # stuck-safety cap (spec: "a timeout, then recovery")

# --- --restart-on-jam: quit, relaunch, and let the next run's own embark_run abandon
# the jammed one from the main menu (bug 3/7) ----------------------------------------
_STEAM_APP_ID = "2868840"
# Same pattern coach.saves.game_is_running already checks for.
_GAME_PROCESS_PATTERN = "SlayTheSpire2.app/Contents/MacOS"
_GAME_QUIT_TIMEOUT_S = 30.0
_GAME_HELLO_TIMEOUT_S = 90.0  # a cold Steam launch can take a while (README)


def _quit_game(*, timeout_s: float = _GAME_QUIT_TIMEOUT_S, poll_s: float = 1.0) -> None:
    """Force-quit the jammed game process: the jam leaves it deaf to a graceful
    `return_to_main_menu` (spec, and tonight's own recoveries -- a restart was the
    only thing that ever cleared it), so this is the same last resort a person at the
    keyboard used tonight, automated."""
    subprocess.run(["pkill", "-f", _GAME_PROCESS_PATTERN], check=False)
    deadline = time.monotonic() + timeout_s
    while saves.game_is_running() and time.monotonic() < deadline:
        time.sleep(poll_s)


def _launch_game() -> None:
    subprocess.run(["open", f"steam://rungameid/{_STEAM_APP_ID}"], check=False)


def _await_hello(client: ModClient, *, timeout_s: float, poll_s: float = 2.0) -> None:
    """Wait for the mod's `hello` to answer after a relaunch -- the root URL is only a
    health check (STS2MCP), so this is the same thing a person watching the terminal
    would do before trying anything else."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            client.hello()
            return
        except BridgeError:
            if time.monotonic() > deadline:
                raise
            time.sleep(poll_s)


def _restart_after_jam(
    client: ModClient,
    *,
    say: Callable[[str], None],
    quit_game: Callable[[], None],
    launch_game: Callable[[], None],
    hello_timeout_s: float = _GAME_HELLO_TIMEOUT_S,
) -> None:
    """Clear a jam the only way confirmed to work: quit the frozen game process and
    relaunch it. Abandoning the jammed run itself needs no extra step here -- the next
    `for` iteration's own `_play_run` calls `embark_run`, whose first move is already
    `_abandon_any_run_in_progress` from the main menu, exactly what the spec asks for.
    """
    say("  quitting the game")
    quit_game()
    say("  relaunching the game")
    launch_game()
    _await_hello(client, timeout_s=hello_timeout_s)
    say("  the mod answered; continuing")


@dataclass
class RunRecord:
    won: bool
    floor: int | None
    moves: int
    acts_reached: int
    buffered: bool


@dataclass
class AutopilotSummary:
    runs_played: int = 0
    moves_recorded: int = 0
    wins: int = 0
    deaths: int = 0
    act_transitions: int = 0
    records: list[RunRecord] = field(default_factory=list)

    def add(self, record: RunRecord) -> None:
        self.records.append(record)
        self.runs_played += 1
        self.moves_recorded += record.moves
        self.act_transitions += max(0, record.acts_reached - 1)
        if record.won:
            self.wins += 1
        else:
            self.deaths += 1

    def format(self) -> str:
        return (
            f"autopilot: {self.runs_played} run(s), {self.moves_recorded} move(s) "
            f"recorded -- {self.wins} win(s), {self.deaths} death(s), "
            f"{self.act_transitions} act transition(s) total"
        )


def run(
    client: ModClient,
    engine: EngineClient,
    runlog: RunLog,
    *,
    runs: int,
    ascension: int = 10,
    buffer_hp: int = 0,
    time_budget_s: float = 1.5,
    game_version: str = "unknown",
    stop: Callable[[], bool] = lambda: False,
    say: Callable[[str], None] = lambda _message: None,
    move_player: MovePlayer | None = None,
    restart_on_jam: bool = False,
    quit_game: Callable[[], None] = _quit_game,
    launch_game: Callable[[], None] = _launch_game,
    restart_hello_timeout_s: float = _GAME_HELLO_TIMEOUT_S,
) -> AutopilotSummary:
    """Play whole runs until `runs` is reached or `stop()` says otherwise. `move_player`
    is a seam for tests, as in `coach.fightlab.run`.

    On a game jam (`coach.autoplay.GameJamError`: `is_resolving` stuck true with no
    state change for about a minute, the game ignoring all input -- spec), the default
    is to stop the whole autopilot with a clear message, since only a game restart is
    known to clear it and doing that unasked would be a surprising thing for an
    unattended process to do on its own. `restart_on_jam=True` does it anyway: quits
    the game, relaunches it, and continues with the next run once the mod answers
    again."""
    player = move_player or MovePlayer(
        client,
        engine,
        runlog,
        time_budget_s=time_budget_s,
        game_version=game_version,
        source="autopilot",
    )
    initial_state = client.get_state()
    runlog.append(
        "session",
        {
            "command": "autopilot",
            "runs": runs,
            "ascension": ascension,
            "buffer_hp": buffer_hp,
            **navigator.session_header(initial_state),
        },
    )
    # Bug 5/7: started mid-run (at Neow, the map, a fight...) rather than at the main
    # menu, this used to call embark_run unconditionally regardless -- which abandons
    # nothing (no saved run to abandon, the current one is live), then waits forever
    # for a main menu that was never coming. Only the first of `runs` can possibly
    # resume a run already in progress; every run after that really did just end, back
    # at the main menu, and embarks fresh as before.
    resume_state = initial_state if initial_state.get("state_type") != "menu" else None
    summary = AutopilotSummary()
    try:
        for _ in range(runs):
            if stop():
                break
            say(
                f"run {summary.runs_played + 1}/{runs} at ascension {ascension}"
                + (f", buffered +{buffer_hp} hp/fight" if buffer_hp else "")
            )
            try:
                record = _play_run(
                    client,
                    player,
                    engine,
                    runlog,
                    ascension=ascension,
                    buffer_hp=buffer_hp,
                    stop=stop,
                    resume_state=resume_state,
                )
            except autoplay.GameJamError as exc:
                resume_state = None
                runlog.append("error", {"error": f"autopilot: {exc}"})
                if not restart_on_jam:
                    say(
                        f"  game jam detected ({exc}); stopping autopilot -- pass "
                        "--restart-on-jam to recover automatically next time"
                    )
                    break
                say(f"  game jam detected ({exc}); restarting the game")
                try:
                    _restart_after_jam(
                        client,
                        say=say,
                        quit_game=quit_game,
                        launch_game=launch_game,
                        hello_timeout_s=restart_hello_timeout_s,
                    )
                except BridgeError as restart_exc:
                    runlog.append(
                        "error", {"error": f"autopilot: restart-on-jam failed: {restart_exc}"}
                    )
                    say(f"  could not recover from the jam ({restart_exc}); stopping autopilot")
                    break
                continue
            except (BridgeError, navigator.NavigatorError) as exc:
                resume_state = None
                say(f"  run failed ({exc}); recovering")
                runlog.append("error", {"error": f"autopilot: {exc}"})
                _recover(client)
                continue
            resume_state = None
            summary.add(record)
            say(
                f"  -> {'won' if record.won else 'died'} on floor {record.floor}, "
                f"{record.moves} move(s), act {record.acts_reached} reached"
            )
    except KeyboardInterrupt:
        say("autopilot: stopping (Ctrl-C)")
    return summary


def _play_run(
    client: ModClient,
    player: MovePlayer,
    engine: EngineClient,
    runlog: RunLog,
    *,
    ascension: int,
    buffer_hp: int,
    stop: Callable[[], bool],
    resume_state: dict | None = None,
) -> RunRecord:
    # Bug 5/7: `resume_state` is the screen the game was already on when this
    # (the first) run started -- anything but the main menu -- so play continues
    # from there instead of embark_run abandoning/waiting for a run that is not over.
    state = resume_state if resume_state is not None else navigator.embark_run(
        client, ascension=ascension
    )
    buffered = buffer_hp > 0
    moves_before = player.stats.moves
    acts_reached = navigator.act_of(state) or 1
    tracker = LiveRunTracker()
    for _ in range(_MAX_SCREENS_PER_RUN):
        if stop():
            break
        state = autoplay.wait_settled(client, timeout_s=_STEP_TIMEOUT_S)
        acts_reached = max(acts_reached, navigator.act_of(state) or acts_reached)
        if autoplay.in_combat(state):
            if buffered:
                navigator.apply_hp_buffer(
                    client, runlog, buffer_hp, navigator.max_hp_of(state), source="autopilot"
                )
            outcome = player.play_fight(state)
            state = outcome.state
            if state.get("state_type") == "game_over":
                won = (outcome.last_hp or 0) > 0
                floor = (state.get("run") or {}).get("floor")
                navigator.leave_run(client, state)
                return RunRecord(
                    won=won,
                    floor=floor,
                    moves=player.stats.moves - moves_before,
                    acts_reached=acts_reached,
                    buffered=buffered,
                )
            continue
        if state.get("state_type") == "menu":
            # Left the run some other way; nothing more this run can do.
            return RunRecord(
                won=False,
                floor=None,
                moves=player.stats.moves - moves_before,
                acts_reached=acts_reached,
                buffered=buffered,
            )
        _play_screen(client, engine, tracker, state, runlog)
    raise navigator.NavigatorError(
        f"autopilot: {_MAX_SCREENS_PER_RUN} screens without the run ending"
    )


def _play_screen(
    client: ModClient, engine: EngineClient, tracker: LiveRunTracker, state: dict, runlog: RunLog
) -> None:
    if state.get("state_type") == "map" and _has_one_map_option(state):
        # No real decision to make (and one less round trip through the strategy
        # service that can fail with things like "NotReadyError: the current map
        # position is not known", seen live on an Act 2 map): choose the only option
        # directly. A failed click still falls through to the strategy/fallback path.
        options = (state.get("map") or {}).get("next_options") or []
        try:
            navigator.act_and_await_change(
                client, {"action": "choose_map_node", "index": options[0]["index"]}, state
            )
            return
        except BridgeError:
            pass
    live_run, screen = tracker.build(state)
    result = None
    if live_run is not None:
        try:
            result = engine.strategy(live_run, screen)
        except EngineError as exc:
            runlog.append(
                "strategy", {"state_type": state.get("state_type"), "error": str(exc)}
            )
    if result is not None and strategyplay.execute_best_option(client, state, result):
        return
    navigator.advance_one_step(client, state, runlog=runlog)


def _has_one_map_option(state: dict) -> bool:
    return len((state.get("map") or {}).get("next_options") or []) == 1


def _recover(client: ModClient) -> None:
    try:
        state = client.get_state()
    except BridgeError:
        time.sleep(2.0)
        return
    try:
        navigator.leave_run(client, state)
    except BridgeError:
        pass
