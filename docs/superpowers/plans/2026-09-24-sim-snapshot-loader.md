# Simulator Snapshot Loader — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The emulator can load any combat position from a `sts2-emu-snapshot/1` document, export its state in the same format, and clone and resample loaded positions correctly, so search can start from a live game position.

**Architecture:**
- New native exports in `src/Sts2Emulator/Interop/NativeExports.cs`, backed by a new `src/Sts2Emulator/Interop/CombatSnapshot.cs`. It holds a JSON reader and writer built on `System.Text.Json.JsonDocument` / `Utf8JsonWriter`, with no reflection serializer, so it is NativeAOT-safe.
- A new `NativeCombat(CombatState, int seed)` constructor.
- Fixes to `StateCloning.cs`.
- ctypes bindings in `src/sts2_gym/native.py`.

**Tech Stack:** C# .NET 9 NativeAOT (SDK 9.0.317), xUnit, Python 3.12 ctypes, unittest.

**Spec:** `docs/superpowers/specs/2026-09-24-snapshot-schemas.md` §2 (binding). Parent: `2026-09-23-spire-coach-design.md` §5.2, §6.

## Global Constraints

- Repo: the simulator fork. Work in the worktree `~/Documents/git/sts2-wt/loader` on branch `spire-coach-loader`, created from `spire-coach` at `37e4264`.
- Environment for every command: `source ~/Documents/git/spire-coach/tools/env.sh` (or export `DOTNET_ROOT="$HOME/.dotnet"; PATH="$HOME/.dotnet:$HOME/.dotnet/tools:$HOME/.local/bin:$PATH"`).
- Build: `bash scripts/build.sh osx-arm64` produces `out/Sts2Emulator.dylib`.
- C# tests: `dotnet test src/Sts2Emulator.Tests/Sts2Emulator.Tests.csproj`. Baseline is 4,801 passed, 1 skipped, 0 failed; that must hold plus the new tests.
- Python: `uv sync --python 3.12` then `uv run python -m unittest discover -s tests/python`. Baseline is 527 OK, 6 skipped.
- Follow the repo's own rules in `AGENTS.md` and `HANDOFF.md`:
  - generated files are never hand-edited;
  - expected values in tests cite their source;
  - native API changes bump `NATIVE_API_VERSION` and the Python `_REQUIRED_NATIVE_API_VERSION` together (see `HANDOFF.md` near line 544).
- `decompiled/` exists locally and is gitignored. Never commit it.
- JSON parsing must be NativeAOT/trim-safe. Use `JsonDocument` and `Utf8JsonWriter` only, never `JsonSerializer` with reflection. Confirm with a real `build.sh` publish, not just `dotnet build`.
- Do not change game-logic behaviour outside what this plan names.

## Review Focus

1. **Malformed or partial documents** (missing required field, unknown id, wrong type, hand of 11 cards). The load returns a failure code, and `Sts2_GetLastError` names the field. It never crashes or produces a half-built state.
2. **Draw pile with known top and bottom cards plus an unordered middle.** After a resampling clone, only the unordered middle is reshuffled.
3. **A pending selection loaded, then cloned.** Every field survives the clone (the existing clone drops three).
4. **Loaded buffs include debuffs while the player holds Artifact.** Buffs are placed directly, so Artifact does not eat them.
5. **Round-trip of a state produced by the normal `CombatFactory.Reset` path.** Export, load, export again gives identical documents, and playing each card in hand gives identical immediate results in the original and the loaded copy.

---

### Task 1: Clone correctness fixes

**Files:**
- Modify: `src/Sts2Emulator/Core/StateCloning.cs` (around lines 43–62, 107–116 and 136–266)
- Test: `src/Sts2Emulator.Tests/StateCloningTests.cs` (extend) or a new `src/Sts2Emulator.Tests/SnapshotCloneFixTests.cs`

**Interfaces:**
- Produces: `PendingCardSelection.Clone()` copies every property. `CombatState.Clone(seed)` reseeds every stream, including `EnergyCostRng`, each from a distinct derived seed.

Requirements:
1. `PendingCardSelection.Clone` must copy `Skippable`, `GeneratedUpgraded` and `GeneratedRetained`, in addition to what it copies today. Add a reflection test that fails if any public **property** of `PendingCardSelection` differs between original and clone. The existing guard walks fields only (`StateCloningTests.cs:37-38`), so extend it to properties.
2. `CombatState.Clone(seed)` currently clones `EnergyCostRng` without reseeding (`StateCloning.cs:185`). Reseed it like the other streams.
3. Every reseeded stream currently gets the same int seed (`StateCloning.cs:182-195`). Derive a distinct per-stream seed, e.g. `unchecked(seed * 0x9E3779B1 + DeterministicHash(streamName))`, so resampled streams are not correlated.
   - Use the existing `GameRng`/`DeterministicHash` helper; do not invent a new hash.
   - Keep the no-seed clone path bit-identical to today.
