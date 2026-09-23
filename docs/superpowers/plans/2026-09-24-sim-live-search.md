# Live Combat Search — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A time-budgeted combat search the coach can call on any combat handle. It returns the best move with its target, the expected rest of the turn, and per-move statistics (expected HP, death risk, confidence intervals) good enough to grade the player's move. It supports root-parallel search across processes and an optional learned policy prior.

**Architecture:** A new module `rl/live_search.py` in the simulator fork, built on the existing search primitives in `rl/combat_mcts.py`: node structure, tempo rollout, common random numbers, UCT/PUCT. Refactor shared pieces out of `combat_mcts.py` only where they would otherwise be copied, and keep `combat_mcts.py` behaviour identical: its tests and the FINDINGS benchmarks depend on it.
- Root children are keyed by `(action, target)`.
- The search runs common-random-number sweeps across every root child first (guaranteeing each a minimum number of simulations), then UCT until the deadline.
- `merge_stats` combines the root statistics of several independent searches (one per process, different seeds) into one result.

**Tech Stack:** Python 3.12, numpy, the emulator's ctypes bindings (`src/sts2_gym/native.py`), multiprocessing for root parallelism.

**Spec:** `docs/superpowers/specs/2026-09-23-spire-coach-design.md` §7.2–7.5 (binding: value definition, search settings, grades). Research notes: `.superpowers/sdd/research/simulator-internals.md`, section "Python search".

## Global Constraints

- Work in the worktree `~/Documents/git/sts2-wt/search` on branch `spire-coach-search`, created from `spire-coach` at `37e4264`.
- Environment: `source ~/Documents/git/spire-coach/tools/env.sh`; `uv sync --python 3.12`; `bash scripts/build.sh osx-arm64` in the worktree before Python tests.
- Python tests: `uv run python -m unittest discover -s tests/python`. Baseline is 527 OK / 6 skipped; it must hold plus the new tests. Lint changed files with `uv run ruff check` and `uv run black --check`.
- **The value, in HP units (spec §7.2):** `V = end_hp + potion_value * potions_held_at_end` if the player wins; `V = -100` if the player dies. `potion_value` defaults to 10.
  - A rollout that hits the step cap without ending counts as a win at its current HP, and is also counted in a `truncated` statistic.
- **Search defaults (spec §7.3):** exploration constant `c = 0.1`, tempo-weighted rollout policy (the weights in `rl/combat_mcts.py`), common random numbers across root children, final choice by visit count, and at least **200** simulations per legal root child before UCT.
  - Identical cards in hand collapse into one root action. Two hand indices with equal `(def_id, upgraded, enchantment, enchant_amount)` and equal per-copy state share one child.
  - Default wall-clock budget is 1.5 s per search call.
- **Targets:** a card or potion that needs an enemy target gets one root child per living targetable enemy, stepped with `native.step(h, action, obs, rew, target_enemy_index=t)`. Everything else uses target −1. Target indices refer to the full `state.Enemies` list; see the research notes on `Sts2_GetEnemies`.
- The emulator is not thread-safe. Root parallelism uses processes, and each process loads the native library and builds its own root handle.
- No hidden-information cheating. Every simulation starts from a resampling clone (`Sts2_Clone(..., resampleHidden=1, seed)`), never from the root handle's true draw order.

## Review Focus

1. **Positions with one legal action** (only End Turn). The search returns immediately with that action and no wasted simulations.
2. **Multi-enemy fights.** Targets are enumerated and the best target is reported. Dead or reviving enemies are never offered.
3. **An in-combat selection pending at the root** (the valid actions are candidate indices plus skip). The search works on candidate choices, and the result reports them as selection choices, not card plays.
4. **A deadline shorter than the minimum sweep.** The search still returns a result, flagged `budget_exhausted=True`, with whatever statistics it gathered.
5. **Grading noise.** `stderr` and a paired difference against the best child come from common-random-number pairs, so two children with near-identical play show an overlapping interval. The coach turns this into a "Best/Good" grade, never a spurious "Mistake".

---

### Task 1: `search_live` with targets, minimum sweep, per-child statistics and the expected line

**Files:**
- Create: `rl/live_search.py`
- Modify (only to share code, behaviour unchanged): `rl/combat_mcts.py`
- Test: create `tests/python/test_live_search.py`

**Interfaces:**
- Produces, in `rl/live_search.py`:

