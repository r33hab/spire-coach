"""spire-coach: setup (saves, mod install, doctor), capture mode and the live coach.

This is the console entry point (`spire-coach`). Subcommands wire together
coach.saves, coach.modinstall and coach.capture, and for `coach` the live session,
the engine service, the panel's server and its window; this module's own job is
argument parsing, that wiring, and turning every exception at the boundary into one
clear line on stderr, never a traceback (Review Focus #2/#3).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from pathlib import Path

import uvicorn

from coach import autopilot, autoplay, dashboard, deckseed, fidelity_scoreboard, fightlab, navigator
from coach.bridge.client import BridgeAuthError, BridgeError, ModClient
from coach.bridge.versions import VersionStatus, check_versions, read_game_version
from coach.capture import capture_loop
from coach.config import (
    CONF_FILE_NAME,
    EXPECTED_GAME_VERSION,
    GAME_DIR,
    RELEASE_INFO,
    SAVE_ROOT,
    Config,
    load_config,
    read_allow_debug_actions,
    read_token,
)
from coach.engine import EngineClient
from coach.live import MODES, CoachSession, CoachSettings, engine_health_problems
from coach.modinstall import (
    DLL_NAME,
    MANIFEST_NAME,
    InstallError,
    build_mod,
    enable_mod_in_settings,
    install_mod,
)
from coach.runlog import RunLog
from coach.saves import SaveError, backup, find_steam_dir, game_is_running, restore, seed_modded
from coach.server import DEFAULT_PORT, HOST, Broadcaster, create_app, create_server
from coach.view import CoachState, compose, sentence
from coach.window import open_window

DEFAULT_BACKUP_ROOT = Path.home() / "Documents" / "spire-coach-backups"
DEFAULT_MOD_REPO = Path.home() / "Documents" / "git" / "STS2MCP"
MOD_ID = "STS2_MCP"
DEFAULT_WORKERS = 4  # the engine service's search workers (spec 5.3)
DEFAULT_BUDGET_S = 1.5  # seconds of search per position (spec 5.3, 7.3)
# Whether the engine's fight search plays the combat prior when neither --prior nor
# --no-prior is given; None would be whenever the prior loop has exported one. Off: at the
# coach's own budget (4 workers, 1.5 s, some 40,000 simulations a move) the round-1 prior
# played 300 held-out real fights to +0.42 HP (95% CI -0.19, +1.03) with 56 deaths against
# the plain search's 50, and 300 more (before the boss-check fixes) to +0.07 HP with 72
# against 60: 25 fights lost only with it, 7 only without (roadmap E2, sts2
# `rl/prior_loop/coach_check.py`).
DEFAULT_PRIOR: bool | None = False
DEFAULT_LAB_ACTS = (2, 3, 1)  # Act 2-3 first, then the rest
DEFAULT_LAB_ASCENSION = 10
DEFAULT_LAB_FIGHTS = 20
DEFAULT_LAB_TIME_SCALE = 3.0  # the fight lab's default (spec: "raise the game's time scale")
# HP only (never attack/Strength: that would shorten a fight and hide its later moves).
# Confirmed live that some bosses need more than a token buffer to show their full
# move set (fast per-turn scaling, and A10's second boss); 500 is a starting point, not
# a measured number -- raise it with --buffer-hp if a fight still ends early.
DEFAULT_LAB_BUFFER_HP = 500
DEFAULT_AUTOPILOT_RUNS = 1
DEFAULT_AUTOPILOT_BUFFER_HP = 0  # off by default (spec)
# The default 2.0s (coach.bridge.client.DEFAULT_TIMEOUT_S) is tuned for a human's pace;
# confirmed live that a menu transition, a room entry or a busy main thread under
# time_scale can take longer than that during unattended, back-to-back play.
LAB_CLIENT_TIMEOUT_S = 10.0

_SERVER_START_S = 10.0  # uvicorn listens well within this
_SERVER_STOP_S = 5.0
# How long one poll of the session can take before it sees it should stop: the mod's
# 2 s timeout plus the 1 s wait between polls while the game is away, with room to spare.
_POLL_S = 5.0
# What stops the coach: Ctrl-C, a plain `kill`, the terminal closing.
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


# --- doctor --------------------------------------------------------------------


def _doctor_checks(
    config: Config,
    *,
    game_dir: Path = GAME_DIR,
    release_info: Path = RELEASE_INFO,
    save_root: Path = SAVE_ROOT,
    is_running: Callable[[], bool] = game_is_running,
    client_factory: Callable[[str, str | None], ModClient] = ModClient,
) -> list[tuple[bool, str]]:
    """Run every doctor check and return (ok, line) pairs, in report order."""
    checks: list[tuple[bool, str]] = []

    if game_dir.is_dir():
        checks.append((True, "✓ game dir exists"))
    else:
        checks.append((False, f"✗ game dir exists: {game_dir} not found"))

    game_version = read_game_version(release_info)
    if game_version is None:
        checks.append((False, "✗ game version: unknown (release_info.json missing or unreadable)"))
    elif game_version != EXPECTED_GAME_VERSION:
        checks.append(
            (
                False,
                f"✗ game version: {game_version} does not match expected {EXPECTED_GAME_VERSION}",
            )
        )
    else:
        checks.append((True, f"✓ game version: {game_version}"))

    missing = [
        name
        for name in (DLL_NAME, MANIFEST_NAME, CONF_FILE_NAME)
        if not (config.mods_dir / name).is_file()
    ]
    if missing:
        checks.append(
            (False, f"✗ mod files are installed (DLL, JSON, conf): missing {', '.join(missing)}")
        )
    else:
        checks.append((True, "✓ mod files are installed (DLL, JSON, conf)"))

    conf_path = config.mods_dir / CONF_FILE_NAME
    if not conf_path.is_file():
        checks.append((False, "✗ token: STS2_MCP.conf not found"))
    else:
        try:
            json.loads(conf_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            checks.append((False, "✗ token: STS2_MCP.conf is not valid JSON"))
        else:
            token = read_token(config.mods_dir)
            if token:
                checks.append((True, "✓ token present"))
            else:
                checks.append((False, "✗ token: no token set in STS2_MCP.conf"))

    try:
        steam_dir = find_steam_dir(save_root)
        settings = json.loads((steam_dir / "settings.save").read_text(encoding="utf-8"))
        mod_settings = settings.get("mod_settings", {}) if isinstance(settings, dict) else {}
        mod_list = mod_settings.get("mod_list", []) if isinstance(mod_settings, dict) else []
        entry = next((e for e in mod_list if isinstance(e, dict) and e.get("id") == MOD_ID), None)
        enabled = (
            isinstance(mod_settings, dict)
            and mod_settings.get("mods_enabled") is True
            and entry is not None
            and entry.get("is_enabled") is True
        )
    except (SaveError, OSError, json.JSONDecodeError) as exc:
        checks.append((False, f"✗ mod is enabled in settings.save: {exc}"))
    else:
        if enabled:
            checks.append((True, "✓ mod is enabled in settings.save"))
        else:
            checks.append((False, "✗ mod is enabled in settings.save: mod is not enabled"))

    if is_running():
        checks.append((True, "✓ game is running"))
    else:
        checks.append((False, "✗ game is running: Slay the Spire 2 is not running"))

    try:
        client = client_factory(config.base_url, config.token)
    except Exception as exc:
        checks.append((False, f"✗ hello reachable: could not create a client: {exc}"))
        checks.append((False, "✗ auth accepted: not checked (hello unreachable)"))
        checks.append((False, "✗ versions ok: not checked (hello unreachable)"))
        return checks

    try:
        try:
            client.hello()
            hello_ok = True
        except BridgeError as exc:
            checks.append((False, f"✗ hello reachable: {exc}"))
            hello_ok = False

        if hello_ok:
            checks.append((True, "✓ hello reachable"))
            try:
                payload = client.get_state()
                auth_ok = True
            except BridgeError as exc:
                checks.append((False, f"✗ auth accepted: {exc}"))
                auth_ok = False
        else:
            checks.append((False, "✗ auth accepted: not checked (hello unreachable)"))
            auth_ok = False

        if auth_ok:
            checks.append((True, "✓ auth accepted"))
            status = check_versions(game_version, payload)
            if status.ok:
                checks.append((True, "✓ versions ok"))
            else:
                checks.append((False, f"✗ versions ok: {'; '.join(status.problems)}"))
        else:
            reason = "hello unreachable" if not hello_ok else "auth failed"
            checks.append((False, f"✗ versions ok: not checked ({reason})"))
    finally:
        client.close()

    return checks


def cmd_doctor(
    config: Config,
    *,
    game_dir: Path = GAME_DIR,
    release_info: Path = RELEASE_INFO,
    save_root: Path = SAVE_ROOT,
    is_running: Callable[[], bool] = game_is_running,
    client_factory: Callable[[str, str | None], ModClient] = ModClient,
) -> int:
    checks = _doctor_checks(
        config,
        game_dir=game_dir,
        release_info=release_info,
        save_root=save_root,
        is_running=is_running,
        client_factory=client_factory,
    )
    for _, line in checks:
        print(line)
    return 0 if all(ok for ok, _ in checks) else 1


# --- capture ---------------------------------------------------------------------


def cmd_capture(config: Config) -> int:
    log = RunLog(config.data_dir)
    client = ModClient(config.base_url, config.token)
    try:
        stats = capture_loop(
            client, log, poll_interval_s=config.poll_interval_s, stop=lambda: False
        )
    except KeyboardInterrupt:
        print("capture stopped")
        return 0
    finally:
        client.close()
    print(f"captured {stats.records} state(s), {stats.actions} action(s)")
    if stats.error_message:
        print(stats.error_message, file=sys.stderr)
    return 2 if stats.errors else 0


# --- saves -------------------------------------------------------------------------


def cmd_saves_backup(backup_root: Path, *, is_running: Callable[[], bool] = game_is_running) -> int:
    steam_dir = find_steam_dir(SAVE_ROOT)
    dest = backup(steam_dir, backup_root, is_running=is_running)
    print(f"backed up {steam_dir} to {dest}")
    return 0


def cmd_saves_seed_modded(
    backup_root: Path,
    source_profile: int,
    target_slot: int,
    *,
    is_running: Callable[[], bool] = game_is_running,
) -> int:
    steam_dir = find_steam_dir(SAVE_ROOT)
    result = seed_modded(
        steam_dir,
        backup_root,
        source_profile=source_profile,
        target_slot=target_slot,
        is_running=is_running,
    )
    print(f"backed up {steam_dir} to {result.backup_path}")
    for path in result.written:
        print(f"wrote {path}")
    return 0


def cmd_saves_restore(
    backup_path: Path, *, is_running: Callable[[], bool] = game_is_running
) -> int:
    steam_dir = find_steam_dir(SAVE_ROOT)
    aside = restore(backup_path, steam_dir, is_running=is_running)
    print(f"restored {backup_path} to {steam_dir}")
    print(f"previous contents moved to {aside}")
    return 0


# --- mod -----------------------------------------------------------------------------


def cmd_mod_install(
    config: Config,
    repo: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    is_running: Callable[[], bool] = game_is_running,
) -> int:
    manifest = repo / "mod_manifest.json"
    dll = build_mod(repo, game_dir=GAME_DIR, run=run)
    print(f"built {dll}")
    result = install_mod(dll, manifest, config.mods_dir, is_running=is_running)
    print(f"installed mod to {config.mods_dir}")
    if result.previous_backup is not None:
        print(f"previous install backed up to {result.previous_backup}")
    print("generated a new token" if result.token_created else "kept the existing token")
    return 0


def cmd_mod_enable(backup_root: Path, *, is_running: Callable[[], bool] = game_is_running) -> int:
    steam_dir = find_steam_dir(SAVE_ROOT)
    settings_path = steam_dir / "settings.save"
    backup_path = enable_mod_in_settings(
        settings_path, backup_root, mod_id=MOD_ID, is_running=is_running
    )
    print(f"enabled {MOD_ID} in {settings_path}")
    print(f"backed up {settings_path.name} to {backup_path}")
    return 0


# --- coach -----------------------------------------------------------------------------


def cmd_dashboard(config: Config, *, port: int, root: Path, open_browser: bool) -> int:
    """Serve the dashboard on 127.0.0.1:`port` until Ctrl-C (read-only; see coach.dashboard)."""
    with socket.socket() as probe:  # uvicorn exits silently on a taken port: say it first
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn binds
        try:
            probe.bind((HOST, port))
        except OSError:
            _say(
                f"Port {port} is taken: is a dashboard already running? Pick another with --port.",
                error=True,
            )
            return 1
    client = ModClient(config.base_url, config.token, timeout_s=dashboard.PROBE_TIMEOUT_S)
    model_path = config.data_dir / "models" / "strategy" / "model.json"
    fidelity_dir = config.data_dir / "fidelity"
    app = dashboard.create_app(
        dashboard.snapshot_source(root, client, model_path, fidelity_dir=fidelity_dir)
    )
    server = create_server(app, port=port)
    url = f"http://{HOST}:{port}/"
    _say(f"The dashboard is at {url} (training job: {root})")
    _say("Press Ctrl-C to stop it.")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        server.run()
    finally:
        client.close()
    return 0


def cmd_fidelity_scoreboard(
    config: Config,
    *,
    sts2_repo: Path,
    runs_dir: Path | None,
    out_dir: Path | None,
    resamples: int | None,
) -> int:
    """Score the simulator against every captured session (see coach.fidelity_scoreboard)."""
    runs_dir = runs_dir or (config.data_dir / "runs")
    out_dir = out_dir or (config.data_dir / "fidelity")
    if not fidelity_scoreboard.discover_sessions(runs_dir):
        _say(f"No session logs found under {runs_dir}.", error=True)
        return 1
    doc = fidelity_scoreboard.build_scoreboard(runs_dir, sts2_repo, resamples=resamples)
    stamped, latest = fidelity_scoreboard.write_scoreboard(doc, out_dir)
    _say(fidelity_scoreboard.format_table(doc))
    _say(f"\nWrote {stamped} and {latest}")
    return 0


# --- fight lab and autopilot -----------------------------------------------------


def _read_fidelity_doc(data_dir: Path) -> dict | None:
    path = data_dir / "fidelity" / fidelity_scoreboard.LATEST_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _set_time_scale(client: ModClient, time_scale: float) -> None:
    try:
        client.post_action({"action": "set_time_scale", "scale": time_scale})
    except BridgeError as exc:
        _say(f"could not raise the time scale to {time_scale}: {exc}", error=True)


def cmd_fight_lab(
    config: Config,
    *,
    fights: int = DEFAULT_LAB_FIGHTS,
    time_scale: float = DEFAULT_LAB_TIME_SCALE,
    acts: list[int] | None = None,
    ascension: int = DEFAULT_LAB_ASCENSION,
    buffer_hp: int = DEFAULT_LAB_BUFFER_HP,
    workers: int = DEFAULT_WORKERS,
    budget_s: float = DEFAULT_BUDGET_S,
    prior: bool | None = None,
    client_factory: Callable[[str, str | None], ModClient] | None = None,
    engine_factory: Callable[[], EngineClient] | None = None,
) -> int:
    """Play `fights` real fights unattended and record every move (see
    coach.fightlab). Stops at the first unmet precondition, printing exactly what to
    fix, same as `doctor`."""
    acts = list(DEFAULT_LAB_ACTS) if acts is None else acts
    prior_dir = _prior_dir(config, prior)
    client = (
        client_factory or (lambda url, token: ModClient(url, token, timeout_s=LAB_CLIENT_TIMEOUT_S))
    )(config.base_url, config.token)
    try:
        problems = navigator.check_preconditions(client, config)
        if problems:
            for problem in problems:
                _say(problem, error=True)
            return 1
        _set_time_scale(client, time_scale)
        engine = (engine_factory or (lambda: EngineClient(workers=workers, prior_dir=prior_dir)))()
        _say_prior(prior_dir)
        try:
            problems = engine_health_problems(engine, timeout_s=config.engine_start_timeout_s)
            if problems:
                for problem in problems:
                    _say(f"engine: {problem}", error=True)
                return 1
            runlog = RunLog(config.data_dir, subdir="runs", prefix="fightlab-")
            _say(f"recording to {runlog.path}")
            summary = fightlab.run(
                client,
                engine,
                runlog,
                fights=fights,
                acts=acts,
                ascension=ascension,
                buffer_hp=buffer_hp,
                time_budget_s=budget_s,
                game_version=read_game_version() or "unknown",
                fidelity_doc=_read_fidelity_doc(config.data_dir),
                shards_root=deckseed.DEFAULT_SHARDS_ROOT,
                say=_say,
            )
            _say(summary.format())
        finally:
            engine.close()
    finally:
        client.close()
    return 0


def autopilot_engine_env(*, deck_rollouts: bool, prior_dir: Path | None) -> dict[str, str]:
    """The engine service's environment for the autopilot: the full-run champion, and
    measured fights at deck decisions when asked (sts2 `deckfights`)."""
    env = {"STS2_STRATEGY_MODEL_DIR": str(autopilot.FULL_RUN_MODEL_DIR)}
    if deck_rollouts:
        env["STS2_DECK_ROLLOUTS"] = "1"
        env["STS2_DECK_ROLLOUT_PRIOR"] = str(prior_dir or "")
    return env


def cmd_autopilot(
    config: Config,
    *,
    runs: int = DEFAULT_AUTOPILOT_RUNS,
    time_scale: float = DEFAULT_LAB_TIME_SCALE,
    ascension: int = DEFAULT_LAB_ASCENSION,
    buffer_hp: int = DEFAULT_AUTOPILOT_BUFFER_HP,
    workers: int = DEFAULT_WORKERS,
    budget_s: float = DEFAULT_BUDGET_S,
    restart_on_jam: bool = False,
    auto_kill: bool = True,
    auto_kill_rounds: int = autoplay.DEFAULT_AUTO_KILL_NO_PROGRESS_ROUNDS,
    prior: bool | None = None,
    client_factory: Callable[[str, str | None], ModClient] | None = None,
    engine_factory: Callable[[], EngineClient] | None = None,
    fresh_run: bool = False,
    deck_rollouts: bool = False,
) -> int:
    """Play `runs` whole real runs unattended, Ironclad at `ascension`, using the fight
    lab's own move player and recording between real fights too (see coach.autopilot).
    """
    prior_dir = _prior_dir(config, prior)
    client = (
        client_factory or (lambda url, token: ModClient(url, token, timeout_s=LAB_CLIENT_TIMEOUT_S))
    )(config.base_url, config.token)
    try:
        problems = navigator.check_preconditions(client, config)
        if problems:
            for problem in problems:
                _say(problem, error=True)
            return 1
        _set_time_scale(client, time_scale)
        engine = (
            engine_factory
            or (
                lambda: EngineClient(
                    workers=workers,
                    extra_env=autopilot_engine_env(deck_rollouts=deck_rollouts, prior_dir=prior_dir),
                    prior_dir=prior_dir,
                )
            )
        )()
        _say_prior(prior_dir)
        try:
            problems = engine_health_problems(engine, timeout_s=config.engine_start_timeout_s)
            if problems:
                for problem in problems:
                    _say(f"engine: {problem}", error=True)
                return 1
            runlog = RunLog(config.data_dir, subdir="runs", prefix="autopilot-")
            _say(f"recording to {runlog.path}")
            summary = autopilot.run(
                client,
                engine,
                runlog,
                runs=runs,
                ascension=ascension,
                buffer_hp=buffer_hp,
                time_budget_s=budget_s,
                game_version=read_game_version() or "unknown",
                say=_say,
                restart_on_jam=restart_on_jam,
                auto_kill=auto_kill,
                auto_kill_no_progress_rounds=auto_kill_rounds,
                allow_debug_actions=read_allow_debug_actions(config.mods_dir),
                fresh_run=fresh_run,
            )
            _say(summary.format())
        finally:
            engine.close()
    finally:
        client.close()
    return 0


class CoachError(Exception):
    """The live coach could not start. The message is one line for the user."""


def combat_prior_dir(config: Config) -> Path:
    """Where the prior loop exports the combat prior the fight search can play."""
    return config.data_dir / "models" / "combat_prior"


def _prior_dir(config: Config, prior: bool | None) -> Path | None:
    """The combat prior the engine's fight search plays, or None for the plain search.

    `prior` is --prior (True), --no-prior (False) or neither (None, `DEFAULT_PRIOR`: on
    when a prior is exported to the data folder, off when none is).

    Raises:
        CoachError: If --prior asks for a prior and none is exported.
    """
    folder = combat_prior_dir(config)
    exported = (folder / "model.json").is_file() and (folder / "combat_policy.pt").is_file()
    wanted = prior if prior is not None else DEFAULT_PRIOR
    if wanted is None:
        wanted = exported
    if not wanted:
        return None
    if not exported:
        raise CoachError(f"--prior: no combat prior is exported to {folder}")
    return folder


def _say_prior(prior_dir: Path | None) -> None:
    if prior_dir is not None:
        _say(f"The fight search plays the combat prior in {prior_dir}.")


def _wait_for_ctrl_c() -> None:
    """Block until Ctrl-C (or SIGTERM or SIGHUP), which raises KeyboardInterrupt here."""
    while True:
        time.sleep(1.0)


def _say(text: str, *, error: bool = False) -> None:
    """One line for the user. A terminal that has gone away (a broken pipe) is no reason
    to stop the coach, least of all while it is stopping."""
    with contextlib.suppress(OSError):
        print(text, file=sys.stderr if error else sys.stdout, flush=True)


def cmd_coach(
    config: Config,
    *,
    workers: int = DEFAULT_WORKERS,
    budget_s: float = DEFAULT_BUDGET_S,
    port: int = DEFAULT_PORT,
    window: bool = True,
    mode: str = "show",
    prior: bool | None = None,
    engine_cmd: list[str] | None = None,
    client_factory: Callable[[str, str | None], ModClient] | None = None,
    show_window: Callable[[str], bool] | None = None,
    wait_for_ctrl_c: Callable[[], None] | None = None,
) -> int:
    """Coach live until Ctrl-C, or until the panel's window is closed.

    The engine service starts first and is health-checked. Then the panel's server and
    the coach session each get a thread of their own, and the window takes the main
    thread, as pywebview needs on macOS. When the window cannot open, the panel is one
    browser tab away and the coach runs until Ctrl-C. SIGTERM (a plain `kill`) and SIGHUP
    (the terminal closing) stop it as Ctrl-C does. `engine_cmd` replaces the engine
    service's launch command; the last three keywords replace the mod client, the window
    and the wait for Ctrl-C (tests use stand-ins). `prior` is --prior/--no-prior: whether
    the fight search plays the exported combat prior (`_prior_dir`).
    """
    prior_dir = _prior_dir(config, prior)
    settings = CoachSettings(
        mode=mode,
        time_budget_s=budget_s,
        poll_interval_s=config.poll_interval_s,
        engine_start_timeout_s=config.engine_start_timeout_s,
    )
    with _stop_signals_raise_keyboard_interrupt():
        coach = _LiveCoach(
            (client_factory or ModClient)(config.base_url, config.token),
            EngineClient(engine_cmd, workers=workers, prior_dir=prior_dir),
            settings,
        )
        _say_prior(prior_dir)
        try:
            coach.start(config.data_dir, port)
            url = f"http://{HOST}:{port}/"
            _say(f"The coach panel is at {url}")
            if window:
                _say("Close the panel's window or press Ctrl-C to stop the coach.")
                if (show_window or open_window)(url):
                    return 0
            _say("Press Ctrl-C to stop the coach.")
            (wait_for_ctrl_c or _wait_for_ctrl_c)()
        except KeyboardInterrupt:
            pass
        finally:
            coach.stop()
    return 0


class _LiveCoach:
    """The live coach's parts: started in order by `start`, stopped in reverse by `stop`."""

    def __init__(self, client: ModClient, engine: EngineClient, settings: CoachSettings) -> None:
        self._client = client
        self._engine = engine
        self._settings = settings
        self._stopping = threading.Event()
        self._session: CoachSession | None = None
        self._session_thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None

    def start(self, data_dir: Path, port: int) -> None:
        _say("Starting the engine service…")
        # A cold start of the service can take a while: this first check may wait for
        # it. The session checks a failing engine again by itself.
        problems = engine_health_problems(
            self._engine, timeout_s=self._settings.engine_start_timeout_s
        )
        for problem in problems:
            _say(
                f"{sentence(f'Advice is off: {problem}')} Turn on the version override in "
                "the panel to see advice anyway.",
                error=True,
            )
        broadcaster = Broadcaster()
        feed = _PanelFeed(_starting_state(self._settings), broadcaster)
        self._session = CoachSession(
            self._client,
            self._engine,
            publish=feed.publish,
            data_dir=data_dir,
            settings=self._settings,
            engine_problems=problems,
        )
        app = create_app(feed.state, self._apply_settings, broadcaster=broadcaster)
        self._server = create_server(app, port=port)
        self._server_thread = _start_thread(_serve, self._server, name="coach-server")
        _wait_until_listening(self._server, self._server_thread, port)
        self._session_thread = _start_thread(
            self._session.run, self._stopping.is_set, name="coach-session"
        )

    def stop(self) -> None:
        """Stop whatever was started: the session first, so that no engine call is cut
        off early, then the engine service, the panel's server and the mod client. Each
        step runs even when one before it failed. Ctrl-C, SIGTERM and SIGHUP are ignored
        meanwhile: every wait here is bounded, and one of them would cut stopping short
        and could leave the engine service running."""
        with _stop_signals_ignored(), contextlib.ExitStack() as then:
            then.callback(self._client.close)
            then.callback(self._stop_server)
            then.callback(self._engine.close)
            self._stop_session()

    def _stop_session(self) -> None:
        self._stopping.set()
        if self._session_thread is None:
            if self._session is not None:
                self._session.close()  # its engine thread runs from the start
            return
        _say("Stopping the coach…")
        # One poll to see the stop, then the session's own close waits for its engine
        # thread.
        bound = _POLL_S + self._session.close_timeout_s
        self._session_thread.join(bound)
        if self._session_thread.is_alive():
            _say(
                f"The coach session did not stop within {bound:.0f} s; closing the engine "
                "service now, which cuts off any request it is still waiting on.",
                error=True,
            )

    def _stop_server(self) -> None:
        if self._server_thread is None:
            return
        self._server.should_exit = True
        self._server_thread.join(_SERVER_STOP_S)
        if self._server_thread.is_alive():
            self._server.force_exit = True  # a panel that will not let go
            self._server_thread.join(_SERVER_STOP_S)

    def _apply_settings(self, update: dict) -> None:
        """A settings update from the panel, already validated: the mode changes at once,
        the override from the session's next poll (it reads the setting on every poll)."""
        if "mode" in update:
            self._session.set_mode(update["mode"])
        if "override_versions" in update:
            self._settings.override_versions = update["override_versions"]


