# Engine Service (simulator side) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One long-lived process in the simulator's own Python environment. It takes live snapshots from the coach over JSON lines on stdin/stdout and returns:
- **analyze:** combat recommendations and per-move statistics, in game terms;
- **check:** one-step fidelity checks;
- **strategy:** strategy-model option values (once a model exists).

**Architecture:** `src/sts2_gym/live/service.py`, run as `uv run --project ~/Documents/git/sts2 python -m sts2_gym.live.service`, is a thin protocol layer over pieces that already exist on the integration branch:
- `sts2_gym.live.convert.convert` (live → emulator snapshot);
- `native.load_combat_snapshot` / `get_state_json` (loader);
- `rl/live_search.py` `ParallelSearcher` (root-parallel search with paired statistics);
- later, `rl/run_train/policy.score_options`.

Emulator actions are mapped back to game moves: live hand index and card uid, enemy `entity_id`, potion slot, selection index. **Ruling (ledger):** the engine runs out-of-process from the coach. That keeps dependencies isolated (the simulator pins torch/numpy), avoids any network surface (pipes are private), and contains native crashes: a SIGABRT kills only the service, and the coach restarts it.

**Tech Stack:** Python 3.12 (the simulator's uv env), the emulator's ctypes bindings, multiprocessing (via ParallelSearcher), unittest.

**Spec:** `docs/superpowers/specs/2026-09-23-spire-coach-design.md` §5.3, §6.3, §7.3–7.5, §8.1. `docs/superpowers/specs/2026-09-24-snapshot-schemas.md` (binding).

## Global Constraints

- Work in the worktree `~/Documents/git/sts2-wt/service` on branch `spire-coach-service`, created from the integration branch `spire-coach` AFTER it contains the loader, converter (Tasks 1–3), live-search (Tasks 1–2) and potions merges. The controller creates the worktree.
- Environment: `source ~/Documents/git/spire-coach/tools/env.sh`; `uv sync --python 3.12`; `bash scripts/build.sh osx-arm64`.
- Suites: C# and Python must stay green: the integration baseline plus the new tests.
- The protocol is JSON lines. stdout carries ONLY protocol lines; all logging goes to stderr. A request is `{"id": int, "method": str, "params": {...}}`. A response is `{"id": int, "ok": true, "result": {...}}` or `{"id": int, "ok": false, "error": {"type": str, "message": str}}`. The service never crashes on a bad request.
- No hidden information. The service accepts only `sts2-live-snapshot/1` documents, which the converter validates strictly. It never reads the game or mod directly.
- Search statistics exposed for grading are the paired ones: `paired_diff_*`, `paired_death_diff_*`, `paired_n`. Raw `value_mean`/`p_death` are shown only as informational, never as a difference.

## Review Focus

1. **Emulator-to-game move mapping.**
   - The hand index must match the live hand order.
   - The emulator's enemy target index must map to the correct live `entity_id`, including when a dead or reviving enemy is present.
   - Merged identical cards list every live hand index they cover.
2. **A converter warning** (unmodelled content) returns `reliable: false` with the warnings, and still returns a best-effort analysis.
3. **A malformed request, an unknown method, or a snapshot the converter rejects** returns `ok: false` with a clear error. The service stays up.
4. **The check method** separates deterministic mismatches from random fields (a field that varies across resamples) and flags implausible random outcomes. A check the service cannot run (unmodelled content) returns `comparable: false`, never a false mismatch.
5. **Shutdown and cleanup.** `shutdown` and stdin EOF both stop the worker pool cleanly, leaving no orphan processes.

---

### Task 1: Protocol, health, analyze (with game-term move mapping)

**Files:**
- Create: `src/sts2_gym/live/service.py`, `src/sts2_gym/live/moves.py` (the mapping helpers)
- Test: `tests/python/test_live_service.py`

**Interfaces:**
- Produces the protocol methods:
  - `health` → `{service_version, native_api_version, sim_commit, game_version_target: "v0.107.1", workers, strategy_model: null|{...}, combat_prior: null|{...}}`
  - `analyze` with params `{live, time_budget_s=1.5, seed=0, min_child_sims=200, confirm_pairs=50}` → `{reliable, warnings, best: Move, line: [Move], line_truncated_at_draw, moves: [MoveStat], sims, elapsed_s, budget_exhausted, select_mapping_failures}`
  - `shutdown` → `{}`
- Move shape: `{kind: "card"|"potion"|"end_turn"|"select"|"skip", card_index: int|null, card_uid: str|null, card_id: str|null, potion_slot: int|null, potion_id: str|null, target: str|null, select_index: int|null, label: str}`. The label is human text such as "Bash → Jaw Worm", "Fire Potion → Louse", "End turn".
- MoveStat shape: Move + `{visits, value_mean, p_death, paired_diff_vs_best, paired_diff_stderr, paired_n, paired_death_diff_vs_best, paired_death_diff_stderr, hand_indices: [int]}`.
- `moves.to_move(child: ChildStat, live: dict, emu_doc: dict) -> Move` and `moves.hand_indices(child, live) -> list[int]`.

Requirements:
1. The main loop reads stdin line by line and dispatches. It builds one `ParallelSearcher(workers=N, nice=10)` at startup, where N comes from `--workers` (default 4).
2. `analyze`:
   - convert → emulator document;
   - search with `ParallelSearcher.search(doc, time_budget_s=…, seed=…, min_child_sims=…, confirm_pairs=…)`;
   - map `best`, every `children` entry and each `line` step into game terms;
   - `reliable` = the converter's `reliable` flag, AND no search warnings about unmodelled content;
   - `warnings` = converter warnings plus search warnings.
3. The mapping invariants must be verified at runtime:
   - the emulator hand has the same length and order as the live hand (compare card ids);
   - the emulator enemy list has the same order as the live enemies.
   On any violation return `ok: false`, error type `"mapping"`.
4. Tests:
   - a protocol round trip in-process (feed lines to the dispatcher);
   - `health` fields are present;
   - `analyze` on the converter's live fixtures returns a best move whose `card_index` points at a hand card with the matching id, whose target (when present) is a live `entity_id`, and whose `hand_indices` cover identical copies;
   - a malformed request → `ok: false`;
   - an unknown method → `ok: false`;
   - a snapshot with an unknown card id → `ok: false`, type `"conversion"`;
   - a subprocess test that starts the service with `python -m`, sends `health` and `shutdown`, and confirms the exit code is 0 with no orphan processes.
5. Commit `feat(service): engine service with analyze and game-term move mapping`.

### Task 2: check (one-step fidelity) and strategy (model-backed option values)

**Files:**
- Modify: `src/sts2_gym/live/service.py`
- Create: `src/sts2_gym/live/fidelity.py`
- Test: extend `tests/python/test_live_service.py`, create `tests/python/test_live_fidelity.py`

**Interfaces:**
- `check` with params `{pre: live, move: Move, post: live, resamples=16}` → `{comparable: bool, deterministic_mismatches: [{field, predicted, actual}], random_fields: [str], implausible: [{field, actual, possible: [..]}], warnings: [str]}`
- `strategy` with params `{live_run: <the training design's live-run input>, decision: {...}}` → `{options: [{option, value}], model: {...}}`, or `ok: false` with type `"no_model"` until a champion model is exported.

Requirements:
1. `check` does the following:
   - converts `pre` and loads it `resamples` times with different seeds;
   - maps `move` back to the emulator action and target, and steps it;
   - exports each result with `get_state_json`;
   - converts `post` to an emulator document and compares visible fields: player hp/block/energy/buffs, enemies' hp/block/buffs, hand card ids, pile counts, orbs/stars if any.
   A field whose predicted value is identical across all resamples is deterministic: it is a mismatch if it differs from `post`. A field that varies is random: if `post`'s value is not among the resampled outcomes, it is implausible. If `pre`'s conversion is unreliable, the result is `comparable: false`.
2. `strategy` loads the champion from `~/Documents/git/spire-coach/data/models/strategy/`, if present (`model.json` plus weights), through the training package's inference API (`rl/run_train/policy.score_options`). If the model or the package is absent, return `ok: false`, type `"no_model"`. Reload when `model.json`'s generation changes.
3. Tests:
   - a deterministic move (for example Defend: +5 block) gives no mismatches;
   - a tampered post (block +6) gives a deterministic mismatch on `player.block`;
   - a random-outcome move (for example a random-target card) lists its field as random, never as a mismatch;
   - an unreliable pre gives `comparable: false`;
   - `strategy` returns `no_model` without a model, and with a stub model dir (a fake `score_options` injected) returns options sorted by value.
4. Commit `feat(service): fidelity check and strategy model hook`.
