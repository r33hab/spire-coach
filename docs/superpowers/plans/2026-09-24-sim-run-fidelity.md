# Simulator Run-Engine Fidelity Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three run-engine defects the training investigation found before any long self-play training starts, because each one biases what the strategy model learns:
1. treasure rooms never grant their chest relic;
2. seed `PRB00058` crashes the process;
3. the run engine hardcodes ascension 8, while the user mostly plays ascension 0.

**Architecture:** Changes live in the run layer of the simulator fork (`src/Sts2Emulator/Core/Run/`, `src/Sts2Emulator/Interop/RunNativeExports.cs`, `src/sts2_gym/run_env.py` / `native.py`). Behaviour follows the decompiled game source, with tests citing it.

**Tech Stack:** C# .NET 9 NativeAOT (SDK 9.0.317), xUnit, Python 3.12.

**Spec:** `docs/superpowers/specs/2026-09-23-spire-coach-design.md` §6.4 (the gap-filling workflow) and §8 (training scope: ascension 0 first).

## Global Constraints

- Work in the worktree `~/Documents/git/sts2-wt/gaps` on branch `spire-coach-gaps`, created from `spire-coach` at `37e4264`. The decompiled game source is at `./decompiled` (a gitignored symlink; never commit it).
- Environment: `source ~/Documents/git/spire-coach/tools/env.sh`; build with `bash scripts/build.sh osx-arm64`.
- Baselines must hold, plus the new tests:
  - C# suite `dotnet test src/Sts2Emulator.Tests/Sts2Emulator.Tests.csproj`: 4,801 passed / 1 skipped.
  - Python suite: 527 OK / 6 skipped.
  - The full-run trace checks (`scripts/check_run_traces.py`, `replay_full_run_trace.py`, as documented in HANDOFF.md) still pass.
- A native API change bumps the run API version (currently 19) and the Python required constant together (HANDOFF.md ~544).

## Review Focus

1. **A chest relic granted twice**, or granted when the game grants none (special chests, relics that modify chests).
2. **The crash fix hiding the real bug.** The collection-modified crash must be fixed at its cause, not caught and swallowed.
3. **Ascension 0.** Every ascension-dependent value (enemy HP and damage, elite counts, gold, rest-site heal, curses) follows `Ascension.cs` and the decompiled rules at the chosen level, not just combat.
4. **The existing full-run traces still replay clean**, since they were captured at the engine's current ascension.

---

### Task 1: Treasure chest relic, PRB00058 crash, configurable run ascension

**Files:** determined by investigation in `src/Sts2Emulator/Core/Run/`, `src/Sts2Emulator/Core/CombatEngine.cs` (crash site `EndTurn`), `src/Sts2Emulator/Interop/RunNativeExports.cs`, `src/sts2_gym/run_env.py`, `src/sts2_gym/native.py`, plus tests in `src/Sts2Emulator.Tests/` and `tests/python/`.

**Interfaces:**
- Produces:
  - The run reset takes an ascension level through the native API and Python: `run_env` / `native` accept `ascension: int`, defaulting to 0 for new callers.
  - Existing callers that relied on 8 keep 8 explicitly, so traces and FINDINGS benchmarks stay reproducible.

Requirements (one commit per item, each leaving the suites green):
1. **Treasure relic.** Treasure rooms must grant the chest relic exactly as the game does (`decompiled` treasure room / chest classes: relic rarity roll, the stream used, interaction with relics that modify chests). Test: a run seed reaches a treasure room, and afterwards the relic count and relic id match the game's rules. Cite the source.
2. **PRB00058 crash.** Reproduce the SIGABRT ("Collection was modified" in `CombatEngine.EndTurn`, Soul Fysh boss fight), find the collection mutated during enumeration, and fix the cause (iterate a snapshot, or defer the mutation, whichever matches the game's ordering; check the decompiled code for the intended order). Add a regression test that plays that seed's fight to completion. Search `EndTurn` for other enumerate-while-mutating loops and report them; fix any that are the same bug.
3. **Run ascension parameter.**
   - Thread `ascension` through the run engine reset, the native export (bump the run API version), and the Python `run_env`/`native` wrappers.
   - Every ascension-dependent rule in the run and combat layers reads it, not a hardcoded 8.
   - Tests: at ascension 0 vs 8, a known enemy's HP and damage and one run-level rule (for example rest heal or gold) differ exactly as `Ascension.cs` and the decompiled rules say.
   - Existing traces and benchmarks explicitly pass 8 and still replay clean.
4. Commit messages: `fix(run): treasure rooms grant the chest relic`, `fix(combat): <specific cause> crash in EndTurn (seed PRB00058)`, `feat(run): configurable run ascension (default 0)`.
