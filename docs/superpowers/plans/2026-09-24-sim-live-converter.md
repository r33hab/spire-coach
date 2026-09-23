# Live → Simulator Snapshot Converter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python package in the simulator fork, `sts2_gym.live`, that turns a `sts2-live-snapshot/1` document (game terms) into a `sts2-emu-snapshot/1` document (emulator ints and semantics). It covers ids, powers, relic counters, cards, history counters and turn, per-monster enemy AI state for every Act 1 monster, and Ironclad in-combat selections.

**Architecture:** Emulator-specific meaning lives here, next to the emulator:
- **Id and semantics tables:** `src/sts2_gym/live/ids.py`, `powers.py`, `relics.py`, `cards.py`.
- **Per-monster AI mapping:** `src/sts2_gym/live/enemy_ai.py`.
- **Orchestration:** `src/sts2_gym/live/convert.py`, exposing `convert(live: dict) -> ConversionResult`.

The package reuses the repo's existing mapping code rather than re-deriving it:
- `scripts/buff_map.py` (`BUFF_ID_TO_LIVE`, `LIVE_ALIASES`, `canonical_power`);
- `scripts/generate_card_capture_tests.py` (`DISPLAY_AMOUNT_BUFFS`, `PRESENCE_ONLY_BUFFS`, `card_literal`, `enemy_def_id`);
- `scripts/replay_full_run_trace.py` (`card_slug_to_id`, `relic_slug_to_id`, `potion_name_by_id`);
- `src/sts2_gym/commands.py` (`camel_to_replay_id`);
- `src/sts2_gym/names.py`;
- `data/id_map.json`.

Move shared logic into `sts2_gym.live` and have the scripts import it, rather than copying it.

