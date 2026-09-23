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

## Mod tasks (4–6): the STS2MCP fork at `~/Documents/git/STS2MCP`

Shared facts for Tasks 4–6. They were verified against the v0.107.1 game DLL in `.superpowers/sdd/research/mod-merge-report.md`, which every mod implementer must read; the sections are cited below.
- A merged, building tree exists in the scratch clone `/private/tmp/claude-501/-Users-r33hab/547e675b-0c52-416b-8614-38e7cf7ee834/scratchpad/mod-merge/STS2MCP`, on branch `spire-coach` at `2df6073`:
  - upstream `55e0648`;
  - plus evangambit `608fd7a`, merged in `3614a8f`;
  - plus cat12079801 `9853cba`, merged in `82f4f6b` with 8 textual and 5 semantic conflicts resolved (report §2);
  - plus the cherry-pick timothy `409fc77`, as `2df6073`.
- The build command, run from the repo root with the .NET env sourced:
  `dotnet build STS2_MCP.csproj -c Release -o out/STS2_MCP -p:STS2GameDir="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2"`.
  The pass criterion is `0 Warning(s), 0 Error(s)`.
- The mod has no unit-test project. Verification means a clean build, a review, and Task 12's live smoke test. Every new HTTP field or endpoint must be documented in `docs/raw-full.md` in the same commit.
- Never push. Never modify the game install in Tasks 4–6: installation happens in Task 7, via the coach, and live in Task 12.

### Task 4: Integrate the merge; token, origin and CORS lockdown; hidden-state and debug gates

**Files:** `~/Documents/git/STS2MCP`: git remotes and branch, `McpMod.cs`, `McpMod.Actions.cs`, `McpMod.StateBuilder.cs`, `McpMod.Helpers.cs` if needed, `.gitignore`, `docs/raw-full.md`, `README.md` (the auth section).

**Interfaces:**
- Produces:
  - `STS2_MCP.conf` (JSON, next to the DLL) keys: `port` (int, default 15526), `token` (string; if absent the mod generates 64 hex chars and writes it back), `allow_hidden_state` (bool, default false), `allow_debug_actions` (bool, default false).
  - The env var `STS2_MCP_TOKEN` overrides the conf token.
  - Every request must carry the header `X-STS2-Token: <token>` or `Authorization: Bearer <token>`.

Requirements:
1. In `~/Documents/git/STS2MCP`:
   - `git remote add scratch <scratch clone path>`, `git fetch scratch`;
   - add remotes `evangambit` and `cat` (https URLs from the report §1) and fetch them;
   - `git checkout -b spire-coach scratch/spire-coach`, and confirm HEAD is `2df6073`;
   - set every remote's push URL to `DISABLED_NO_PUSH` (`git remote set-url --push <r> DISABLED_NO_PUSH`);
   - make sure `out/` and `obj/` are gitignored.
2. Token check exactly as report §6(1): constant-time compare; Origin header present → 403 "Browser origins are not permitted"; missing or invalid token → 401 "Missing or invalid token". Remove the three `Access-Control-Allow-*` headers. POST bodies must be `application/json` (415 otherwise). The token must be set before `_listener.Start()`.
3. Hidden state:
   - `draw_pile_ordered`, the true draw order (StateBuilder.cs:1652–1665), is emitted only when `allow_hidden_state` is true **and** the GET query has `hidden=1`.
   - `hand_ordered`, `discard_pile_ordered` and `exhaust_pile_ordered` stay, because their order is visible to the player.
   - Audit the merged tree for any other field revealing RNG, pools or future encounters, gate each the same way, and list them in the commit message.
