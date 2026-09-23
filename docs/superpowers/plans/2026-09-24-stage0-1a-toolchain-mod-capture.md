# Stage 0–1a: Toolchain, Merged Mod, Capture Mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working toolchain and simulator baseline, the merged STS2MCP mod (with our token, enemy-move and action-log additions) installed in the game, and a coach "capture mode" that records every settled game state to disk.

**Architecture:** Three repos. `~/Documents/git/sts2` is the simulator fork; this plan only builds and tests it. `~/Documents/git/STS2MCP` is the mod fork: a merge of two community forks, plus cherry-picks and our additions. `~/Documents/git/spire-coach` holds the Python coach package: config, save tools, mod installer, bridge client, decision detection, run log, capture loop, and CLI.

**Tech Stack:** .NET SDK 9.0.317 in `~/.dotnet` (the simulator author's exact SDK), C# Godot mod, ilspycmd 8.2.0.7535, Python 3.12 via uv, httpx, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-23-spire-coach-design.md`

## Global Constraints

- Game version `v0.107.1` (game commit `59260271`). Anything else is a version mismatch.
- Single-player only. Do not add or touch multiplayer features.
- Offline at runtime. The coach talks only to `127.0.0.1:15526`.
- Decompiled game code stays local. Never commit `decompiled/` in any repo.
- Saves:
  - Never modify saves without a fresh backup made in the same command.
  - Never touch saves while the game is running.
- Hidden-information fields (true pile order, RNG state, card/potion pools, upcoming encounters) must never be used by coach or autopilot logic. Only tests and capture tools may read them.
- .NET environment for every dotnet command: `export DOTNET_ROOT="$HOME/.dotnet"; export PATH="$HOME/.dotnet:$HOME/.dotnet/tools:$HOME/.local/bin:$PATH"`. For ilspycmd, also set `DOTNET_ROLL_FORWARD=LatestMajor`.
- Python `>=3.12,<3.14` (the simulator's torch/numpy pins need 3.12), managed with uv. Tests use pytest; lint uses `uv run ruff check .`.
- Commits use conventional prefixes: `feat:`, `fix:`, `test:`, `docs:`, `chore:`.

## Review Focus

1. **The game or mod disappears mid-capture** (game closed, mod not loaded, connection refused). Capture keeps running, prints "waiting for game" once, and resumes recording when the game returns. It never crashes.
2. **Wrong or missing token.** Every command prints one clear line, e.g. "token rejected: reinstall the mod or check STS2_MCP.token". No stack trace.
3. **Saves commands while the game is running.** They refuse with a clear message, and nothing on disk changes.
4. **Game auto-updated past v0.107.1.** `doctor` and `capture` warn loudly. Capture still records, and stamps each record with the versions it saw.
5. **Intermediate states during animations or effect resolution.** Only settled states are recorded, and consecutive identical states are stored once.

---

### Task 1: Toolchain and simulator baseline

Mostly mechanical, and partly executed during planning. The executor verifies each item and does whatever is missing.

**Files:**
- Create: `spire-coach/tools/env.sh`

**Interfaces:**
- Produces: `tools/env.sh`, which every later task sources before running dotnet or uv.

- [ ] **Step 1: Write `tools/env.sh`**

```bash
# Source this before any dotnet/uv command: `source tools/env.sh`
export DOTNET_ROOT="$HOME/.dotnet"
export PATH="$HOME/.dotnet:$HOME/.dotnet/tools:$HOME/.local/bin:$PATH"
export DOTNET_ROLL_FORWARD=LatestMajor   # ilspycmd 8.2 targets an older runtime
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2"
export STS2_DATA_DIR="$STS2_GAME_DIR/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
```

- [ ] **Step 2: Verify the SDK and tools**

Run: `source tools/env.sh && dotnet --list-sdks && ilspycmd --version && uv --version`

Expected: a line `9.0.317 [/Users/you/.dotnet/sdk]`, `ilspycmd: 8.2.0.7535`, and any `uv 0.9+`.

If the SDK is missing, run `bash dotnet-install.sh --version 9.0.317 --install-dir ~/.dotnet`, using Microsoft's official `https://dot.net/v1/dotnet-install.sh`. If ilspycmd is missing, run `dotnet tool install -g ilspycmd --version 8.2.0.7535` (never 11.x; it is broken).

- [ ] **Step 3: Verify the simulator fork**

Run: `cd ~/Documents/git/sts2 && git rev-parse --short HEAD && git branch --show-current`

Expected: `37e4264` and `spire-coach`.

If the clone is missing: `git clone https://github.com/evangambit/sts2 ~/Documents/git/sts2 && cd ~/Documents/git/sts2 && git checkout -b spire-coach 37e4264`.

- [ ] **Step 4: Verify the local decompile**

Run: `cd ~/Documents/git/sts2 && cat decompiled/.version && find decompiled -name '*.cs' | wc -l && git status --short decompiled | wc -l`

Expected: a sha256 hash, about 3,400 `.cs` files, and `0` (the folder is ignored).

If it is missing: `source ~/Documents/git/spire-coach/tools/env.sh && bash scripts/decompile.sh "$STS2_DATA_DIR"`.

- [ ] **Step 5: Build the native library and run both test suites**

Run:
```bash
source ~/Documents/git/spire-coach/tools/env.sh && cd ~/Documents/git/sts2
bash scripts/build.sh osx-arm64 && nm -gU out/Sts2Emulator.dylib | grep -c Sts2
dotnet test src/Sts2Emulator.Tests/Sts2Emulator.Tests.csproj 2>&1 | tail -3
uv sync --python 3.12 && uv run python -m unittest discover -s tests/python 2>&1 | tail -3
```

Expected:
- `out/Sts2Emulator.dylib` exists and exports 70+ `Sts2` symbols.
- The dotnet test summary shows `Failed: 0` (about 3,650 tests).
- The Python run ends with `OK`, possibly with skips.

Record the exact pass/skip counts in the commit message of Step 6. Any failure here is upstream breakage: stop and report it. Do not patch the simulator in this task.

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/git/spire-coach && git add tools/env.sh
git commit -m "chore: add toolchain env script (sim baseline: <N> C# tests passed, python OK)"
```

---

### Task 2: Coach package skeleton and config

**Files:**
- Create: `spire-coach/pyproject.toml`, `spire-coach/.python-version`, `spire-coach/.gitignore`, `spire-coach/coach/__init__.py`, `spire-coach/coach/config.py`
- Test: `spire-coach/tests/test_config.py`

**Interfaces:**
- Produces:
  - `coach.config.Config`, a frozen dataclass: `base_url: str`, `token: str | None`, `data_dir: Path`, `poll_interval_s: float`, `mods_dir: Path`.
  - `coach.config.load_config(env: dict[str, str] | None = None) -> Config`
  - `coach.config.read_token(mods_dir: Path) -> str | None`
  - Constants: `DEFAULT_BASE_URL`, `EXPECTED_GAME_VERSION`, `GAME_DIR`, `APP_BUNDLE`, `MODS_DIR`, `RELEASE_INFO`, `SAVE_ROOT`, `TOKEN_FILE_NAME`, `DEFAULT_DATA_DIR`.

- [ ] **Step 1: Create the project files**

`pyproject.toml`:
```toml
[project]
name = "spire-coach"
version = "0.1.0"
description = "Offline Slay the Spire 2 coach and autopilot"
requires-python = ">=3.12,<3.14"
dependencies = ["httpx>=0.27,<1"]

[project.scripts]
spire-coach = "coach.cli:main"

[dependency-groups]
dev = ["pytest>=8.3", "ruff>=0.6"]

[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["coach"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]
```

`.python-version`:
```
3.12
```

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
data/
```

`coach/__init__.py`:
```python
"""Spire Coach: an offline Slay the Spire 2 coach and autopilot."""

__version__ = "0.1.0"
```

- [ ] **Step 2: Write the failing tests**

`tests/test_config.py`:
```python
from coach.config import DEFAULT_BASE_URL, TOKEN_FILE_NAME, load_config, read_token


def test_defaults_when_env_is_empty(tmp_path):
    cfg = load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path)})
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.token is None
    assert cfg.poll_interval_s == 0.2
    assert cfg.mods_dir == tmp_path


def test_token_is_read_from_the_mods_dir(tmp_path):
    (tmp_path / TOKEN_FILE_NAME).write_text("abc123\n", encoding="utf-8")
    assert read_token(tmp_path) == "abc123"
    assert load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path)}).token == "abc123"


def test_env_token_overrides_the_file(tmp_path):
    (tmp_path / TOKEN_FILE_NAME).write_text("from-file", encoding="utf-8")
    cfg = load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path), "SPIRE_COACH_TOKEN": "from-env"})
    assert cfg.token == "from-env"


def test_blank_token_file_means_no_token(tmp_path):
    (tmp_path / TOKEN_FILE_NAME).write_text("   \n", encoding="utf-8")
    assert read_token(tmp_path) is None


def test_trailing_slash_is_stripped_from_base_url(tmp_path):
    cfg = load_config(
        {"SPIRE_COACH_MODS_DIR": str(tmp_path), "SPIRE_COACH_BASE_URL": "http://127.0.0.1:9999/"}
    )
    assert cfg.base_url == "http://127.0.0.1:9999"


def test_poll_interval_and_data_dir_come_from_env(tmp_path):
    cfg = load_config(
        {
            "SPIRE_COACH_MODS_DIR": str(tmp_path),
            "SPIRE_COACH_POLL_S": "0.5",
            "SPIRE_COACH_DATA_DIR": str(tmp_path / "d"),
        }
    )
    assert cfg.poll_interval_s == 0.5
    assert cfg.data_dir == tmp_path / "d"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd ~/Documents/git/spire-coach && uv sync && uv run pytest tests/test_config.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'coach.config'`.

- [ ] **Step 4: Implement `coach/config.py`**

```python
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
```

- [ ] **Step 5: Run the tests and lint**

Run: `uv run pytest tests/test_config.py && uv run ruff check .`

Expected: `6 passed` and `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .python-version .gitignore coach tests uv.lock
git commit -m "feat: add coach package skeleton and config"
```

---

### Task 3: Save tools (backup, seed modded slot, restore)

**Files:**
- Create: `spire-coach/coach/saves.py`
- Test: `spire-coach/tests/test_saves.py`

**Interfaces:**
- Consumes: `coach.config.SAVE_ROOT`.
- Produces:
  - `class SaveError(Exception)`
  - `game_is_running() -> bool`
  - `find_steam_dir(save_root: Path) -> Path`
  - `backup(steam_dir: Path, backup_root: Path, *, now: datetime | None = None, is_running: Callable[[], bool] = game_is_running) -> Path`
  - `seed_modded(steam_dir: Path, backup_root: Path, *, source_profile: int, target_slot: int, now: datetime | None = None, is_running: Callable[[], bool] = game_is_running) -> SeedResult`, where `SeedResult(backup_path: Path, written: list[Path])`.
  - `restore(backup_path: Path, steam_dir: Path, *, now: datetime | None = None, is_running: Callable[[], bool] = game_is_running) -> Path`, which returns the folder the replaced saves were moved to.

**Background:** save files are plain JSON. `steam/<steamid>/profileN/saves/` holds `progress.save` (unlocks, discoveries, epochs), `prefs.save`, `current_run.save`, each with a `.backup` twin, plus `history/*.run`. Modded play uses `steam/<steamid>/modded/profileN/saves/`. Seeding copies only progress and prefs (and their `.backup` twins) into the modded slot. It never touches the slot's `current_run.save` or history: the user's modded `profile1` currently holds a run in progress.

- [ ] **Step 1: Write the failing tests**

`tests/test_saves.py`:
```python
from datetime import datetime

import pytest

from coach.saves import SaveError, backup, find_steam_dir, restore, seed_modded

NOW = datetime(2026, 9, 24, 12, 0, 0)


def never_running():
    return False


def always_running():
    return True


def make_save_tree(root):
    steam = root / "steam" / "76561198000000000"
    saves = steam / "profile1" / "saves"
    (saves / "history").mkdir(parents=True)
    (saves / "progress.save").write_text('{"unlocks": "full"}')
    (saves / "progress.save.backup").write_text('{"unlocks": "full"}')
    (saves / "prefs.save").write_text('{"fast_mode": true}')
    (saves / "current_run.save").write_text('{"run": "normal"}')
    (saves / "history" / "1.run").write_text("{}")
    modded = steam / "modded" / "profile1" / "saves"
    modded.mkdir(parents=True)
    (modded / "progress.save").write_text('{"unlocks": "none"}')
    (modded / "current_run.save").write_text('{"run": "modded-in-progress"}')
    (steam / "profile.save").write_text('{"last_profile_id": 1}')
    return steam


def test_find_steam_dir_requires_exactly_one_account(tmp_path):
    steam = make_save_tree(tmp_path)
    assert find_steam_dir(tmp_path / "steam") == steam
    (tmp_path / "steam" / "76561198000000001").mkdir()
    with pytest.raises(SaveError, match="exactly one"):
        find_steam_dir(tmp_path / "steam")


def test_backup_copies_the_whole_account_folder(tmp_path):
    steam = make_save_tree(tmp_path)
    dest = backup(steam, tmp_path / "backups", now=NOW, is_running=never_running)
    assert dest.name == "76561198000000000-20260924-120000"
    assert (dest / "profile1" / "saves" / "history" / "1.run").exists()
    assert (dest / "modded" / "profile1" / "saves" / "current_run.save").exists()


def test_backup_refuses_while_the_game_runs(tmp_path):
    steam = make_save_tree(tmp_path)
    with pytest.raises(SaveError, match="running"):
        backup(steam, tmp_path / "backups", now=NOW, is_running=always_running)
    assert not (tmp_path / "backups").exists()


def test_seed_copies_progress_and_prefs_only_after_a_backup(tmp_path):
    steam = make_save_tree(tmp_path)
    result = seed_modded(
        steam, tmp_path / "backups", source_profile=1, target_slot=1, now=NOW,
        is_running=never_running,
    )
    modded = steam / "modded" / "profile1" / "saves"
    assert (modded / "progress.save").read_text() == '{"unlocks": "full"}'
    assert (modded / "prefs.save").read_text() == '{"fast_mode": true}'
    # The modded run in progress is untouched, and nothing else is copied over.
    assert (modded / "current_run.save").read_text() == '{"run": "modded-in-progress"}'
    assert not (modded / "history").exists()
    # The backup holds the pre-seed modded state.
    saved = result.backup_path / "modded" / "profile1" / "saves" / "progress.save"
    assert saved.read_text() == '{"unlocks": "none"}'
    assert sorted(p.name for p in result.written) == [
        "prefs.save", "progress.save", "progress.save.backup",
    ]


def test_seed_creates_an_empty_target_slot(tmp_path):
    steam = make_save_tree(tmp_path)
    seed_modded(
        steam, tmp_path / "backups", source_profile=1, target_slot=3, now=NOW,
        is_running=never_running,
    )
    assert (steam / "modded" / "profile3" / "saves" / "progress.save").exists()


def test_seed_refuses_while_the_game_runs_and_changes_nothing(tmp_path):
    steam = make_save_tree(tmp_path)
    with pytest.raises(SaveError, match="running"):
        seed_modded(
            steam, tmp_path / "backups", source_profile=1, target_slot=1, now=NOW,
            is_running=always_running,
        )
    modded = steam / "modded" / "profile1" / "saves" / "progress.save"
    assert modded.read_text() == '{"unlocks": "none"}'
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("source,target", [(0, 1), (4, 1), (1, 0), (1, 4)])
def test_seed_rejects_profile_numbers_outside_1_to_3(tmp_path, source, target):
    steam = make_save_tree(tmp_path)
    with pytest.raises(SaveError, match="1, 2 or 3"):
        seed_modded(
            steam, tmp_path / "backups", source_profile=source, target_slot=target, now=NOW,
            is_running=never_running,
        )


def test_seed_fails_cleanly_when_the_source_has_no_progress(tmp_path):
    steam = make_save_tree(tmp_path)
    with pytest.raises(SaveError, match="progress.save"):
        seed_modded(
            steam, tmp_path / "backups", source_profile=2, target_slot=1, now=NOW,
            is_running=never_running,
        )
    assert not (tmp_path / "backups").exists()


def test_restore_round_trips_and_keeps_the_replaced_folder(tmp_path):
    steam = make_save_tree(tmp_path)
    dest = backup(steam, tmp_path / "backups", now=NOW, is_running=never_running)
    (steam / "profile1" / "saves" / "progress.save").write_text('{"unlocks": "corrupted"}')
    aside = restore(dest, steam, now=NOW, is_running=never_running)
    assert (steam / "profile1" / "saves" / "progress.save").read_text() == '{"unlocks": "full"}'
    assert (aside / "profile1" / "saves" / "progress.save").read_text() == '{"unlocks": "corrupted"}'


def test_restore_rejects_a_folder_that_is_not_a_backup(tmp_path):
    steam = make_save_tree(tmp_path)
    not_backup = tmp_path / "random"
    not_backup.mkdir()
    with pytest.raises(SaveError, match="not a save backup"):
        restore(not_backup, steam, now=NOW, is_running=never_running)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_saves.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'coach.saves'`.

- [ ] **Step 3: Implement `coach/saves.py`**

```python
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
    steam_dir.rename(aside)
    shutil.copytree(backup_path, steam_dir)
    return aside
```

- [ ] **Step 4: Run the tests and lint**

Run: `uv run pytest tests/test_saves.py && uv run ruff check .`

Expected: `13 passed` and `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add coach/saves.py tests/test_saves.py
git commit -m "feat: add save backup, modded-slot seeding and restore with game-running guard"
```

---

<!-- Tasks 4–12 (mod merge and additions, installer, bridge, decisions, capture, live smoke test) are appended once the mod-merge research report is verified. -->
