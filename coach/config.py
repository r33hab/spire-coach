"""Paths and settings for the coach. Everything stays on this Mac."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:15526"
# How long the engine service's first health check waits for it to start: a cold start
# of the simulator's service (uv, imports, the search workers) can take a while.
DEFAULT_ENGINE_START_S = 90.0
EXPECTED_GAME_VERSION = "v0.107.1"
GAME_DIR = Path.home() / "Library/Application Support/Steam/steamapps/common/Slay the Spire 2"
APP_BUNDLE = GAME_DIR / "SlayTheSpire2.app"
MODS_DIR = APP_BUNDLE / "Contents/MacOS/mods"
RELEASE_INFO = APP_BUNDLE / "Contents/Resources/release_info.json"
SAVE_ROOT = Path.home() / "Library/Application Support/SlayTheSpire2/steam"
CONF_FILE_NAME = "STS2_MCP.conf"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class Config:
    base_url: str
    token: str | None
    data_dir: Path
    poll_interval_s: float
    mods_dir: Path
    engine_start_timeout_s: float = DEFAULT_ENGINE_START_S


def read_token(mods_dir: Path) -> str | None:
    """The mod and the coach share one secret, stored in the mod's conf file."""
    try:
        raw = (mods_dir / CONF_FILE_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    token = data.get("token", "")
    token = token.strip() if isinstance(token, str) else ""
    return token or None


def read_allow_debug_actions(mods_dir: Path) -> bool:
    """Whether STS2_MCP.conf has opted into the mod's `debug_*` actions (fight-lab and
    autopilot need them; both refuse to run without this, per spec)."""
    try:
        raw = (mods_dir / CONF_FILE_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and data.get("allow_debug_actions") is True


def _read_poll_interval_s(env: dict[str, str]) -> float:
    raw = env.get("SPIRE_COACH_POLL_S", "0.2")
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"invalid setting SPIRE_COACH_POLL_S={raw}: must be a number"
        ) from exc


def _read_engine_start_s(env: dict[str, str]) -> float:
    raw = env.get("SPIRE_COACH_ENGINE_START_S")
    if raw is None:
        return DEFAULT_ENGINE_START_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        raise ValueError(
            f"invalid setting SPIRE_COACH_ENGINE_START_S={raw}: "
            "must be a positive number of seconds"
        )
    return value


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    mods_dir = Path(env.get("SPIRE_COACH_MODS_DIR", str(MODS_DIR)))
    return Config(
        base_url=env.get("SPIRE_COACH_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        token=env.get("SPIRE_COACH_TOKEN") or read_token(mods_dir),
        data_dir=Path(env.get("SPIRE_COACH_DATA_DIR", str(DEFAULT_DATA_DIR))),
        poll_interval_s=_read_poll_interval_s(env),
        mods_dir=mods_dir,
        engine_start_timeout_s=_read_engine_start_s(env),
    )
