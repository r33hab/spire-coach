"""Check that the running game, mod schema and mod build are what we expect.

The coach only ever talks to one pinned game version and one mod schema. Any
drift (an auto-update, a stale mod install, a missing field) must be reported
so `doctor` and `capture` can warn loudly instead of acting on data they
don't understand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from coach.config import EXPECTED_GAME_VERSION, RELEASE_INFO

EXPECTED_MOD_SCHEMA = 4
_EXPECTED_MOD_VERSION_PREFIX = "0.4"


@dataclass(frozen=True)
class VersionStatus:
    ok: bool
    problems: list[str]


def read_game_version(release_info: Path = RELEASE_INFO) -> str | None:
    """The installed game's version string, or None if it can't be read."""
    try:
        data = json.loads(release_info.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else None


def check_versions(game_version: str | None, payload: dict) -> VersionStatus:
    """Flag a mismatched game version, mod schema, mod build, or a missing field."""
    problems: list[str] = []

    if game_version is None:
        problems.append("game version unknown: release_info.json is missing or unreadable")
    elif game_version != EXPECTED_GAME_VERSION:
        problems.append(
            f"game version {game_version} does not match expected {EXPECTED_GAME_VERSION}"
        )

    if "schema_version" not in payload:
        problems.append("schema_version is missing from the mod payload")
    else:
        schema_version = payload["schema_version"]
        if schema_version != EXPECTED_MOD_SCHEMA:
            problems.append(
                f"schema_version {schema_version} does not match expected {EXPECTED_MOD_SCHEMA}"
            )

    if "mod_version" not in payload:
        problems.append("mod_version is missing from the mod payload")
    else:
        mod_version = payload["mod_version"]
        prefix = _EXPECTED_MOD_VERSION_PREFIX
        if not (isinstance(mod_version, str) and mod_version.startswith(prefix)):
            problems.append(
                f"mod_version {mod_version!r} does not start with {_EXPECTED_MOD_VERSION_PREFIX}"
            )

    return VersionStatus(ok=not problems, problems=problems)