4. Tests:
   - A cloned pending selection keeps all fields.
   - A resampling clone gives `EnergyCostRng` a different state from the source.
   - Two streams in a resampling clone produce different first draws.
   - A non-resampling clone leaves every stream's `CallCount` and next value identical to the source.

Steps: write the failing tests; run them to confirm the failures; implement; run the focused tests, then the full C# suite; commit `fix(clone): copy all PendingCardSelection fields and reseed every stream distinctly`.

---

### Task 2: `Sts2_LoadCombatSnapshot`, `Sts2_GetStateJson`, `Sts2_GetLastError`

**Files:**
- Create: `src/Sts2Emulator/Interop/CombatSnapshot.cs`, holding `static class CombatSnapshot` with `CombatState Read(ReadOnlySpan<byte> json, int seed, out int turn)`, `byte[] Write(CombatState state)` and a `SnapshotException : Exception` carrying the field path.
- Modify: `src/Sts2Emulator/Interop/NativeExports.cs`, adding three exports, the `NativeCombat(CombatState state, int seed)` constructor, and the API version bump.
- Modify: `src/Sts2Emulator/ILLink.Roots.xml`, only if new types need rooting.
- Test: create `src/Sts2Emulator.Tests/CombatSnapshotTests.cs`.

**Interfaces:**
- Consumes: Task 1's clone fixes.
- Produces:
  - `[UnmanagedCallersOnly(EntryPoint = "Sts2_LoadCombatSnapshot")] static int LoadCombatSnapshot(byte* json, int len, int seed, int* obsBuf)` returns a combat handle (≥0), or −1 on a parse/validation error, or −2 when the handle pool is full.
  - `[UnmanagedCallersOnly(EntryPoint = "Sts2_GetStateJson")] static int GetStateJson(int handle, byte* buf, int cap)` returns the byte length needed. It writes only when `cap` is large enough.
  - `[UnmanagedCallersOnly(EntryPoint = "Sts2_GetLastError")] static int GetLastError(byte* buf, int cap)` returns the UTF-8 message length of the last load error (0 if none), with the same size-and-retry contract.

Requirements (the full field list is in the schema spec §2; follow its construction rules §2.2):
1. Build a **fresh** `CombatState` and never reuse a pooled one: `CombatFactory.Reset` leaves many fields stale.
2. Assign fields directly:
   - Player scalars, orbs, Osty and stars.
   - Relics as `RelicInstance(id, counter)`.
   - Potion slots (6 wide) and `MaxPotionSlots`.
   - Buffs appended directly to `PlayerBuffs` / `EnemyState.Buffs`, never through `BuffSystem.Apply`.
   - `PlayerDebuffsAtRoundStart` from the document, or from `BuffSystem.DurationDebuffSnapshot` when the field is null.
   - Cards as `CardInstance` built with `with` for each optional field. `cost_for_combat` / `cost_this_turn` of null map to the `int.MinValue` sentinel.
   - Hand, discard, exhaust and incoming lists in order.
   - Draw pile: `unordered` shuffled with a `CountingRandom(seed)`, then `DrawPile = [..known_top, ..shuffled, ..known_bottom]` with `KnownTopCount`/`KnownBottomCount` set.
   - Enemies as `new EnemyState { … }` with every `ai` field and `CurrentIntent`, followed by `EnemyAI.UpdateSecondaryIntents(state.Enemies)`.
   - `Turn`, `PlayerTurn = true`, every named counter, `IsEliteCombat`, `IsEliteRoom`, `FurCoatBites`, `EncounterId`.
   - The pending selection (all properties) and `EndTurnAwaitingSelection`.
   - `WhisperingEarringSpent` defaults to **true** when the document omits it, so a loaded turn-0 position never replays the earring.
3. Skip `CombatFactory.Reset`, `EnemyAI.ChooseIntents`, every `RelicEffects.Apply*`, `QueueCombatStartAutoPlays` and `CombatFactory.CreateEnemy`.
4. `NativeCombat(CombatState state, int seed)`: set `Seed = seed`, assign `State`, call the existing `SeedStreams()`, then also seed `EnergyCostRng` and `OrbGenerationRng` from `GameRng((uint)seed, "combat_energy_costs")` and `GameRng((uint)seed, "combat_orbs")`. Those names must match whatever names the run path uses for these streams, if it has them; check `RunEngine.cs` first. Register the combat in the handle pool exactly as `Sts2_Create` does, and write the observation into `obsBuf`.
5. Validation. Every unknown id is a `SnapshotException` whose message names the JSON path (e.g. `enemies[1].buffs[0].id: unknown BuffId 999`). The generated `Get(int)` methods throw on unknown ids; catch and rethrow with the path. Other rules:
   - hand ≤ 10;
   - at most 6 enemies;
   - `max_potion_slots` ≤ 6;
   - buffs beyond the observation's 10 (player) / 5 (per enemy) are allowed in state, but the load logs nothing and never truncates.
