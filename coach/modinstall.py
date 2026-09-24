"""Build, install and enable the STS2_MCP mod in the game.

Same two rules as coach.saves: never touch the game install or its settings
while the game is running, and every mutation keeps a timestamped backup of
whatever it replaces.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from coach.config import CONF_FILE_NAME, GAME_DIR
from coach.saves import _refuse_if_running, _stamp, game_is_running

DLL_NAME = "STS2_MCP.dll"
MANIFEST_NAME = "STS2_MCP.json"
DEFAULT_PORT = 15526


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class InstallResult:
    previous_backup: Path | None
    token_created: bool


def _output_tail(result: subprocess.CompletedProcess, lines: int = 20) -> str:
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return "\n".join(combined.strip().splitlines()[-lines:])


def build_mod(
    repo: Path,
    *,
    game_dir: Path = GAME_DIR,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
    """Build the mod DLL against the installed game. Raises InstallError on failure."""
    result = run(
        [
            "dotnet", "build", "STS2_MCP.csproj",
            "-c", "Release",
            "-o", "out/STS2_MCP",
            f"-p:STS2GameDir={game_dir}",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise InstallError(
            f"mod build failed (exit {result.returncode}):\n{_output_tail(result)}"
        )
    return repo / "out" / "STS2_MCP" / "STS2_MCP.dll"


def install_mod(
    dll: Path,
    manifest: Path,
    mods_dir: Path,
    *,
    is_running: Callable[[], bool] = game_is_running,
    now: datetime | None = None,
    token_factory: Callable[[], str] = lambda: secrets.token_hex(32),
) -> InstallResult:
    """Install the built DLL and manifest, backing up any previous install first.

    A previous STS2_MCP.conf is copied back after the backup so its token, and
    any other keys we don't manage, survive a reinstall.
    """
    _refuse_if_running(is_running, InstallError)

    existing = [
        name for name in (DLL_NAME, MANIFEST_NAME, CONF_FILE_NAME) if (mods_dir / name).is_file()
    ]
    previous_backup: Path | None = None
    if existing:
        previous_backup = mods_dir / ".previous" / _stamp(now)
        if previous_backup.exists():
            raise InstallError(f"backup already exists: {previous_backup}")
        previous_backup.mkdir(parents=True)
        for name in existing:
            shutil.move(str(mods_dir / name), str(previous_backup / name))
        old_conf = previous_backup / CONF_FILE_NAME
        if old_conf.is_file():
            shutil.copy2(old_conf, mods_dir / CONF_FILE_NAME)
    else:
        mods_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(dll, mods_dir / DLL_NAME)
    shutil.copy2(manifest, mods_dir / MANIFEST_NAME)

    conf_path = mods_dir / CONF_FILE_NAME
    conf = json.loads(conf_path.read_text(encoding="utf-8")) if conf_path.is_file() else {}
    existing_token = conf.get("token")
    token_created = not (isinstance(existing_token, str) and existing_token.strip())
    conf["port"] = DEFAULT_PORT
    conf["token"] = token_factory() if token_created else existing_token
    conf_path.write_text(json.dumps(conf, indent=2), encoding="utf-8")
    conf_path.chmod(0o600)

    return InstallResult(previous_backup=previous_backup, token_created=token_created)


def enable_mod_in_settings(
    settings_path: Path,
    backup_root: Path,
    *,
    mod_id: str = "STS2_MCP",
    is_running: Callable[[], bool] = game_is_running,
    now: datetime | None = None,
) -> Path:
    """Flip mods_enabled and the mod's is_enabled flag in settings.save, after a backup."""
    _refuse_if_running(is_running, InstallError)

    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / f"{settings_path.name}.{_stamp(now)}"
    if backup_path.exists():
        raise InstallError(f"backup already exists: {backup_path}")
    shutil.copy2(settings_path, backup_path)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    mod_settings = settings.setdefault("mod_settings", {})
    mod_settings["mods_enabled"] = True
    mod_list = mod_settings.setdefault("mod_list", [])
    entry = next((e for e in mod_list if e.get("id") == mod_id), None)
    if entry is not None:
        entry["is_enabled"] = True
    else:
        mod_list.append({"id": mod_id, "is_enabled": True, "source": "mods_directory"})

    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return backup_path
