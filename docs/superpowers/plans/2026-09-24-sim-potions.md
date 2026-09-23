# Simulator Combat Potions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every potion the emulator knows has its real in-combat effect, and potions that need an enemy target are targetable. Today only 8 of 63 do anything: `PotionEffects.Apply` handles ids 5, 9, 17, 32, 47, 49, 50 and 51, and every other id hits `default: break`. Fairy in a Bottle is a separate death check.

**Architecture:** Effects go in `src/Sts2Emulator/Core/Effects/PotionEffects.cs`, which `CombatEngine.UsePotion` (`CombatEngine.cs:1871`) calls. The target comes from `state.TargetEnemyIndex`, which `CombatEngine.Step` already sets for every action (`CombatEngine.cs:20`), potions included. Potion target types come from the game's `PotionModel.TargetType` (`decompiled/MegaCrit.Sts2.Core.Models/PotionModel.cs:101`).

**Tech Stack:** C# .NET 9, xUnit.

**Spec:** `docs/superpowers/specs/2026-09-23-spire-coach-design.md` §6.4 (gap-filling workflow: implement from decompiled code, test values from that code).

## Global Constraints

- Work in the worktree `~/Documents/git/sts2-wt/potions` on branch `spire-coach-potions`, created from `spire-coach` at `37e4264`.
- Environment for every command: `source ~/Documents/git/spire-coach/tools/env.sh`.
- The source of truth is the decompiled game code: `decompiled/MegaCrit.Sts2.Core.Models.Potions/<Name>.cs`, plus whatever it calls. `decompiled/` exists in the main clone at `~/Documents/git/sts2/decompiled`. It is gitignored: read it there, and never copy it into a commit or a test fixture.
- Tests cite their source the repo's way: a comment naming the decompiled file and the value it came from. Follow `AGENTS.md`. Generated files (`src/Sts2Emulator/Generated/*.g.cs`) are regenerated, never hand-edited; if you add a generated field, change `scripts/extract_data.py` and regenerate.
- Reuse the existing effect helpers (damage, block, draw, buff application, card generation) in `CardEffects` / `BuffSystem`. Mirror how cards with the same effect are implemented, and do not duplicate helper logic.
- Randomness uses the stream the game uses: `CardGenerationRng` for generated cards, `PotionGenerationRng` for generated potions, and so on. Check the decompiled code for which stream.
- Baselines must hold: C# suite 4,801 passed / 1 skipped / 0 failed plus new tests; `bash scripts/build.sh osx-arm64` succeeds.

## Review Focus

1. **An enemy-targeted potion when the target index is −1 or points at a dead or reviving enemy.** It follows the same fallback rule cards use (`FirstEnemy`, `CardEffects.cs:8588`) and never throws.
2. **Potions that apply debuffs while the enemy has Artifact.** They go through `BuffSystem.Apply`, so Artifact blocks them as in the game.
3. **Potions whose value scales** (Sacred Bark doubles potency, and relics like Toy Ornithopter trigger on potion use). Existing relic hooks still fire, and potency follows the game's rules.
4. **Potions usable only outside combat, or combat-only.** Their combat behaviour matches the game.
5. **The observation and valid-action encoding are unchanged in shape.** Targets are chosen with `Sts2_StepTargeted`, with no new action ids.

---

### Task 1: Potion target types, and potion ids 1–31

**Files:**
- Modify: `src/Sts2Emulator/Core/Effects/PotionEffects.cs`
- Modify: `scripts/extract_data.py` and the regenerated `src/Sts2Emulator/Generated/Potions.g.cs`, **or** create `src/Sts2Emulator/Core/Effects/PotionTargets.cs` if the generator cannot be extended cleanly. Rule: prefer the generator. If you use a hand table, add a test that cross-checks it against the decompiled `TargetType` of every potion (skipped when `decompiled/` is absent).
- Test: create `src/Sts2Emulator.Tests/PotionEffectTests.cs`.

**Interfaces:**
- Produces:
  - `PotionDef.TargetType`, or `PotionTargets.Get(int potionId)`, returning an enum with at least `Self`, `AnyEnemy`, `AllEnemies` and `None`, mirroring the game's `TargetType`.
  - `static bool PotionEffects.NeedsEnemyTarget(int potionId)`.

Requirements:
1. Add the target-type data for all 63 potions.
2. Implement every unimplemented potion whose emulator id is 1–31 (ids 5, 9 and 17 already exist; check each against the decompiled source and fix any discrepancy you find, citing it). For each potion:
   - read its decompiled class and every helper it calls;
   - implement the combat effect;
   - write at least one test asserting the exact numbers, and for enemy-targeted potions one test that targets a non-first enemy.
3. If a potion's mechanic has no emulator support (for example, it needs a subsystem the emulator lacks), implement nothing fake. Add it to a `PotionEffects.Unsupported` set with a comment explaining why, and report it.
4. Run the full C# suite and `build.sh`; commit `feat(potions): target types and combat effects for potion ids 1-31`.

---

### Task 2: Potion ids 32–63, plus the unsupported-potion guard

**Files:**
- Modify: `src/Sts2Emulator/Core/Effects/PotionEffects.cs`, `src/Sts2Emulator/Core/CombatEngine.cs` (valid actions)
- Test: extend `src/Sts2Emulator.Tests/PotionEffectTests.cs`

**Interfaces:**
- Consumes: Task 1's target-type data and `PotionEffects.Unsupported`.
- Produces: `PotionEffects.IsModelled(int potionId) -> bool`. It is false only for members of `Unsupported`.

Requirements:
1. Implement every unimplemented potion with id 32–63, under the same rules as Task 1 (ids 32, 47, 49, 50 and 51 exist; verify them against the source).
2. `CombatEngine` valid actions (`CombatEngine.cs` around 2388–2394) currently offer every nonzero potion slot and ignore `MaxPotionSlots`:
   - Slots at index ≥ `MaxPotionSlots` must not be offered.
   - Potions that are not modelled (`!IsModelled`) must not be offered, so search never "uses" a potion that does nothing.
   - Add tests for both.
3. The final report lists every potion id with its status: implemented / verified-existing / unsupported (with reason).
4. Run the full C# suite and `build.sh`; commit `feat(potions): combat effects for potion ids 32-63 and hide unmodelled potions from search`.
