"""Back up, seed and restore Slay the Spire 2 save profiles.

Two rules: never touch saves while the game runs, and always back up first.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SEED_FILES = ("progress.save", "progress.save.backup", "prefs.save", "prefs.save.backup")
VALID_PROFILES = (1, 2, 3)


class SaveError(Exception):
    pass


@dataclass(frozen=True)
class SeedResult:
    backup_path: Path
    written: list[Path]


def game_is_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "SlayTheSpire2.app/Contents/MacOS"], capture_output=True, text=True
    )
    return result.returncode == 0


def _refuse_if_running(is_running: Callable[[], bool]) -> None:
    if is_running():
        raise SaveError("Slay the Spire 2 is running; close the game first")


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def find_steam_dir(save_root: Path) -> Path:
    accounts = [p for p in save_root.iterdir() if p.is_dir() and p.name.isdigit()]
    if len(accounts) != 1:
        raise SaveError(
            f"expected exactly one Steam account folder in {save_root}, found {len(accounts)}"
        )
    return accounts[0]


def backup(
    steam_dir: Path,
    backup_root: Path,
    *,
    now: datetime | None = None,
    is_running: Callable[[], bool] = game_is_running,
) -> Path:
    _refuse_if_running(is_running)
    dest = backup_root / f"{steam_dir.name}-{_stamp(now)}"
    if dest.exists():
        raise SaveError(f"backup already exists: {dest}")
    backup_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(steam_dir, dest)
    return dest


def seed_modded(
    steam_dir: Path,
    backup_root: Path,
    *,
    source_profile: int,
    target_slot: int,
    now: datetime | None = None,
    is_running: Callable[[], bool] = game_is_running,
) -> SeedResult:
    """Copy a normal profile's progress and prefs into a modded slot, after a full backup."""
    if source_profile not in VALID_PROFILES or target_slot not in VALID_PROFILES:
        raise SaveError("profile and slot numbers must be 1, 2 or 3")
    _refuse_if_running(is_running)
    src = steam_dir / f"profile{source_profile}" / "saves"
    if not (src / "progress.save").is_file():
        raise SaveError(f"no progress.save in {src}")
    backup_path = backup(steam_dir, backup_root, now=now, is_running=is_running)
    dst = steam_dir / "modded" / f"profile{target_slot}" / "saves"
    dst.mkdir(parents=True, exist_ok=True)
    written = []
    for name in SEED_FILES:
        if (src / name).is_file():
            shutil.copy2(src / name, dst / name)
            written.append(dst / name)
    return SeedResult(backup_path=backup_path, written=written)


def restore(
    backup_path: Path,
    steam_dir: Path,
    *,
    now: datetime | None = None,
    is_running: Callable[[], bool] = game_is_running,
) -> Path:
    """Put a backup back. The replaced folder is moved aside, never deleted."""
    _refuse_if_running(is_running)
    if not any(backup_path.glob("profile*")):
        raise SaveError(f"{backup_path} is not a save backup")
    aside = steam_dir.with_name(f"{steam_dir.name}.replaced-{_stamp(now)}")
    if aside.exists():
        raise SaveError(
            f"restore target already exists: {aside}; wait a second and retry"
        )
    steam_dir.rename(aside)
    shutil.copytree(backup_path, steam_dir)
    return aside
