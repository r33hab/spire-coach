"""spire-coach: setup (saves, mod install, doctor) and capture mode.

This is the console entry point (`spire-coach`). Subcommands wire together
coach.saves, coach.modinstall and coach.capture; this module's own job is
argument parsing and turning every exception at the boundary into one clear
line on stderr, never a traceback (Review Focus #2/#3).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from coach.bridge.client import BridgeAuthError, BridgeError, ModClient
from coach.bridge.versions import check_versions, read_game_version
from coach.capture import capture_loop
from coach.config import (
    CONF_FILE_NAME,
    EXPECTED_GAME_VERSION,
    GAME_DIR,
    RELEASE_INFO,
    SAVE_ROOT,
    Config,
    load_config,
    read_token,
)
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

DEFAULT_BACKUP_ROOT = Path.home() / "Documents" / "spire-coach-backups"
DEFAULT_MOD_REPO = Path.home() / "Documents" / "git" / "STS2MCP"
MOD_ID = "STS2_MCP"


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
        checks.append(
            (False, "✗ game version: unknown (release_info.json missing or unreadable)")
        )
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
        entry = next(
            (e for e in mod_list if isinstance(e, dict) and e.get("id") == MOD_ID), None
        )
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

    client = client_factory(config.base_url, config.token)
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


# --- argument parsing ------------------------------------------------------------------


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

    return parser


def _current_operation(args: argparse.Namespace) -> str:
    return (
        getattr(args, "saves_command", None)
        or getattr(args, "mod_command", None)
        or args.command
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
    except BridgeAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (SaveError, InstallError) as exc:
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