class _PanelFeed:
    """The newest state for the panel's app to read, and the broadcaster that pushes each.

    `publish` is the session's publish callback. It keeps each state BEFORE handing it
    to the broadcaster (coach/server.py's contract): a panel that connects meanwhile
    reads the state as its first one, or has it pushed; never neither.
    """

    def __init__(self, first: CoachState, broadcaster: Broadcaster) -> None:
        self._state = first
        self._broadcaster = broadcaster

    def state(self) -> CoachState:
        return self._state

    def publish(self, state: CoachState) -> None:
        self._state = state
        self._broadcaster.publish(state)


def _starting_state(settings: CoachSettings) -> CoachState:
    """What the panel shows until the session's first poll: waiting for the game."""
    return compose(
        None,
        connected=False,
        bridge_problem=None,
        internal_error=None,
        versions=VersionStatus(ok=True, problems=[]),
        override_versions=settings.override_versions,
        mode=settings.mode,
        last_grade=None,
        run={"act": None, "floor": None},
        fidelity_warnings=[],
        engine_restarting=False,
        z=settings.thresholds.z,
        potion_value_hp=settings.potion_value_hp,
    )


def _start_thread(target: Callable[..., object], *args: object, name: str) -> threading.Thread:
    # A daemon: a thread that failed to stop in time never keeps the process alive.
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


