"""The dashboard: one local page that shows the whole setup at a glance.

`spire-coach dashboard` serves it on 127.0.0.1 (port 15531 by default) and opens it in the
browser. The page asks `/api/dashboard` for a fresh snapshot every few seconds. A snapshot
comes from four places:

- the training job's folder, `runs/stage2`, written by the sts2 training loop: its
  `status.json`, `config.json` and `loop.pid`, each generation's `selfplay.json`,
  `eval.json` and `promotion.json`, the champion's evals, and the tail of `loop.log`;
- the mod's `hello` and profiles, which say whether the game is running with the mod
  loaded and which save profile is current;
- the coach's panel server, when the coach is running (`GET /state` on its port);
- the exported strategy model, `data/models/strategy/model.json`, once there is one.

It only reads: it never writes to the training folder, never sends the game an action and
never changes the coach's settings.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from coach.bridge.client import BridgeError, ModClient
from coach.server import DEFAULT_PORT as PANEL_PORT
from coach.server import PANEL_DIR, _OnlyAddressedToThePanel

DEFAULT_DASHBOARD_PORT = 15531
TRAINING_ROOT_ENV = "SPIRE_COACH_TRAINING_ROOT"
# Where the training loop keeps its job: the training worktree while stage 2 is built
# there, the sts2 checkout once it is merged.
TRAINING_ROOTS = (
    Path.home() / "Documents/git/sts2-wt/training/runs/stage2",
    Path.home() / "Documents/git/sts2/runs/stage2",
)
# Job folders synced here from another machine (sts2 scripts/night_train.sh and
# desktop_fight_job.sh): their loop.pid names a process over there, so "running" means
# their files arrived within REMOTE_FRESH_S instead (they sync every 5-30 minutes).
SYNCED_ROOTS = (
    Path.home() / "Documents/git/sts2-wt/night",
    Path.home() / "Documents/git/sts2-wt/desktop",
)
REMOTE_FRESH_S = 40 * 60
PROBE_TIMEOUT_S = 0.8  # the game and the coach answer locally, well within this
_LOG_LINES = 40
_LOG_TAIL_BYTES = 64 * 1024
_SUMMARY_KEYS = (
    "n",
    "mean_floor",
    "ci95",
    "boss_kill_rate",
    "wilson95",
    "win_rate",
    "win_wilson95",
    "mean_value",
)
_SELFPLAY_KEYS = (
    "played_by",
    "runs",
    "boss_kills",
    "boss_kill_rate",
    "wins",
    "win_rate",
    "reached_act",
    "mean_floor",
    "truncated",
    "quarantined",
    "retried",
    "worker_restarts",
    "runs_per_hour",
    "seconds",
)


def training_root(env: dict[str, str] | None = None) -> Path:
    """The training job's folder: $SPIRE_COACH_TRAINING_ROOT, else the first of
    TRAINING_ROOTS that exists (the first one when none does)."""
    env = dict(os.environ) if env is None else env
    raw = env.get(TRAINING_ROOT_ENV)
    if raw:
        return Path(raw).expanduser()
    return next((root for root in TRAINING_ROOTS if root.exists()), TRAINING_ROOTS[0])


def collect_training(root: Path) -> dict[str, Any]:
    """A snapshot of the training job in `root`. `found` is False until the loop has
    written its first `status.json`."""
    status = _read_json(root / "status.json")
    if not isinstance(status, dict):
        return {"found": False, "root": str(root)}
    config = _read_json(root / "config.json")
    loop_config = config.get("loop", {}) if isinstance(config, dict) else {}
    synced_at = None
    if any(root.resolve().is_relative_to(parent.resolve()) for parent in SYNCED_ROOTS):
        pid, synced_at = None, (root / "status.json").stat().st_mtime
        alive = time.time() - synced_at < REMOTE_FRESH_S
    else:
        pid, alive = _loop_process(root / "loop.pid")
    generations = [_generation(path) for path in sorted(root.glob("gen_[0-9][0-9][0-9][0-9]"))]
    return {
        "found": True,
        "root": str(root),
        "running": alive,
        "pid": pid,
        "synced_at": synced_at,
        "status": status,
        "runs_per_generation": loop_config.get("runs_per_generation"),
        "baseline": _baseline(root, status),
        "generations": generations,
        "log": _tail(root / "loop.log", _LOG_LINES),
    }


def game_status(client: ModClient) -> dict[str, Any]:
    """Whether the game is running with the mod loaded, and which profile is current."""
    try:
        hello = client.hello()
    except BridgeError as exc:
        return {"running": False, "detail": str(exc)}
    game: dict[str, Any] = {
        "running": True,
        "mod_version": hello.get("version"),
        "mod_commit": hello.get("commit"),
        "profile": None,
    }
    try:
        game["profile"] = client.profiles().get("current_profile_id")
    except BridgeError:
        pass  # an older mod without profiles still counts as running
    return game


def coach_status(port: int = PANEL_PORT) -> dict[str, Any]:
    """The coach panel's current state when the coach is running on `port`."""
    panel_url = f"http://127.0.0.1:{port}/"
    try:
        with urllib.request.urlopen(f"{panel_url}state", timeout=PROBE_TIMEOUT_S) as response:
            state = json.load(response)
    except (OSError, ValueError):  # URLError and timeouts are OSErrors
        return {"running": False, "panel_url": panel_url}
    return {"running": True, "panel_url": panel_url, "state": state}


