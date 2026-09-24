# Live Coach (coach side) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `spire-coach coach` runs the live coach while the user plays:
- it watches the game;
- analyzes every settled combat decision with the engine service;
- shows the best move, the expected line, alternatives and warnings in a floating always-on-top panel;
- grades each move the player makes (Best/Good/Inaccuracy/Mistake/Blunder);
- runs a one-step fidelity check after each move and writes gap reports;
- records everything to the run log.

**Architecture:**
- `coach/engine.py` is a client for the out-of-process engine service. It spawns `uv run --project ~/Documents/git/sts2 python -m sts2_gym.live.service` and speaks JSON lines. See `docs/superpowers/plans/2026-09-24-sim-engine-service.md` for the protocol.
- `coach/grader.py` turns the paired statistics into grades.
- `coach/live.py` orchestrates: bridge → Stabilizer → snapshot → engine → publish, then action log → grade → fidelity check.
- `coach/server.py` is a FastAPI app with a WebSocket, serving the static `panel/`.
- `coach/window.py` is a pywebview always-on-top window.

The existing modules are reused: `config`, `bridge.client`, `bridge.decisions`, `bridge.versions`, `snapshot`, `runlog`, `cli`.

**Tech Stack:** Python 3.12 (coach uv env), FastAPI + uvicorn, websockets, pywebview, plain HTML/CSS/JS (no build step), pytest, httpx TestClient.

**Spec:** `docs/superpowers/specs/2026-09-23-spire-coach-design.md` §5.3–5.5, §6.3, §7.1–7.5, §9 (binding). Schemas: `docs/superpowers/specs/2026-09-24-snapshot-schemas.md`.

## Global Constraints

- Repo `~/Documents/git/spire-coach`, branch `dev`. Tests use fakes only (a fake bridge, a fake engine subprocess script, TestClient). No test touches the real game, the network or the real simulator.
- Offline:
  - the server binds `127.0.0.1` only, on port 15530 by default;
  - the engine service is a child process over pipes;
  - no outbound connections.
- Grades use ONLY the paired statistics: `paired_diff_vs_best`, `paired_diff_stderr`, `paired_n`, `paired_death_diff_vs_best`, `paired_death_diff_stderr`. The lower bound is `diff − 1.645·stderr` (90% one-sided).
  - Grade by that lower bound: ≤ 0.5 → Best (or when the move is the best move); < 2 → Good; < 5 → Inaccuracy; < 10 → Mistake; otherwise Blunder.
  - Also Blunder when the paired death-risk increase's lower bound is ≥ 0.10.
  - With `paired_n` < 20 or a missing stat, the move is "Ungraded".
  - Thresholds live in config.
- Advice is shown only when versions match (game v0.107.1, mod schema 4, engine healthy) and the conversion is reliable. Otherwise the panel shows why, and a manual override in the settings exists.
- The user never waits on the coach: analysis runs in the background, and stale results (the state changed) are dropped.
- Every warning and error reaches the panel's status line as one readable sentence. No stack traces are shown.

## Review Focus

1. **The player acts before the analysis finishes.** The stale analysis is dropped, the new position is analyzed, and the grade for the earlier move still uses the analysis of the position the move was made from, when one exists. Otherwise the move is Ungraded.
2. **The engine service crashes or hangs.** The client restarts it, and the panel shows "engine restarting". A hung request times out after `2 × budget + 5 s`.
3. **Several actions between two polls** (fast play, or an auto-play chain). Each human action is matched to the analysis of the position it was made from, in order. Actions with `source = "api"` are not graded.
4. **A position the converter marks unreliable.** The recommendation is still shown, with the warning line, and grades for moves from that position are marked "(unreliable)".
5. **The panel reconnects** after a server restart or sleep: the WebSocket auto-reconnects and the last state is re-sent on connect.

---

### Task 1: Engine client

**Files:** create `coach/engine.py`, `tests/test_engine.py` and `tests/fakes/fake_engine_service.py` (a script that speaks the protocol with canned responses).