4. Debug and staging actions (evangambit's `McpMod.Debug.cs`: `debug_add_card`, `debug_set_energy`, `debug_add_power`, `debug_set_stars`, `debug_enchant_card`, `debug_start_event`, `debug_start_shop` and their siblings) mutate game state. When `allow_debug_actions` is false they return 403 `{"status":"error","error":"debug actions disabled"}`.
5. Build: 0 warnings, 0 errors.
6. Commit `feat(security): token auth, no browser origins, gated hidden state and debug actions`.

### Task 5: State fields the coach's snapshot needs; schema version 4

**Files:** `McpMod.StateBuilder.cs` (and a new partial `McpMod.StateExtras.cs` if StateBuilder grows past its current structure), `McpMod.cs` (the `StateSchemaVersion` constant), `docs/raw-full.md`.

**Interfaces:**
- Produces new JSON fields, which `coach/snapshot.py` (Task 11) consumes:
  - Pile cards (`draw_pile[]`, `discard_pile[]`, `exhaust_pile[]`): add `id` (`card.Id.Entry`), `is_upgraded`, `upgrade_level`. `*_ordered[]` entries: add `uid`.
  - Every card object (hand, piles, deck): `enchantment: {id, amount} | null` and `affliction: string | null` (report §6 follow-ups), plus `fields: {name: value}`. `fields` holds the card's runtime cost overrides (cost-this-turn, cost-for-combat and similar), found by reading the decompiled `CardModel`/energy-cost types. Only primitive values, only when they differ from a fresh copy.
  - Every enemy:
    - `move_id` (`creature.Monster?.NextMove?.StateId`), `move_follow_up_id`, `is_stunned` (`creature.IsStunned`);
    - `state_log` (`monster.MoveStateMachine.StateLog.Select(s => s.Id)`);
    - `slot`, the creature's position index among the combat's enemy slots, taken from the game's own slot/position data;
    - `ai_fields`: declared `int`/`bool`/`enum`/`string` instance fields and properties of the concrete monster class, walking up to but excluding `MonsterModel`, and excluding any `Rng`-typed member;
    - per attack intent: `damage` (displayed, per hit, as the game computes it for the player), `base_damage` (unmodified per hit; find the correct API on the decompiled `AttackIntent`) and `hits`.
  - Every relic: `status`, `is_used_up`, `stack_count`, `saved` (report §6(4) `BuildSavedProps`), and `fields` (declared `int`/`bool` instance fields up to `RelicModel`).
  - `battle.history`, from `CombatManager.Instance.History`: `cards_played_this_turn`, `attacks_played_this_turn`, `skills_played_this_turn`, `powers_played_this_turn`, `cards_played_this_combat`.
  - `schema_version`: 4.

Requirements:
1. Every new read sits in a try/catch that routes the exception to the existing `Warn(...)` warnings mechanism. A failing field must never drop the whole payload.
2. **Never export** `RunRng.MonsterAi` state, RNG seeds or counters, or the `_initialState` of the AI graph beyond what `state_log`/`move_id` show. Report §6(2) explains why.
3. `docs/raw-full.md`: document every new field and set the schema to 4.
4. Build: 0 warnings, 0 errors.
5. Commit `feat(state): move ids, AI state, card/relic/pile details and history for the coach (schema 4)`.

### Task 6: Player action log; character-select race fix; time scale

**Files:** create `McpMod.ActionLog.cs`; modify `McpMod.cs` (routing, `EnsureActionHooks` call in `ProcessMainThreadQueue`), `McpMod.Actions.cs` (`_apiDepth` wrap, `set_time_scale`, character-select port), `McpMod.StateBuilder.cs` (`action_log_seq`, `time_scale`), `docs/raw-full.md`.

**Interfaces:**
- Produces:
  - `GET /api/v1/actions?since=<seq>` returns `{"status":"ok","latest_seq":N,"entries":[...]}`. Each entry is `{seq, kind: "play_card"|"use_potion"|"end_turn", source: "human"|"api", utc, act, floor, round, card_id?, card_uid?, target?, target_combat_id?, slot?}`, held in a ring buffer capped at 2000.
  - `action_log_seq` in every state payload.
  - The action `{"action":"set_time_scale","scale":<float 0.5–10>}` and the state field `time_scale`.

Requirements:
1. Implement report §6(3):
   - hook through `ActionQueueSet.ActionEnqueued`, re-attached whenever the queue-set reference changes, and `CombatManager.PlayerEndedTurn`;
   - source tagging through `_apiDepth` around `ExecuteAction`'s dispatch;
   - thread safety under `_logLock`;
   - log only for the local player.
2. Port cicero `7e4977a` by hand (report §3): `IsCharacterSelectionBusy()`, Deselect-before-Select, the `selection_busy` and `selected_character` state fields. Do **not** take its manifest bump.
3. `set_time_scale` sets `Godot.Engine.TimeScale` through `RunOnMainThreadBlocking` + `EnsureStatus`, clamped to 0.5–10. The state reports the current value.
4. Build: 0 warnings, 0 errors.
5. Commit `feat(actions): player action log with source tags, character-select race fix, time scale`.

---

## Coach tasks (7–11): the Python package in `~/Documents/git/spire-coach`

Shared facts: the mod JSON field reference is report §5c plus Tasks 5–6 above. Tests use hand-written payload fixtures under `tests/fixtures/mod/`, built from those field lists. Mocked HTTP uses `httpx.MockTransport`, and no test talks to a real game.

### Task 7: Mod installer, settings enabler, token from the conf

**Files:** create `coach/modinstall.py` and `tests/test_modinstall.py`; modify `coach/config.py` and `tests/test_config.py`.

**Interfaces:**
- Produces:
  - `build_mod(repo: Path, *, game_dir: Path = GAME_DIR, run=subprocess.run) -> Path` builds the DLL and returns `repo/out/STS2_MCP/STS2_MCP.dll`. It raises `InstallError` with the build output tail on failure.
  - `install_mod(dll: Path, manifest: Path, mods_dir: Path, *, is_running=game_is_running, now=None, token_factory=lambda: secrets.token_hex(32)) -> InstallResult(previous_backup: Path | None, token_created: bool)`.
  - `enable_mod_in_settings(settings_path: Path, backup_root: Path, *, mod_id="STS2_MCP", is_running=game_is_running, now=None) -> Path` returns the backup path.
  - `class InstallError(Exception)`.
  - `coach.config.read_token(mods_dir)` now reads `"token"` from `mods_dir/STS2_MCP.conf` (JSON) and returns None when the file or key is missing or blank. `TOKEN_FILE_NAME` is replaced by `CONF_FILE_NAME = "STS2_MCP.conf"`.

Requirements:
1. `install_mod`:
   - refuses while the game runs;
   - moves any existing `STS2_MCP.dll`, `STS2_MCP.json` and `STS2_MCP.conf` into `mods_dir/.previous/<stamp>/`, keeping the conf's token by copying the conf back;
   - copies the DLL to `STS2_MCP.dll` and the manifest to `STS2_MCP.json`;
   - ensures `STS2_MCP.conf` has `port` 15526 and a `token`, keeping an existing token and generating one otherwise;
   - writes the conf with mode `0600`.
2. `enable_mod_in_settings`:
   - refuses while the game runs;
   - copies `settings.save` to `backup_root/settings.save.<stamp>`;
   - sets `mod_settings.mods_enabled = true` and the `STS2_MCP` entry's `is_enabled = true` (appending `{"id": "STS2_MCP", "is_enabled": true, "source": "mods_directory"}` if absent);
   - preserves every other key and the file's JSON formatting (2-space indent).
3. Tests with temp dirs:
   - a fresh install creates the conf with a 64-hex token;
   - a reinstall keeps the token and backs up the old files;
   - both functions refuse while the game runs, with nothing changed;
   - the settings flip preserves other keys and adds the entry when missing;
   - `build_mod` failure raises `InstallError` (fake `run`);
   - `read_token` covers missing file, missing key, blank and valid tokens.
   Update the Task 2 config tests to the conf format.
4. Commit `feat(install): mod installer, settings enabler and conf-based token`.

### Task 8: Bridge client

**Files:** create `coach/bridge/__init__.py`, `coach/bridge/client.py` and `tests/test_bridge_client.py`.

**Interfaces:**
- Produces:
  - `ModClient(base_url: str, token: str | None, *, timeout_s: float = 2.0, transport: httpx.BaseTransport | None = None)`, with methods:
    - `hello() -> dict`
    - `get_state() -> dict`, calling `GET /api/v1/singleplayer`. It never sends `hidden=1`: there is no parameter for it.
    - `post_action(action: dict, *, wait: bool = True, wait_timeout_ms: int = 10000) -> dict`
    - `actions_since(seq: int) -> dict`
    - `compendium() -> dict`
    - `close()`
  - Errors: `BridgeError(Exception)`, `BridgeUnavailable(BridgeError)` for connection refused or timeout, `BridgeAuthError(BridgeError)` for 401/403 with the server message. A 503 with `retry: true` becomes `BridgeUnavailable`.

Requirements:
1. Every request sends `X-STS2-Token` when a token is set. POSTs send JSON (`Content-Type: application/json`).
2. Error messages are one line, human-readable and name the remedy. A 401 says "token rejected: reinstall the mod (spire-coach mod install) or check STS2_MCP.conf".
3. Tests (MockTransport):
   - the header is present;
   - no `hidden` param ever appears in any request URL;
   - 401 → `BridgeAuthError` with the remedy text;
   - a connect error → `BridgeUnavailable`;
   - 503 retry → `BridgeUnavailable`;
   - non-JSON → `BridgeError`;
   - `post_action` puts `wait` and `wait_timeout_ms` in the body;
   - `actions_since` passes `since`.
4. Commit `feat(bridge): mod HTTP client with auth and clear errors`.

### Task 9: Decision detection, settling, versions

**Files:** create `coach/bridge/decisions.py`, `coach/bridge/versions.py`, `tests/test_decisions.py`, `tests/test_versions.py` and the fixtures under `tests/fixtures/mod/`.

**Interfaces:**
- Produces:
  - `DecisionKind`, an Enum: `COMBAT`, `HAND_SELECT`, `NON_COMBAT`, `NONE`.
  - `Decision(kind, state_type, room_type)`, a frozen dataclass.
  - `classify(state: dict) -> Decision`.
  - `is_settled(state) -> bool`.
  - `fingerprint(state) -> str`.
  - `class Stabilizer`, with `feed(state) -> dict | None`. It returns the state once the same settled fingerprint has been seen on 2 consecutive polls.
  - `read_game_version(release_info: Path = RELEASE_INFO) -> str | None`.
  - `EXPECTED_MOD_SCHEMA = 4`.
  - `check_versions(game_version: str | None, payload: dict) -> VersionStatus(ok: bool, problems: list[str])`.

Requirements:
1. Settled means `is_resolving` is false; and in combat (`state_type` in monster/elite/boss) also `battle.is_play_phase` is true and `battle.turn == "player"`. `hand_select` counts as settled when `is_resolving` is false.
2. `classify`:
   - combat states → COMBAT;
   - `hand_select` → HAND_SELECT;
   - rewards, card_reward, map, shop, rest_site, event, treasure, card_select and relic_select → NON_COMBAT;
   - anything else, or an unsettled state → NONE.
   `room_type` is the combat `state_type`, or None.
3. `fingerprint`: a sha1 of canonical JSON over `state_type`, `battle`, `player` and `run`, ignoring `warnings`, `mod_commit`, `action_log_seq` and `time_scale`.
4. `check_versions` flags each of these:
   - game version ≠ `v0.107.1`;
   - `schema_version` ≠ 4;
   - `mod_version` not starting with `0.4`;
   - a missing field.
   Each problem is a one-line message.
5. Tests: every classification branch; the settle rules including enemy turn and mid-resolution; the Stabilizer needs two identical settled polls and resets on change; versions ok or mismatched per field.
6. Commit `feat(bridge): decision detection, settling and version checks`.

### Task 10: Run log, capture loop, CLI

**Files:** create `coach/runlog.py`, `coach/capture.py`, `coach/cli.py`, `tests/test_runlog.py`, `tests/test_capture.py` and `tests/test_cli.py`.

**Interfaces:**
- Consumes: Tasks 3, 7, 8 and 9.
- Produces:
  - `RunLog(data_dir: Path, *, now=None)` writes to `data/captures/<stamp>.jsonl`, with `append(kind: str, payload: dict) -> None`. Each line is `{ts, kind, payload}`, flushed on every write.
  - `capture_loop(client, log, *, poll_interval_s, stop: Callable[[], bool], sleep=time.sleep, out=print) -> CaptureStats(records, actions, errors)`.
  - `main(argv: list[str] | None = None) -> int`, the console script with these subcommands:
    - `doctor`
    - `capture`
    - `saves backup`
    - `saves seed-modded --from N --to M`
    - `saves restore PATH`
    - `mod install [--repo PATH]`
    - `mod enable`

Requirements:
1. `capture_loop`:
   - polls `get_state`;
   - passes each state through a `Stabilizer`;
   - logs each stabilized state as a `"state"` record with its `Decision` and `check_versions` result;
   - polls `actions_since` and logs each new entry as an `"action"` record;
   - on `BridgeUnavailable`, prints "waiting for game…" once, sleeps 1 s and retries, then prints "game connected" on recovery;
   - on `BridgeAuthError`, prints the message and returns with `errors` counted, and the CLI exits with code 2.
2. `doctor` prints one line per check, each ✓/✗:
   - game dir exists;
   - game version;
   - mod files are installed (DLL, JSON, conf);
   - token present;
   - the mod is enabled in `settings.save`;
   - whether the game is running;
   - `hello` reachable;
   - auth accepted;
   - versions ok.
   It exits 0 only when all pass.
3. The `saves` and `mod` subcommands call Tasks 3 and 7's functions and print what they did, including backup paths.
4. Tests:
   - the capture loop over a scripted fake client (resolving → settled twice → unavailable → back → an auth error) produces exactly the expected records and messages;
   - the RunLog writes valid JSONL;
   - the CLI parses every subcommand and calls the right function (monkeypatched);
   - `doctor` output shows ✗ lines when checks fail.
5. Commit `feat(capture): run log, capture loop and spire-coach CLI`.

### Task 11: Live snapshot builder (mod JSON → `sts2-live-snapshot/1`)

**Files:** create `coach/snapshot.py`, `tests/test_snapshot.py` and fixtures.

**Interfaces:**
- Consumes: the mod fields from report §5c plus Tasks 5–6, and schema `docs/superpowers/specs/2026-09-24-snapshot-schemas.md` §1 (binding).
- Produces:
  - `build_live_snapshot(state: dict, *, game_version: str, room_type: str | None = None, known_top: list[dict] | None = None, known_bottom: list[dict] | None = None) -> dict`
  - `class SnapshotError(Exception)`, carrying a JSON path.

Requirements:
1. Build every field of schema §1 through an explicit allowlist (named field reads only, never dict copies):
   - Strip category prefixes (`CHARACTER.IRONCLAD` → `IRONCLAD`).
   - Card `cost` becomes an int, or null for `"X"` or unplayable.
   - Hand order comes from `hand[]` index. `draw_pile` is a multiset sorted by (id, upgraded). Discard and exhaust order come from `*_ordered` when present.
   - Enemies: alive enemies plus those with a `revive` block. The enemy id is the `entity_id` without its `_<n>` suffix.
   - Intents take `damage`/`base_damage`/`hits` from the intent objects.
   - `hand_select` becomes `pending_selection` (schema §1.1).
2. **Never read or copy** `draw_pile_ordered`, any RNG field, or unknown keys. Test: an input carrying `draw_pile_ordered` in an order that differs from the sorted draw pile; the output's draw pile is the sorted multiset, and nothing in the output depends on that order.
3. A missing required field raises `SnapshotError` naming the path (e.g. `player.hp`).
4. Tests:
   - a full combat fixture produces the exact expected snapshot;
   - the hidden-order test above;
   - `hand_select` → `pending_selection`;
   - a reviving enemy is kept and a dead one is dropped;
   - an X-cost card;
   - a missing field raises with its path;
   - the output validates structurally against schema §1: required keys and types.
5. Commit `feat(snapshot): build live snapshots from mod state with a visible-field allowlist`.

---

### Task 12: Live smoke test (run by the controller, not a subagent)

Steps, in order. Any failure stops the task and is logged.
1. Confirm the game is closed (`pgrep`).
2. `spire-coach saves seed-modded --from 1 --to 1` and `--from 1 --to 3`. Record the backup paths.
3. `spire-coach mod install --repo ~/Documents/git/STS2MCP` builds and installs the DLL, manifest and conf. Then `spire-coach mod enable`.
4. Launch through Steam: `open "steam://rungameid/2868840"`. Wait until `hello` answers, up to 180 s.
5. `spire-coach doctor` passes every check.
6. Through the API: switch to profile 3 (`POST /api/v1/profiles {"action":"switch","profile_id":3}`), then start an Ironclad run through `menu_select`.
7. Run `spire-coach capture` while driving at least one full fight through the API (`play_card`/`end_turn` with `wait`). Record ≥ 30 settled combat states and their action-log entries, all with `source = "api"`.
8. Build a live snapshot from every captured combat state with `build_live_snapshot`, with zero errors. Hand-check one against the game screen.
9. Leave the game on the main menu, close it cleanly, and record the results in the ledger.