def model_info(path: Path) -> dict[str, Any] | None:
    """The exported strategy model's `model.json`: its scalar fields and eval summary."""
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return None
    info = {key: value for key, value in doc.items() if not isinstance(value, dict | list)}
    info["eval"] = _pick(doc.get("eval"), _SUMMARY_KEYS)
    info["path"] = str(path)
    return info


_TREND_ACT_KEYS = ("replayed", "comparable", "matched", "mismatched", "not_comparable")
DEFAULT_TREND_LIMIT = 30  # scoreboard runs kept in the trend; the chart reads oldest to newest


def collect_fidelity(
    fidelity_dir: Path, *, trend_limit: int = DEFAULT_TREND_LIMIT
) -> dict[str, Any]:
    """The fidelity scoreboard's snapshot (`coach.fidelity_scoreboard`, roadmap task A1):
    the latest run in full (`data/fidelity/latest.json`), and a trend across the runs
    before it -- a few numbers per act, not each one's full category and reason detail,
    which stays in the files themselves for anyone who wants it."""
    latest = _read_json(fidelity_dir / "latest.json")
    if not isinstance(latest, dict):
        return {"found": False, "root": str(fidelity_dir)}
    runs = sorted(p for p in fidelity_dir.glob("*.json") if p.name != "latest.json")
    trend = [point for point in (_trend_point(path) for path in runs[-trend_limit:]) if point]
    return {"found": True, "root": str(fidelity_dir), "latest": latest, "trend": trend}


def _trend_point(path: Path) -> dict[str, Any] | None:
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return None
    totals = doc.get("totals")
    acts = (
        {
            act: _pick(bucket.get("totals"), _TREND_ACT_KEYS)
            for act, bucket in totals.items()
            if isinstance(bucket, dict)
        }
        if isinstance(totals, dict)
        else {}
    )
    sts2 = doc.get("sts2") if isinstance(doc.get("sts2"), dict) else {}
    return {
        "generated_at": doc.get("generated_at"),
        "commit": sts2.get("commit"),
        "acts": acts,
    }


def snapshot_source(
    root: Path,
    client: ModClient,
    model_path: Path,
    *,
    panel_port: int = PANEL_PORT,
    fidelity_dir: Path | None = None,
) -> Callable[[], dict[str, Any]]:
    """What `/api/dashboard` answers: a fresh snapshot of everything on each call."""

    def snapshot() -> dict[str, Any]:
        return {
            "now": time.time(),
            "training": collect_training(root),
            "game": game_status(client),
            "coach": coach_status(panel_port),
            "model": model_info(model_path),
            "fidelity": (
                collect_fidelity(fidelity_dir)
                if fidelity_dir is not None
                else {"found": False, "root": None}
            ),
        }

    return snapshot