**Tech Stack:** Python 3.12, unittest (the repo's test runner), the emulator's ctypes bindings for self-consistency tests.

**Spec:** `docs/superpowers/specs/2026-09-24-snapshot-schemas.md` (binding: input §1, output §2). Parent: `2026-09-23-spire-coach-design.md` §6.1–6.3.

## Global Constraints

- Work in the worktree `~/Documents/git/sts2-wt/convert` on branch `spire-coach-convert`, created from `spire-coach` at `37e4264`.
- Environment for every command: `source ~/Documents/git/spire-coach/tools/env.sh`, then `uv sync --python 3.12`.
- Python tests: `uv run python -m unittest discover -s tests/python`. Baseline is 527 OK, 6 skipped, and it must hold plus the new tests.
- Run the repo's Python lint on changed files: `uv run ruff check <files>` and `uv run black --check <files>`.
- The live snapshot contains only player-visible information. The converter must never require hidden information, and must reject any document carrying fields the schema does not define. That catches upstream leaks.
- Semantics come from the decompiled game code, read at `~/Documents/git/sts2/decompiled/` (gitignored; never commit or copy it into fixtures), and from the emulator's C# (`src/Sts2Emulator/Core/EnemyAI.cs`, `Effects/RelicEffects.cs`, `BuffState.cs`). Cite the file:line behind every non-obvious mapping in a comment.
- Unknown ids raise `ConversionError` with the JSON path. Content that is known but unmodelled produces a warning in `ConversionResult.warnings` rather than an error, so the coach can flag the position as unreliable.
- `ConversionResult` is a dataclass: `doc: dict` (the `sts2-emu-snapshot/1` document), `warnings: list[str]`, `reliable: bool` (false when any warning names unmodelled content).

## Review Focus

1. **A live power whose displayed amount differs from what the emulator stores** (Outbreak, Monologue, Orbit, Automation, Panache, Tender, Surrounded, Possess, Thievery/Heist, and the TemporaryStrength family). Amounts are translated, not copied.
2. **An enemy that is Stunned or Reviving.** The emulator-only companion BuffIds (`Stunned`, `Reviving`, …) and the matching `CurrentIntent` are derived. For a stunned slug, `MoveIndex` still points at its pending move.
3. **A relic whose game counter means something different in the emulator** (per-turn counters, modulo counters, used flags). It is translated per relic, using `relics[].fields` when the counter alone is ambiguous.
4. **Displayed intent damage versus base damage.** The emulator gets `base_damage` per hit, never the displayed number. When `base_damage` is null, it is reconstructed from displayed damage and the modifiers in view, or the position is marked unreliable.
5. **An Act 1 monster missing from the AI table, or a `state_log` the table cannot explain.** This produces a warning and `reliable = false`, with the best-effort generic fields. It never raises.

---

### Task 1: Ids, player, piles, powers, relics, history and turn

**Files:**
- Create: `src/sts2_gym/live/__init__.py`, `ids.py`, `powers.py`, `relics.py`, `cards.py`, `convert.py`
- Modify: `scripts/buff_map.py`, `scripts/generate_card_capture_tests.py`, `scripts/replay_full_run_trace.py`, and only these, to import the moved helpers from `sts2_gym.live` instead of defining them.
- Test: create `tests/python/test_live_convert.py`

**Interfaces:**
- Produces:
  - `sts2_gym.live.convert.convert(live: dict) -> ConversionResult`
  - `ConversionError(Exception)`
  - `ids.card_id(entry: str) -> int`, `ids.relic_id(entry) -> int`, `ids.potion_id(entry) -> int`, `ids.enemy_id(entry) -> int`, `ids.enchantment(name) -> int`
  - `powers.to_buffs(live_powers: list[dict], *, owner: str) -> list[dict]`, where owner is `"player"` or `"enemy"`
  - `relics.to_relics(live_relics: list[dict]) -> tuple[list[dict], list[str]]` (relics, warnings)

Requirements:
1. Every id family resolves game `Entry` strings (UPPER_SNAKE, no category prefix) to emulator ints. Build the reverse tables once from `data/id_map.json` and the generated names (`names.py`). Card and relic `Entry` strings exist directly in the generated defs; potions and enemies go through `camel_to_replay_id` on class names.
2. Powers use the existing live↔BuffId tables:
   - Translate display amounts to stored amounts.
   - Drop presence-only amounts to the emulator's stored value.
   - Map all 13 TemporaryStrength variants onto their single BuffId.
   - Emit a warning for game powers with no emulator BuffId (unmodelled).
3. Relics: write a table covering **every relic an Act 1 run can hold**. `scripts/audit_relics.py --reachable` lists them (131). Each entry translates `(counter, fields)` into the emulator `Counter`, following `RelicEffects.cs` (per-turn counters, modulo counters, used flags, run-level counters). Where the game side cannot supply the value, choose the emulator's start-of-combat default and emit a warning naming the relic.
4. Cards: `def_id`, `upgraded`, `enchantment`/`enchant_amount`, and cost overrides from `fields`. Map `cost_this_turn` / `cost_for_combat` only when the fields say so. A displayed cost that differs from base without explaining fields produces a warning.
5. Player: `max_energy` is **not** the live display. It is 3 plus flat relic bonuses, mirroring `RelicEffects.cs:651-662`.
6. Turn: `turn = round - 1`. Counters: map `history` keys onto the emulator counter names in schema §2. Every other counter takes its default, and `whispering_earring_spent` is true.
7. Enemies in this task:
   - `def_id`, slot, HP, block, buffs.
   - An intent from `intents[0]` using `base_damage` and `hits`; the `IntentType` mapping comes from the game's intent classes.
   - `secondary` intents left to the emulator.
   - A **generic** AI block: `move_index` = number of `MoveState` entries in `state_log` minus one, if the log is non-empty; `last_move`/`move_history` −1/[] unless Task 2's table knows better. Stubbing through a single function lets Task 2 replace it.
8. Reject unknown top-level or nested keys with `ConversionError` (path included).
9. Tests:
   - One live fixture per area (player, piles with known top/bottom, powers including a display-vs-stored case, relics covering each counter kind, cards with an enchantment and a cost override, an enemy with an attack intent).
   - An unknown id → `ConversionError` with its path.
   - An unmodelled power → warning, `reliable = False`.
   - Extra keys rejected.
   - The moved helpers still work from the scripts: import and call each one.
10. Commit `feat(live): convert live snapshots to emulator snapshots (player, piles, powers, relics)`.

---

### Task 2: Per-monster AI mapping for every Act 1 monster

**Files:**
- Create: `src/sts2_gym/live/enemy_ai.py`
- Modify: `src/sts2_gym/live/convert.py`, to call `enemy_ai.to_ai(...)`
- Test: create `tests/python/test_live_enemy_ai.py`

**Interfaces:**
- Consumes: Task 1's `convert` and `ids.enemy_id`.
- Produces: `enemy_ai.to_ai(enemy: dict, *, roster: list[dict], turn: int) -> tuple[dict, dict, list[str]]`, returning (emulator `ai` block, emulator `intent`, warnings). `enemy_ai.SUPPORTED: frozenset[int]` holds the enemy def_ids with a verified mapping.

Requirements:
1. Enumerate every monster that can appear in an Act 1 encounter, from the emulator's encounter data and the decompiled `MegaCrit.Sts2.Core.Models.Monsters` / encounter classes.
2. For each monster, read its game class and build a table from the game `MoveState.StateId` strings, including branch states, to the emulator's move enumeration in its `EnemyAI.SelectIntent` case (`EnemyAI.cs:1100-3554`) and the side-effect reads in `ExecuteIntent`. Also read the monster's `ai_fields` that the emulator needs (counters, flags).
3. Reconstruct the emulator fields from `(move_id, state_log, ai_fields, roster, turn)`: `MoveIndex`, `LastMove`, `MoveHistory`, `LastMoveRepeats`, `OnceOnlyMoveUsed`, `LastBranch`, `RepeatStreak`, `BranchCooldown`, `StartsOnBranch`, emulator-only BuffIds (`SummonCooldown`, `BackupCount`, …) and `CurrentIntent`.
   - Follow the convention that at display time `MoveIndex` equals the value `SelectIntent` used, and `LastMove`/`MoveHistory` already include the displayed move.
   - Handle slot-dependent openers (Exoskeleton) from `roster` and stunned states.
4. **Self-consistency tests with no live game.** For each supported monster:
   - Reset its encounter in the emulator and play enemy turns for at least 6 turns, using a deterministic player policy such as "end turn" with high max HP.
   - Record the emulator's own AI fields and intents each turn.
   - Translate the emulator move history into game-style `state_log`/`move_id` using the table.
   - Feed that into `to_ai`, and assert the reconstructed fields equal the emulator's recorded fields on every turn.
   - For deterministic monsters, also assert that a throwaway `ChooseIntents` from the reconstructed state yields the recorded next intent. Random-branch monsters are checked field by field only.
5. Any monster the table cannot cover exactly is left out of `SUPPORTED`, with a comment explaining why. `convert` then warns and marks the position unreliable.
6. Commit `feat(live): per-monster AI state mapping for Act 1 monsters`.

---

### Task 3: Ironclad in-combat selections, and end-to-end load

**Files:**
- Create: `src/sts2_gym/live/selection.py`, and `src/sts2_gym/live/__main__.py`, a CLI `python -m sts2_gym.live <live.json>` that prints the emulator document and warnings.
- Modify: `src/sts2_gym/live/convert.py`
- Test: create `tests/python/test_live_selection.py`, and extend `tests/python/test_live_convert.py`

**Interfaces:**
- Consumes: Tasks 1–2. When merged into `spire-coach`, also `native.load_combat_snapshot` / `native.get_state_json` from the loader plan; see requirement 3.
- Produces: `selection.to_pending(live_pending: dict, hand: list[dict]) -> dict | None`

Requirements:
1. Map the in-combat selection prompts that Ironclad cards and Act 1 relics and potions can raise onto the emulator's `PendingCardSelection` (25 kinds in `PendingCardSelection.cs:4-202`).
   - The emulator models selections as **post-play**: the played card is already in its result pile, and the follow-up must be expressible in `PendingCardSelection` fields.
   - Unmappable prompts warn and return None.
2. Tests: one per mapped kind, plus one unmappable prompt.
3. End-to-end test:
   - Skip unless `native` exposes `load_combat_snapshot`. The loader lands on a separate branch; the test must activate automatically once both are merged.
   - For every live fixture from Tasks 1–3: `convert` → `load_combat_snapshot` → `get_state_json` → assert equal to `convert`'s output after normalization (schema §2.1).
4. Commit `feat(live): Ironclad in-combat selections and end-to-end load check`.