```python
@dataclass
class ChildStat:
    action: int            # emulator action id at the root
    target: int            # enemy index, -1 if none
    kind: str              # "card" | "potion" | "end_turn" | "select" | "skip"
    card_def_id: int | None
    visits: int
    value_mean: float      # V in HP units (see Global Constraints)
    value_stderr: float
    p_death: float
    mean_end_hp_if_win: float
    paired_diff_vs_best: float | None    # mean over CRN pairs of V(best) - V(this); None if no pairs
    paired_diff_stderr: float | None

@dataclass
class SearchResult:
    best: ChildStat
    children: list[ChildStat]            # sorted by visits desc
    line: list[tuple[int, int, str]]     # expected rest of the turn: (action, target, kind) along most-visited children until end turn or depth 12
    sims: int
    elapsed_s: float
    budget_exhausted: bool
    truncated_rollouts: int

def search_live(handle: int, *, time_budget_s: float = 1.5, min_child_sims: int = 200,
                c: float = 0.1, potion_value: float = 10.0, seed: int = 0,
                max_sims: int | None = None, prior_fn=None, c_puct: float = 0.2) -> SearchResult
```

Requirements:
1. Implement the interface above following the Global Constraints.
   - The value is computed by rollout to the end of combat with the tempo policy.
   - Common-random-number pairing: during the minimum sweep, child *i* and child *j* on sweep *k* share the same clone seed and rollout RNG seed. `paired_diff_vs_best` uses those pairs; `best` is the child with the most visits after UCT.
2. `line` follows the most-visited child at each depth below the root, taken from the search tree, until an `end_turn` node or depth 12.
3. Tests, built with the existing environment helpers to reset real encounters:
   - (a) a single legal action returns immediately with `sims == 0`;
   - (b) a lethal position, where a Bash plus Strike kills a low-HP enemy, puts a lethal line best, with `p_death == 0` for it;
   - (c) a two-enemy encounter enumerates both targets for Strike, and the lower-HP target is chosen when it enables a kill;
   - (d) identical Strikes collapse into one child;
   - (e) a pending selection at the root yields `kind == "select"` children;
   - (f) a 1 ms budget returns `budget_exhausted=True` with a valid `best`;
   - (g) for two duplicate-effect children, the paired stderr is small and the difference's confidence interval covers 0;
   - (h) `combat_mcts.py`'s existing tests still pass unchanged.
4. Commit `feat(search): live search with targets, minimum sweep and per-move statistics`.

---

### Task 2: Root-parallel search and prior hook

**Files:**
- Modify: `rl/live_search.py`
- Test: extend `tests/python/test_live_search.py`

**Interfaces:**
- Produces:

```python
def merge_stats(results: list[SearchResult]) -> SearchResult
    # sums visits and value sums per (action, target); recomputes means, stderr, p_death, paired diffs; best = max visits

class ParallelSearcher:
    def __init__(self, workers: int = 4, nice: int = 10): ...   # starts worker processes once; each imports sts2_gym.native
    def search(self, snapshot_doc: dict, *, time_budget_s: float = 1.5, **kw) -> SearchResult
        # sends the emulator snapshot document to every worker; each worker loads it with native.load_combat_snapshot(doc, seed=worker_seed),
        # runs search_live, returns its SearchResult; the parent merges them
    def close(self) -> None
```

Requirements:
1. `merge_stats` is exact: merging one result returns an equal result, and merging is order-independent.
2. `ParallelSearcher` starts processes once and reuses them across calls, so a search call never pays process startup.
   - A worker that dies is restarted, and that call returns the merge of the surviving workers with a `warnings` note. Add `warnings: list[str] = field(default_factory=list)` to `SearchResult`.
   - Workers set `os.nice(nice)`.
3. `ParallelSearcher.search` depends on `native.load_combat_snapshot`, which comes from the loader plan on another branch. If the binding is absent, raise a clear `RuntimeError("load_combat_snapshot unavailable: merge the loader branch")`. Tests that need it skip on the same condition and activate automatically after the merge.
4. `prior_fn(handle, legal_actions) -> np.ndarray` switches the tree policy to PUCT (`c_puct`). Add one test with a uniform prior that shows the result is still valid.
5. Tests:
   - the merge properties;
   - a worker crash is recovered (a worker function that raises once);
   - with the loader present, a parallel search on a loaded document returns a legal best move and more total simulations than a single worker gets in the same budget.
6. Commit `feat(search): root-parallel live search with merge and prior hook`.