def create_app(snapshot: Callable[[], dict[str, Any]]) -> FastAPI:
    """The dashboard's app: the page at `/`, the snapshot at `/api/dashboard`. Like the
    panel, it answers only requests addressed to 127.0.0.1 or localhost on its port."""
    app = FastAPI(title="Spire Coach dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_OnlyAddressedToThePanel)

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(PANEL_DIR / "dashboard.html")

    @app.get("/api/dashboard")
    def api() -> JSONResponse:  # a plain def: FastAPI runs the file and socket reads in a thread
        return JSONResponse(json_safe(snapshot()), headers={"Cache-Control": "no-store"})

    return app


def json_safe(value: Any) -> Any:
    """`value` with every NaN or infinity replaced by None: strict JSON has neither."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    return value


# --- helpers -------------------------------------------------------------------------


def _generation(path: Path) -> dict[str, Any]:
    selfplay = _read_json(path / "selfplay.json")
    promotion = _read_json(path / "promotion.json")
    comparison = promotion.get("comparison") if isinstance(promotion, dict) else None
    return {
        "generation": int(path.name.removeprefix("gen_")),
        "selfplay": _pick(selfplay, _SELFPLAY_KEYS),
        "eval": _summary(_read_json(path / "eval.json")),
        "result": _result(promotion),
        # The champion it was compared with, on the seeds both completed (5.6).
        "champion_then": _pick(
            comparison.get("champion") if isinstance(comparison, dict) else None,
            _SUMMARY_KEYS,
        ),
        "retries": _count_lines(path / "retries.jsonl"),
    }


def _baseline(root: Path, status: dict[str, Any]) -> dict[str, Any] | None:
    """The rules policy's eval, the line every learner is measured against: the champion's
    while the rules still hold the title, then the eval a promotion kept of them."""
    champion = status.get("champion")
    if isinstance(champion, dict) and champion.get("generation") is None:
        found = _pick(champion.get("eval"), _SUMMARY_KEYS)
        if found:
            return found
    for candidate in (root / "champion" / "eval.prev.json", root / "champion" / "eval.json"):
        doc = _read_json(candidate)
        if isinstance(doc, dict) and (doc.get("policy") or {}).get("kind") == "rules":
            return _summary(doc)
    promotion = _read_json(root / "gen_0000" / "promotion.json")
    if isinstance(promotion, dict):
        return _pick((promotion.get("comparison") or {}).get("champion"), _SUMMARY_KEYS)
    return None


def _result(promotion: Any) -> str | None:
    if not isinstance(promotion, dict):
        return None
    if promotion.get("rolled_back"):
        return "rolled back"
    return "promoted" if promotion.get("promoted") else "not promoted"


def _summary(doc: Any) -> dict[str, Any] | None:
    """An eval file's summary (its `summary` block), or None."""
    if not isinstance(doc, dict):
        return None
    return _pick(doc.get("summary"), _SUMMARY_KEYS)


def _pick(doc: Any, keys: tuple[str, ...]) -> dict[str, Any] | None:
    """The `keys` that `doc` has: an Act-1 job has no win fields, a full-run job does."""
    if not isinstance(doc, dict):
        return None
    return {key: doc[key] for key in keys if key in doc}


def _read_json(path: Path) -> Any:
    """The file's JSON, or None when it is missing or unreadable. The loop writes its
    files whole (atomically), so a half-written file is not expected; a broken one is
    shown as missing rather than breaking the page."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _loop_process(pid_file: Path) -> tuple[int | None, bool]:
    """The loop's pid from `loop.pid`, and whether that process is alive."""
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None, False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return pid, False
    except PermissionError:
        return pid, True  # it exists, under another user
    return pid, True


def _tail(path: Path, lines: int) -> list[str]:
    try:
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            file.seek(max(0, file.tell() - _LOG_TAIL_BYTES))
            text = file.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as file:
            return sum(1 for line in file if line.strip())
    except OSError:
        return 0