**Interfaces:**
- Produces:
  - `EngineClient(cmd: list[str] | None = None, *, workers: int = 4, request_timeout_s: float = 10.0)`, with methods:
    - `start()`, `health() -> dict`;
    - `analyze(live: dict, *, time_budget_s=1.5, seed=0) -> Analysis`;
    - `check(pre: dict, move: Move, post: dict) -> CheckResult`;
    - `strategy(live_run: dict, decision: dict) -> StrategyResult | None` (None on `no_model`);
    - `close()`.
  - Frozen dataclasses mirroring the protocol shapes: `Move`, `MoveStat`, `Analysis`, `CheckResult`, `StrategyResult`.
  - `EngineError(Exception)` and `EngineUnavailable(EngineError)`.
- The default `cmd` is `["uv", "run", "--project", str(Path.home()/"Documents/git/sts2"), "python", "-m", "sts2_gym.live.service", "--workers", str(workers)]`.

Requirements:
1. Requests carry an increasing `id`, and responses are matched by `id`. stderr from the service goes to a rotating log file under `data/logs/engine.log`, never to the coach's stdout.
2. On EOF, crash or timeout, the client restarts the service once per request and retries idempotent calls (`health`, `analyze`, `check`, `strategy`) once. After a second failure it raises `EngineUnavailable` with a one-line reason.
3. Tests, run against the fake service script:
   - round trip for every method;
   - `ok: false` → `EngineError` carrying the service's message;
   - a fake that exits mid-request triggers a restart and retry;
   - a fake that hangs times out;
   - `no_model` → None;
   - `close()` leaves no child process.
4. Commit `feat(engine): client for the out-of-process engine service`.

### Task 2: Grader

**Files:** create `coach/grader.py` and `tests/test_grader.py`.

**Interfaces:**
- Produces:
  - `Grade(label: str, cost_lower_hp: float | None, cost_mean_hp: float | None, death_risk_delta_lower: float | None, best: Move, played: Move, unreliable: bool)`
  - `identify_move(action_entry: dict, pre_live: dict, analysis: Analysis) -> MoveStat | None`. It maps a mod action-log entry to the matching MoveStat:
    - `play_card`: card_uid → the live hand index → the MoveStat whose `hand_indices` include it AND whose target equals the entry's target `entity_id` (or no target);
    - `use_potion`: slot, plus the target;
    - `end_turn`.
  - `grade(stat: MoveStat, analysis: Analysis, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> Grade`
  - `Thresholds`, a dataclass of the Global Constraints' values.
  - `grade_selection(pre_live, post_live, analysis) -> Grade | None`: an in-combat selection is identified by diffing which candidate card left the hand or pile.

Requirements:
1. The math follows the Global Constraints exactly. The played move equal to the best move → Best.
2. Tests:
   - each grade band at its boundaries;
   - the death-risk Blunder rule;
   - `paired_n` < 20 → Ungraded;
   - a played copy of a merged identical card maps to its child;
   - a targeted card on the wrong enemy maps to that target's child, not the best target's;
   - a potion;
   - end turn;
   - an unknown action → None;
   - an unreliable analysis → `unreliable=True`.
3. Commit `feat(grader): grade moves from paired search statistics`.

### Task 3: Live loop

**Files:** create `coach/live.py` and `tests/test_live.py`.

**Interfaces:**
- Consumes: `ModClient`, `Stabilizer`, `classify`, `check_versions`, `build_live_snapshot`, `RunLog`, `EngineClient`, `grader`.
- Produces:
  - `CoachState`, a dataclass that serializes to JSON for the panel:
    - `status`: `"waiting_for_game" | "idle" | "thinking" | "advice" | "advice_off" | "engine_restarting"`;
    - `status_text`;
    - `recommendation`: `{best, line, alternatives[{move, cost_mean_hp, cost_lower_hp, p_death_delta}], expected_hp_loss, p_death, reliable, warnings}` or null;
    - `last_grade`: a Grade as a dict, or null;
    - `mode`: `"show" | "quiz"`;
    - `versions: VersionStatus`;
    - `run: {act, floor}`.
  - `CoachSession(client, engine, *, publish: Callable[[CoachState], None], data_dir, settings)`, with `step()`, which does one poll iteration and is testable, `run(stop)`, and `set_mode(mode)`.

