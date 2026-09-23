"""Paths and settings for the coach. Everything stays on this Mac."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:15526"
EXPECTED_GAME_VERSION = "v0.107.1"
GAME_DIR = Path.home() / "Library/Application Support/Steam/steamapps/common/Slay the Spire 2"
APP_BUNDLE = GAME_DIR / "SlayTheSpire2.app"
MODS_DIR = APP_BUNDLE / "Contents/MacOS/mods"
RELEASE_INFO = APP_BUNDLE / "Contents/Resources/release_info.json"
SAVE_ROOT = Path.home() / "Library/Application Support/SlayTheSpire2/steam"
TOKEN_FILE_NAME = "STS2_MCP.token"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class Config:
    base_url: str
    token: str | None
    data_dir: Path
    poll_interval_s: float
    mods_dir: Path


def read_token(mods_dir: Path) -> str | None:
    """The mod and the coach share one secret, stored next to the mod's DLL."""
    try:
        token = (mods_dir / TOKEN_FILE_NAME).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return token or None


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    mods_dir = Path(env.get("SPIRE_COACH_MODS_DIR", str(MODS_DIR)))
    return Config(
        base_url=env.get("SPIRE_COACH_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        token=env.get("SPIRE_COACH_TOKEN") or read_token(mods_dir),
        data_dir=Path(env.get("SPIRE_COACH_DATA_DIR", str(DEFAULT_DATA_DIR))),
        poll_interval_s=float(env.get("SPIRE_COACH_POLL_S", "0.2")),
        mods_dir=mods_dir,
    )