6. `GetStateJson` writes the same schema from any `CombatState`, including ones built by `CombatFactory.Reset`. The draw pile is split into `known_top` (first `KnownTopCount`), `known_bottom` (last `KnownBottomCount`) and `unordered` (the rest, **sorted by (def_id, upgraded)** so the export is deterministic). Every optional card and AI field is written explicitly, which makes round trips exact.
7. Bump `NATIVE_API_VERSION` (combat) and the run API version exactly as `HANDOFF.md` prescribes, and update the Python required versions in the same commit as Task 3's bindings. If they must move together for the build/tests to pass, do the Python constant bump in this task.
8. C# tests in `CombatSnapshotTests.cs`:
   - **(a) Minimal document:** a player, one enemy, and a hand of 5 → load → export → equals the input after normalization (schema §2.1).
   - **(b) Every optional field set to a non-default value** round-trips exactly. Use one card that sets every `CardInstance` optional field and one enemy that sets every `ai` field.
   - **(c) The `CombatFactory.Reset` path:** reset a real encounter (use an existing test helper or `Fight`), export, load, export again → identical. Then, for every playable card in hand, step once in the original and once in a clone of the loaded state, and assert equal player HP/block/energy, enemy HP/block/buffs and pile counts.
   - **(d) Draw pile:** `known_top` = [A, B] and `known_bottom` = [Z]. After load, `DrawPile[0..1]` is A, B and `DrawPile[^1]` is Z. After a resampling clone the ends hold, and the middle order changes for some seed.
   - **(e) Errors:** an unknown card id, a missing `player.hp` and 11 hand cards each return −1, with `GetLastError` naming the path.
   - **(f) Artifact:** the player has Artifact and a Weak debuff in the document; after load both are present.
   - **(g) Pending selection:** it loads with all fields, and a resampling clone keeps them.
   - **(h) Turn 0 with Whispering Earring:** the relic does not fire on the first `Step` after load.
9. `bash scripts/build.sh osx-arm64` must succeed, and `nm -gU out/Sts2Emulator.dylib | grep -E "Sts2_(LoadCombatSnapshot|GetStateJson|GetLastError)"` must list all three.

Steps: write the failing tests (a)–(h); run them to confirm the failures; implement `CombatSnapshot.cs` and the exports; run the focused tests, then the full C# suite and `build.sh`; commit `feat(interop): load and export combat snapshots (sts2-emu-snapshot/1)`.

---

### Task 3: Python bindings and end-to-end checks

**Files:**
- Modify: `src/sts2_gym/native.py`, adding bindings and the `_REQUIRED_NATIVE_API_VERSION` bump if Task 2 did not already do it.
- Test: create `tests/python/test_combat_snapshot.py`.

**Interfaces:**
- Consumes: Task 2's exports.
- Produces:
  - `native.load_combat_snapshot(doc: dict | str | bytes, seed: int) -> int` returns a handle. Raises `native.SnapshotError(message)` on −1, carrying `Sts2_GetLastError`'s text; raises `RuntimeError` on −2.
  - `native.get_state_json(handle: int) -> dict` (size-and-retry).
  - `native.SnapshotError(Exception)`.

Requirements:
1. The bindings mirror the existing ctypes style in `native.py`: argtypes/restype declared once, and the size-and-retry pattern copied from `Sts2_GetPile` usage.
2. Tests:
   - **(a)** Reset a real combat through the existing env API, `get_state_json`, `load_combat_snapshot`, `get_state_json` again → dicts equal after normalizing the unordered draw multiset.
   - **(b)** `load_combat_snapshot` on a document with an unknown card id raises `SnapshotError` whose message contains `piles.hand[0]`.
   - **(c)** Run the existing Python combat search (`rl/combat_mcts.py`, at a small budget such as 30 simulations) from a **loaded** handle and assert it returns a legal action. This proves search works from loaded positions.
   - **(d)** Clone a loaded handle with resampling 20 times and check the draw-pile middle is actually resampled: at least two distinct orders when the unordered part has ≥ 3 distinct cards.
3. Run the full Python suite; everything passes (baseline 527 OK plus the new tests).

Steps: write the failing tests; run them to confirm the failures; implement the bindings; run the focused tests, then the full Python suite; commit `feat(python): bind snapshot load/export`.