Requirements:
1. `step()` polls, stabilizes, classifies, and then:
   - On a settled COMBAT/HAND_SELECT decision whose fingerprint differs from the last analyzed one: build the live snapshot. The room type is remembered from the last combat `state_type` for hand_select. Submit `analyze` to a single background worker thread; if a newer position arrives, the older result is discarded on completion. Publish "thinking", then "advice".
   - Every poll: fetch `actions_since(last_seq)`. For each entry with `source == "human"`, find the analysis of the position current when the action happened (keep a small map from fingerprint to (pre_live, analysis)), grade it, and publish `last_grade`. Then, once the next settled state arrives, run `engine.check(pre, move, post)` in the background. If `deterministic_mismatches` or `implausible` is non-empty, write `data/gaps/<ts>.json` with the pre and post snapshots, the move and the check result, and add a warning.
   - Log everything to the RunLog: states, analyses, grades, checks.
2. Quiz mode: the recommendation is published with `best`, `line` and `alternatives` hidden until the next grade for that position arrives.
3. Version or engine problems publish `advice_off` with the reason. `settings.override_versions` allows advice anyway, with a warning.
4. Tests:
   - a scripted fake bridge and fake engine drive a whole scenario: waiting → connected → turn start → analysis → the human plays a card → grade → check → a mismatch writes a gap file;
   - the player acts before analysis completes → stale result dropped, the grade uses the pre-position analysis;
   - two actions between polls → both graded in order;
   - api-source actions are not graded;
   - a version mismatch → advice_off;
   - quiz mode hides the best move until graded.
5. Commit `feat(live): coach session loop with grading and fidelity checks`.

### Task 4: Server, panel, floating window

**Files:** create `coach/server.py`, `coach/window.py`, `panel/index.html`, `panel/panel.css`, `panel/panel.js`, `tests/test_server.py` and `tests/test_window.py`.

**Interfaces:**
- Produces:
  - `create_app(state_provider: Callable[[], CoachState], on_settings: Callable[[dict], None]) -> FastAPI`, with routes:
    - `GET /` serves the panel;
    - `GET /state` returns the JSON;
    - `WS /ws` pushes CoachState JSON on every publish, and sends the current state on connect;
    - `POST /settings` accepts `{mode?: "show"|"quiz", override_versions?: bool}`.
  - `Broadcaster`, with a thread-safe `publish(state)` fanning out to connected WebSockets.
  - `open_window(url: str, *, width=380, height=560, on_top=True) -> None`. It uses pywebview. If pywebview fails to start, it logs one line and prints the URL, so the browser remains the fallback.

Requirements:
1. **Panel design.** Use the `frontend-design` skill for this step. The panel is compact and readable over the game. It has:
   - a status line;
   - a big "Best move" line;
   - the expected rest of the turn;
   - expected HP loss and death risk;
   - 2–3 alternatives, each with its cost in HP;
   - the last move's grade as a colored chip: Best/Good/Inaccuracy/Mistake/Blunder/Ungraded;
   - a warning line;
   - a Show/Quiz toggle;
   - a compact/expanded toggle.
   Dark by default, legible at 380 px width, no external network assets.
2. The WebSocket auto-reconnects with backoff. The last state renders instantly on reconnect.
3. The server binds `127.0.0.1` only. `POST /settings` validates its input.
4. Tests:
   - TestClient: `/state`, `/settings` validation, a WebSocket connect receives the current state, and a publish reaches connected clients;
   - `open_window` with pywebview mocked, and with a pywebview import failure (the fallback message).
5. Commit `feat(panel): floating coach panel with live updates`.

### Task 5: `spire-coach coach` command and README

**Files:** modify `coach/cli.py` and `tests/test_cli.py`; create `README.md`.

Requirements:
1. `spire-coach coach [--workers N] [--budget S] [--port P] [--no-window] [--mode show|quiz]` wires everything together:
   - load config;
   - ModClient + EngineClient (start and health-check);
   - the FastAPI server in a thread (uvicorn);
   - CoachSession in a thread;
   - the window, unless `--no-window`, on the main thread (pywebview needs the main thread on macOS).
   Ctrl-C and closing the window shut everything down cleanly: engine closed, threads joined.
2. `README.md` explains setup and the commands in order:
   1. build the simulator;
   2. `spire-coach mod install`;
   3. `spire-coach saves seed-modded --from 1 --to 1` (and `--to 3` for autopilot);
   4. `spire-coach mod enable`;
   5. launch the game;
   6. `spire-coach doctor`;
   7. `spire-coach coach`.
   It also states what the grades mean and the known limits (Act 1, Ironclad, v0.107.1).
3. Tests: argument parsing, the wiring with fakes (no window, a fake engine command), and clean shutdown.
4. Commit `feat(cli): spire-coach coach command and README`.