def _serve(server: uvicorn.Server) -> None:
    """Run the panel's server until it is told to exit. When it cannot listen (the port
    is taken) uvicorn leaves by SystemExit, and the caller sees it never started."""
    with contextlib.suppress(SystemExit):
        server.run()


def _wait_until_listening(server: uvicorn.Server, thread: threading.Thread, port: int) -> None:
    deadline = time.monotonic() + _SERVER_START_S
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise CoachError(
                f"The coach panel could not listen on {HOST}:{port}: is another coach "
                "running? Choose another port with --port."
            )
        time.sleep(0.01)


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


@contextlib.contextmanager
def _stop_signals_raise_keyboard_interrupt() -> Iterator[None]:
    """SIGTERM (a plain `kill`) and SIGHUP (the terminal closing) raise KeyboardInterrupt
    on the main thread, as Ctrl-C does, so the coach stops in order. (With the window
    open, coach/window.py makes them close the window instead.)"""
    with _signal_handlers({signal.SIGTERM: _interrupt, signal.SIGHUP: _interrupt}):
        yield


@contextlib.contextmanager
def _stop_signals_ignored() -> Iterator[None]:
    with _signal_handlers(dict.fromkeys(_STOP_SIGNALS, signal.SIG_IGN)):
        yield


@contextlib.contextmanager
def _signal_handlers(handlers: dict) -> Iterator[None]:
    """Set these signal handlers for a while. Only the main thread may change signal
    handling; on any other thread this does nothing."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {signum: signal.signal(signum, handler) for signum, handler in handlers.items()}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            if handler is None:  # not set from Python: its own default is the best guess
                handler = signal.default_int_handler if signum == signal.SIGINT else signal.SIG_DFL
            signal.signal(signum, handler)


# --- argument parsing ------------------------------------------------------------------


def _worker_count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {text}")
    return value


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {text}")
    return value


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"must be a port from 1 to 65535, got {text}")
    return value


def _fight_count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {text}")
    return value


def _ascension(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if not 0 <= value <= 20:
        raise argparse.ArgumentTypeError(f"must be an ascension level from 0 to 20, got {text}")
    return value


def _acts(text: str) -> list[int]:
    acts = []
    for token in text.split(","):
        token = token.strip()
        try:
            act = int(token)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{token!r} is not a whole number") from None
        if act not in (1, 2, 3):
            raise argparse.ArgumentTypeError(f"act must be 1, 2 or 3, got {token}")
        acts.append(act)
    if not acts:
        raise argparse.ArgumentTypeError("at least one act is required")
    return acts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spire-coach")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="check the game, mod and token are ready to capture")
    subparsers.add_parser("capture", help="record every settled state and action to disk")

    saves_parser = subparsers.add_parser("saves", help="back up, seed or restore save profiles")
    saves_sub = saves_parser.add_subparsers(dest="saves_command", required=True)

    backup_parser = saves_sub.add_parser("backup", help="back up the Steam save folder")
    backup_parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)

    seed_parser = saves_sub.add_parser(
        "seed-modded", help="copy a normal profile's progress and prefs into a modded slot"
    )
    seed_parser.add_argument("--from", dest="source_profile", type=int, required=True)
    seed_parser.add_argument("--to", dest="target_slot", type=int, required=True)
    seed_parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)

    restore_parser = saves_sub.add_parser("restore", help="restore a save backup")
    restore_parser.add_argument("path", type=Path)

    mod_parser = subparsers.add_parser("mod", help="build, install or enable the STS2_MCP mod")
    mod_sub = mod_parser.add_subparsers(dest="mod_command", required=True)

    install_parser = mod_sub.add_parser("install", help="build and install the mod")
    install_parser.add_argument("--repo", type=Path, default=DEFAULT_MOD_REPO)

    enable_parser = mod_sub.add_parser("enable", help="enable the mod in settings.save")
    enable_parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)

    coach_parser = subparsers.add_parser(
        "coach", help="coach you live while you play, in a floating panel"
    )
    coach_parser.add_argument(
        "--workers",
        type=_worker_count,
        default=DEFAULT_WORKERS,
        help="search worker processes of the engine service (default: %(default)s)",
    )
    coach_parser.add_argument(
        "--budget",
        type=_seconds,
        default=DEFAULT_BUDGET_S,
        help="seconds of search per position (default: %(default)s)",
    )
    coach_parser.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        help="the panel's port on 127.0.0.1 (default: %(default)s)",
    )
    coach_parser.add_argument(
        "--no-window",
        action="store_true",
        help="no floating window: the terminal prints the panel's URL; open it in a browser tab",
    )
    coach_parser.add_argument(
        "--mode",
        choices=MODES,
        default="show",
        help="show the best move at once, or quiz: hide it until you move (default: %(default)s)",
    )
    _add_prior_option(coach_parser)

    dashboard_parser = subparsers.add_parser(
        "dashboard", help="a local page with training progress, the game and the coach"
    )
    dashboard_parser.add_argument(
        "--port",
        type=_port,
        default=dashboard.DEFAULT_DASHBOARD_PORT,
        help="the dashboard's port on 127.0.0.1 (default: %(default)s)",
    )
    dashboard_parser.add_argument(
        "--training-root",
        type=Path,
        default=None,
        help=f"the training job's folder (default: ${dashboard.TRAINING_ROOT_ENV}, else the first "
        "of the training worktree's and the sts2 checkout's runs/stage2 that exists)",
    )
    dashboard_parser.add_argument(
        "--no-open", action="store_true", help="do not open the page in the browser"
    )

    fidelity_parser = subparsers.add_parser(
        "fidelity-scoreboard",
        help="score the simulator against every captured session (roadmap task A1)",
    )
    fidelity_parser.add_argument(
        "--sts2-repo",
        type=Path,
        default=fidelity_scoreboard.DEFAULT_STS2_REPO,
        help="the sts2 checkout to score against (default: %(default)s)",
    )
    fidelity_parser.add_argument(
        "--runs-dir", type=Path, default=None, help="session logs (default: <data dir>/runs)"
    )
    fidelity_parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="where to write the scoreboard (default: <data dir>/fidelity)",
    )
    fidelity_parser.add_argument(
        "--resamples",
        type=int,
        default=None,
        help="resamples per check (default: the sts2 checkout's own default)",
    )

    lab_parser = subparsers.add_parser(
        "fight-lab",
        help="play many real fights unattended and record every move, for the fidelity scoreboard",
    )
    lab_parser.add_argument(
        "--fights",
        type=_fight_count,
        default=DEFAULT_LAB_FIGHTS,
        help="how many fights to play before stopping (default: %(default)s)",
    )
    lab_parser.add_argument(
        "--time-scale",
        type=_seconds,
        default=DEFAULT_LAB_TIME_SCALE,
        help="the game's time scale while the lab runs (default: %(default)s)",
    )
    lab_parser.add_argument(
        "--acts",
        type=_acts,
        default=list(DEFAULT_LAB_ACTS),
        help="which acts to draw fights from, comma-separated (default: %(default)s)",
    )
    lab_parser.add_argument(
        "--ascension",
        type=_ascension,
        default=DEFAULT_LAB_ASCENSION,
        help="ascension level for every fight (default: %(default)s)",
    )
    lab_parser.add_argument(
        "--buffer-hp",
        type=int,
        default=DEFAULT_LAB_BUFFER_HP,
        help="extra current+max HP for elite/boss fights only, 0 = off, HP-only, "
        "so a fight lasts long enough to show its full move set (default: %(default)s)",
    )
    lab_parser.add_argument(
        "--workers",
        type=_worker_count,
        default=DEFAULT_WORKERS,
        help="search worker processes of the engine service (default: %(default)s)",
    )
    lab_parser.add_argument(
        "--budget",
        type=_seconds,
        default=DEFAULT_BUDGET_S,
        help="seconds of search per position (default: %(default)s)",
    )
    _add_prior_option(lab_parser)

    autopilot_parser = subparsers.add_parser(
        "autopilot",
        help="play whole real runs unattended, with an optional HP safety buffer",
    )
    autopilot_parser.add_argument(
        "--runs",
        type=_fight_count,
        default=DEFAULT_AUTOPILOT_RUNS,
        help="how many runs to play before stopping (default: %(default)s)",
    )
    autopilot_parser.add_argument(
        "--buffer-hp",
        type=int,
        default=DEFAULT_AUTOPILOT_BUFFER_HP,
        help="extra current+max HP at the start of every fight, 0 = off (default: %(default)s)",
    )
    autopilot_parser.add_argument(
        "--ascension",
        type=_ascension,
        default=DEFAULT_LAB_ASCENSION,
        help="ascension level for every run (default: %(default)s)",
    )
    autopilot_parser.add_argument(
        "--time-scale",
        type=_seconds,
        default=DEFAULT_LAB_TIME_SCALE,
        help="the game's time scale while autopilot runs (default: %(default)s)",
    )
    autopilot_parser.add_argument(
        "--workers",
        type=_worker_count,
        default=DEFAULT_WORKERS,
        help="search worker processes of the engine service (default: %(default)s)",
    )
    autopilot_parser.add_argument(
        "--budget",
        type=_seconds,
        default=DEFAULT_BUDGET_S,
        help="seconds of search per position (default: %(default)s)",
    )
    autopilot_parser.add_argument(
        "--restart-on-jam",
        action="store_true",
        help=(
            "on a game jam (is_resolving stuck, the game ignoring all input), quit "
            "and relaunch the game and continue with the next run, instead of "
            "stopping (default: off -- stop with a clear message)"
        ),
    )
    autopilot_parser.add_argument(
        "--auto-kill",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "on no progress (a wall like The Insatiable, Aeonglass, Entomancer, "
            "Lagavulin Matriarch or the Queen), call the mod's debug_kill_enemy and "
            "keep playing instead of ending the run (needs allow_debug_actions; "
            "default: on)"
        ),
    )
    autopilot_parser.add_argument(
        "--fresh-run",
        action="store_true",
        help="abandon any saved run at start and begin a new seed (default: resume it)",
    )
    autopilot_parser.add_argument(
        "--auto-kill-rounds",
        type=int,
        default=autoplay.DEFAULT_AUTO_KILL_NO_PROGRESS_ROUNDS,
        help=(
            "rounds without a new low in total enemy hp before --auto-kill acts "
            "(default: %(default)s)"
        ),
    )
    _add_prior_option(autopilot_parser)
    autopilot_parser.add_argument(
        "--deck-rollouts",
        action="store_true",
        help="measure deck decisions with paired simulated fights (slower; sts2 deckfights)",
    )

    return parser


def _add_prior_option(parser: argparse.ArgumentParser) -> None:
    default = {
        None: "on when a combat prior is exported to the data folder, off when none is",
        True: "on",
        False: "off",
    }[DEFAULT_PRIOR]
    parser.add_argument(
        "--prior",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"let the combat prior guide the fight search (default: {default})",
    )


def _current_operation(args: argparse.Namespace) -> str:
    return (
        getattr(args, "saves_command", None) or getattr(args, "mod_command", None) or args.command
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config()

        if args.command == "doctor":
            return cmd_doctor(config)
        if args.command == "capture":
            return cmd_capture(config)
        if args.command == "saves":
            if args.saves_command == "backup":
                return cmd_saves_backup(args.backup_root)
            if args.saves_command == "seed-modded":
                return cmd_saves_seed_modded(
                    args.backup_root, args.source_profile, args.target_slot
                )
            if args.saves_command == "restore":
                return cmd_saves_restore(args.path)
        if args.command == "mod":
            if args.mod_command == "install":
                return cmd_mod_install(config, args.repo)
            if args.mod_command == "enable":
                return cmd_mod_enable(args.backup_root)
        if args.command == "coach":
            return cmd_coach(
                config,
                workers=args.workers,
                budget_s=args.budget,
                port=args.port,
                window=not args.no_window,
                mode=args.mode,
                prior=args.prior,
            )
        if args.command == "dashboard":
            return cmd_dashboard(
                config,
                port=args.port,
                root=args.training_root or dashboard.training_root(),
                open_browser=not args.no_open,
            )
        if args.command == "fidelity-scoreboard":
            return cmd_fidelity_scoreboard(
                config,
                sts2_repo=args.sts2_repo,
                runs_dir=args.runs_dir,
                out_dir=args.out_dir,
                resamples=args.resamples,
            )
        if args.command == "fight-lab":
            return cmd_fight_lab(
                config,
                fights=args.fights,
                time_scale=args.time_scale,
                acts=args.acts,
                ascension=args.ascension,
                buffer_hp=args.buffer_hp,
                workers=args.workers,
                budget_s=args.budget,
                prior=args.prior,
            )
        if args.command == "autopilot":
            return cmd_autopilot(
                config,
                runs=args.runs,
                time_scale=args.time_scale,
                ascension=args.ascension,
                buffer_hp=args.buffer_hp,
                workers=args.workers,
                budget_s=args.budget,
                restart_on_jam=args.restart_on_jam,
                auto_kill=args.auto_kill,
                auto_kill_rounds=args.auto_kill_rounds,
                fresh_run=args.fresh_run,
                prior=args.prior,
                deck_rollouts=args.deck_rollouts,
            )
    except navigator.NavigatorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except BridgeAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (SaveError, InstallError, CoachError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        operation = _current_operation(args)
        path = exc.filename or getattr(exc, "filename2", None) or ""
        strerror = exc.strerror or str(exc)
        print(f"{operation} failed: {strerror} ({path})", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0  # pragma: no cover - argparse's required=True prevents reaching here


if __name__ == "__main__":
    sys.exit(main())
