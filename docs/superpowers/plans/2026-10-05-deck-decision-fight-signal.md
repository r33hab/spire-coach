# Deck Decisions from Measured Fights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the full-run strategy model tell good deck changes from weak ones by measuring their fight effect with paired simulated fights — at decision time, and as FightNet training data.

**Architecture:** A new module `rl/run_train/deckfights.py` plays the same upcoming fights for several candidate afterstates on shared seeds and returns, per candidate, the fight block H that V already reads. `policy` can score a deck screen with that measured H instead of FightNet's (switchable, A/B-tested on the eval seeds). The generation loop gains a `pair` phase that writes paired fights (base plus upgrade/remove/add variants on one seed) that `train` feeds to FightNet. The live engine service and the autopilot can turn measured H on.

**Tech Stack:** Python 3.12, numpy, the sts2 native simulator through `rl/prior_loop/loop.py` (`evaluate_fights`), unittest (sts2), pytest (spire-coach), uv.

**Spec:** `spire-coach/docs/superpowers/specs/2026-10-05-deck-decision-fight-signal-design.md`

## Global Constraints

- Code lives in `~/Documents/git/sts2` (all of Tasks 1–7 and Task 8's service half) and `~/Documents/git/spire-coach` (Task 8's autopilot half and this plan).
- No C# changes and no native rebuild: everything is Python.
- Run sts2 tests as `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.<module>`; spire-coach tests as `cd ~/Documents/git/spire-coach && uv run pytest -q <file> -p no:cacheprovider`.
- Never push. Commit after each task's tests pass.
- The autopilot runs on this Mac while this plan is implemented: no heavy compute here (truth computation, A/B) until its batch is done; those steps name the work Mac.
- Measured H follows `features.fight_blocks`' layout exactly (8 numbers: weak, normal, elite, boss × [E/100, P(death)]).
- A fight's HP after counts the run engine's post-fight heals (Burning Blood +6, Black Blood +12, Meat on the Bone +12 at half HP or less, `RunRewardGenerator.cs:165-196`), as FightNet's labels do; the arena fight engine applies none of them.
- Defaults from the spec: `seeds = 3`, `budget = "120/500"`, `candidates = 4`; truth `seeds = 8`; test set 500 decisions; A/B on the first 200 eval seeds; `paired_fights` default 0 in code (the work Mac sets it).

## Rulings against the spec

- `paired_weight` is not implemented: at its only specified value (1.0) it is a no-op, and `paired_fights` is the dial.
- `select.transform` options are never measured (their outcome is random, and `features.encode_afterstate` already scores them as an expectation); they keep FightNet's H.
- Chosen Cheese's +1 max HP after a fight is ignored in post-fight HP (one HP, not a heal).
- The self-play switch for measured H (spec 3, "a job setting turns it on once the A/B says it pays") is not built here: whether it pays is Task 4's result, and the switch is a one-line spec key (`"rollouts"`, Task 2) the loop's `selfplay_spec` can add then.

## Review Focus

1. A deck screen whose shortlisted fights all crash: every candidate must fall back to FightNet's H and the screen still scores — Task 1 (`test_a_candidate_with_too_many_missing_fights_falls_back`) and Task 2 (`test_failed_measurement_scores_like_fightnet`).
2. Post-fight heals: a won fight with Burning Blood reports end HP + 6, capped at max HP; a lost fight is 0 — Task 1 (`test_post_fight_hp_applies_the_run_engines_heals`).
3. An existing job resuming with the new code must not insert a `pair` phase into a generation that already trained — Task 6 (`test_a_trained_generation_never_gets_a_pair_phase`).
4. A pair record of an eval or final-test seed must be refused by training like a shard record — Task 6 (`test_pairs_of_a_reserved_seed_are_refused`).
5. The live service with `STS2_DECK_ROLLOUTS` unset must score exactly as before (no rollouts kwarg passed) — Task 8 (`test_rollouts_are_off_unless_the_environment_turns_them_on`).

---

### Task 1: Paired fight simulation (`deckfights`)

**Files:**
- Create: `sts2/rl/run_train/deckfights.py`
- Test: `sts2/tests/python/test_run_train_deckfights.py`

**Interfaces:**
- Produces:
  - `RolloutConfig(seeds: int = 3, budget: str = "120/500", candidates: int = 4, workers: int = 1, prior: str = "", ascension: int = 10)` with `to_json() -> dict` and `from_json(dict) -> RolloutConfig`
  - `Play = Callable[[list[dict]], list[dict | None]]`
  - `decision_key(view: RunView) -> int`
  - `fight_seed(key: int, query_index: int, k: int) -> int`
  - `fight_spec(view: RunView, encounter: int, group: str, seed: int, index: int, budget: str) -> dict`
  - `post_fight_hp(relics: Sequence[int], result: dict) -> int`
  - `measured_blocks(base: RunView, views: Sequence[RunView], config: RolloutConfig, *, key: int, play: Play) -> list[np.ndarray | None]`
  - `fight_player(config: RolloutConfig) -> Play`

- [ ] **Step 1: Write the failing tests**

```python
"""Paired fight simulation: measured fight blocks for deck decisions."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rl"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_run_train_features import BASH, STRIKE, make_view  # noqa: E402

from rl.run_train import deckfights, features  # noqa: E402
from rl.run_train.features import CardRef  # noqa: E402


def fake_play(lost: dict[int, int] | None = None, die: frozenset[int] = frozenset()):
    """A player that loses `lost[len(deck)]` HP (10 by default), dying on listed seeds."""

    calls: list[list[dict]] = []

    def play(specs: list[dict]) -> list[dict | None]:
        calls.append(specs)
        out = []
        for spec in specs:
            loss = (lost or {}).get(len(spec["deck"]), 10)
            won = spec["seed"] not in die
            out.append(
                {
                    "seed": spec["seed"],
                    "encounter": spec["encounter"],
                    "start_hp": spec["hp"],
                    "end_hp": max(0, spec["hp"] - loss) if won else 0,
                    "won": won,
                    "max_hp": spec["max_hp"],
                },
            )
        return out

    play.calls = calls  # type: ignore[attr-defined]
    return play


class FightSpecTests(unittest.TestCase):
    def test_a_spec_carries_the_view_and_the_rooms_budget(self) -> None:
        view = make_view(deck=(CardRef(STRIKE, True), CardRef(BASH, False)), floor=7)
        spec = deckfights.fight_spec(view, 42, "elite", 1234, 5, "120/500")
        self.assertEqual(spec["deck"], [-STRIKE, BASH])
        self.assertIsNone(spec["enchantments"])
        self.assertEqual(
            (spec["encounter"], spec["group"], spec["seed"], spec["index"], spec["sims"]),
            (42, "elite", 1234, 5, 500),
        )
        self.assertEqual((spec["hp"], spec["max_hp"], spec["floor"]), (view.hp, view.max_hp, 7))
        hallway = deckfights.fight_spec(view, 42, "hallway", 1, 0, "120/500")
        self.assertEqual(hallway["sims"], 120)

    def test_seeds_are_shared_across_candidates_and_differ_across_fights(self) -> None:
        key = deckfights.decision_key(make_view())
        self.assertEqual(deckfights.fight_seed(key, 3, 1), deckfights.fight_seed(key, 3, 1))
        self.assertNotEqual(deckfights.fight_seed(key, 3, 1), deckfights.fight_seed(key, 3, 2))
        self.assertNotEqual(deckfights.fight_seed(key, 3, 1), deckfights.fight_seed(key, 4, 1))

    def test_post_fight_hp_applies_the_run_engines_heals(self) -> None:
        won = {"won": True, "end_hp": 30, "max_hp": 80}
        self.assertEqual(deckfights.post_fight_hp([], won), 30)
        self.assertEqual(deckfights.post_fight_hp([deckfights.BURNING_BLOOD], won), 36)
        self.assertEqual(deckfights.post_fight_hp([deckfights.BLACK_BLOOD], won), 42)
        # Meat on the Bone checks half HP after the other heals: 30 + 6 = 36 <= 40.
        self.assertEqual(
            deckfights.post_fight_hp([deckfights.BURNING_BLOOD, deckfights.MEAT_ON_THE_BONE], won),
            48,
        )
        self.assertEqual(
            deckfights.post_fight_hp([deckfights.BURNING_BLOOD], {**won, "end_hp": 78}), 80,
        )
        self.assertEqual(
            deckfights.post_fight_hp([deckfights.BURNING_BLOOD], {**won, "won": False, "end_hp": 0}),
            0,
        )


class MeasuredBlockTests(unittest.TestCase):
    def config(self, **fields) -> deckfights.RolloutConfig:
        return dataclasses.replace(deckfights.RolloutConfig(seeds=2), **fields)

    def test_one_block_per_candidate_in_fight_blocks_layout(self) -> None:
        base = make_view(hp=50, relics=())
        thin = dataclasses.replace(base, deck=base.deck[:2])
        play = fake_play(lost={3: 10, 2: 4})
        blocks = deckfights.measured_blocks(base, [base, thin], self.config(), key=7, play=play)
        self.assertEqual(len(play.calls), 1)  # every candidate's fights in one batch
        queries = features.fight_queries(base)
        self.assertEqual(len(play.calls[0]), 2 * 2 * len(queries))
        self.assertEqual(blocks[0].shape, (features.FIGHT_BLOCK_SIZE,))
        np.testing.assert_allclose(blocks[0][0::2], 0.10, rtol=1e-6)  # E/100 in every pool
        np.testing.assert_allclose(blocks[1][0::2], 0.04, rtol=1e-6)
        np.testing.assert_allclose(blocks[0][1::2], 0.0)  # nobody died
        # The same seeds for both candidates.
        seeds = [spec["seed"] for spec in play.calls[0]]
        half = len(seeds) // 2
        self.assertEqual(seeds[:half], seeds[half:])

    def test_a_death_costs_all_hp_brought(self) -> None:
        base = make_view(hp=50, relics=())
        key = 11
        queries = features.fight_queries(base)
        dead = frozenset(deckfights.fight_seed(key, j, 0) for j in range(len(queries)))
        blocks = deckfights.measured_blocks(
            base, [base], self.config(), key=key, play=fake_play(die=dead),
        )
        # Seed 0 of every fight dies (loses 50), seed 1 loses 10: mean loss 30, P(death) 0.5.
        np.testing.assert_allclose(blocks[0][0::2], 0.30, rtol=1e-6)
        np.testing.assert_allclose(blocks[0][1::2], 0.5)

    def test_a_candidate_with_too_many_missing_fights_falls_back(self) -> None:
        base = make_view(relics=())

        def flaky(specs):
            results = fake_play()(specs)
            half = len(specs) // 2
            # Candidate 0 keeps all its fights; candidate 1 loses every one of them.
            return results[:half] + [None] * (len(specs) - half)

        blocks = deckfights.measured_blocks(base, [base, base], self.config(), key=3, play=flaky)
        self.assertIsNotNone(blocks[0])
        self.assertIsNone(blocks[1])

    def test_config_round_trips(self) -> None:
        config = deckfights.RolloutConfig(seeds=5, budget="60", candidates=2, prior="/p")
        self.assertEqual(deckfights.RolloutConfig.from_json(config.to_json()), config)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_run_train_deckfights`
Expected: ERROR, `ImportError: cannot import name 'deckfights'`

- [ ] **Step 3: Write the module**

```python
"""Paired fight simulation: measured fight blocks for deck decisions.

A deck change moves a fight's HP loss by a few points, under far larger fight-to-fight
noise, so FightNet -- which learns from the fights self-play happened to play -- barely
sees it (live 2026-10-05: Smith options within 0.04-0.5 floors of each other). Here the
same upcoming fights are played for every candidate afterstate on the same seeds (common
random numbers), and each candidate gets the fight block H exactly as
`features.fight_blocks` lays it out: measured instead of predicted.

The fights are `features.fight_queries(base)` -- the pools and boss H already averages
over -- `seeds` times each, played by `prior_loop.loop.evaluate_fights` with the combat
prior. A fight's HP after counts the run engine's post-fight heals, as FightNet's labels
do (`post_fight_hp`); the arena fight engine applies none of them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import features
from .features import RunView

log = logging.getLogger(__name__)

# `RunRewardGenerator.cs:165-196`: the heals the run engine applies after a won fight.
BURNING_BLOOD = 36
BLACK_BLOOD = 19
MEAT_ON_THE_BONE = 149

# A query's pool, as the room kind whose budget it is searched at.
POOL_GROUP = {"weak": "hallway", "normal": "hallway", "elite": "elite", "boss": "boss"}
# A candidate missing more of its fights than this keeps FightNet's H (spec: a quarter).
MAX_MISSING = 0.25

Play = Callable[[list[dict[str, Any]]], list[dict[str, Any] | None]]


@dataclass(frozen=True)
class RolloutConfig:
    """How a deck decision's fights are measured."""

    seeds: int = 3
    budget: str = "120/500"
    candidates: int = 4
    workers: int = 1
    prior: str = ""  # a combat prior export folder; "" plays with plain search
    ascension: int = 10

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RolloutConfig:
        return cls(**data)


def decision_key(view: RunView) -> int:
    """A stable key for the position a decision is made in: its fights' seed root."""
    text = repr(
        (view.act_index, view.floor, view.hp, view.max_hp, view.deck, view.relics, view.potions),
    )
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")


def fight_seed(key: int, query_index: int, k: int) -> int:
    """Seed `k` of fight `query_index` at decision `key`, the same for every candidate."""
    digest = hashlib.sha256(f"{key}:{query_index}:{k}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def fight_spec(
    view: RunView,
    encounter: int,
    group: str,
    seed: int,
    index: int,
    budget: str,
) -> dict[str, Any]:
    """A fight `loop.open_fight` deals from `view`, as `real_fights.fight_spec` builds one."""
    normal, _, hard = budget.partition("/")
    hard = hard or normal
    deck = view.deck
    return {
        "index": index,
        "seed": seed,
        "encounter": encounter,
        "group": group,
        "act": view.act_index,
        "deck": [-card.card_id if card.upgraded else card.card_id for card in deck],
        "enchantments": (
            [[card.enchantment, card.enchant_amount] for card in deck]
            if any(card.enchantment for card in deck)
            else None
        ),
        "relics": list(view.relics),
        "potions": list(view.potions),
        "hp": view.hp,
        "max_hp": view.max_hp,
        "gold": view.gold,
        "floor": view.floor,
        "max_potion_slots": view.max_potion_slots,
        "sims": int(normal if group == "hallway" else hard),
    }


def post_fight_hp(relics: Sequence[int], result: dict[str, Any]) -> int:
    """HP after the fight and the run engine's post-fight heals; 0 for a lost fight."""
    if not result["won"]:
        return 0
    hp, top = int(result["end_hp"]), int(result["max_hp"])
    if BURNING_BLOOD in relics:
        hp = min(top, hp + 6)
    if BLACK_BLOOD in relics:
        hp = min(top, hp + 12)
    if MEAT_ON_THE_BONE in relics and hp <= top // 2:
        hp = min(top, hp + 12)
    return hp


def measured_blocks(
    base: RunView,
    views: Sequence[RunView],
    config: RolloutConfig,
    *,
    key: int,
    play: Play,
) -> list[np.ndarray | None]:
    """H for each candidate view, from its fights on the decision's shared seeds.

    None for a candidate missing more than `MAX_MISSING` of its fights, or every fight of
    one query: the caller scores it with FightNet's H.
    """
    queries = features.fight_queries(base)
    per_view = len(queries) * config.seeds
    specs = [
        fight_spec(
            view,
            query.encounter_id,
            POOL_GROUP[query.pool],
            fight_seed(key, j, k),
            v * per_view + j * config.seeds + k,
            config.budget,
        )
        for v, view in enumerate(views)
        for j, query in enumerate(queries)
        for k in range(config.seeds)
    ]
    results = play(specs) if specs else []
    blocks: list[np.ndarray | None] = []
    for v, view in enumerate(views):
        mine = results[v * per_view : (v + 1) * per_view]
        blocks.append(_block(queries, view, mine, config.seeds))
    return blocks


def _block(
    queries: Sequence[features.FightQuery],
    view: RunView,
    results: Sequence[dict[str, Any] | None],
    seeds: int,
) -> np.ndarray | None:
    if sum(result is None for result in results) > MAX_MISSING * len(results):
        return None
    losses = np.zeros(len(queries))
    deaths = np.zeros(len(queries))
    for j in range(len(queries)):
        played = [r for r in results[j * seeds : (j + 1) * seeds] if r is not None]
        if not played:
            return None
        losses[j] = np.mean([r["start_hp"] - post_fight_hp(view.relics, r) for r in played])
        deaths[j] = np.mean([0.0 if r["won"] else 1.0 for r in played])
    return features.fight_blocks(queries, losses[None, :], deaths[None, :])[0]


def fight_player(config: RolloutConfig) -> Play:
    """Play fights with the fight search guided by `config.prior` (plain search if "").

    A batch that raises (a crashed fight takes its batch with it) comes back as all None,
    so its candidates keep FightNet's H.

    Raises:
        ValueError: If `config.prior` names a folder with no combat prior export.

    """
    import combat_prior  # noqa: PLC0415 -- rl/ is on sys.path (the package's __init__)
    from prior_loop import loop as fight_loop  # noqa: PLC0415 -- heavy: only when played

    from .selfplay import DEFAULT_C  # noqa: PLC0415

    champion = combat_prior.find(config.prior) if config.prior else None
    if config.prior and champion is None:
        raise ValueError(f"no combat prior export in {config.prior}")
    settings = {
        "ascension": config.ascension,
        "sims": 0,
        "c": DEFAULT_C,
        "c_puct": champion.c_puct if champion else 0.2,
        "batch_fights": 16,
        "workers": config.workers,
        "prior_min_visits": champion.prior_min_visits if champion else 0,
        "probe": champion.probe if champion else True,
    }
    policy = champion.policy if champion else None

    def play(specs: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
        try:
            return list(fight_loop.evaluate_fights(specs, policy, settings))
        except Exception:
            log.exception("measured fights failed; FightNet's H stands in")
            return [None] * len(specs)

    return play
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_run_train_deckfights`
Expected: OK (7 tests)

- [ ] **Step 5: Smoke-test one real measured decision (light: 2 seeds, 1 worker)**

Run:
```bash
cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python - <<'EOF'
import sys, time, dataclasses
sys.path.insert(0, "rl"); sys.path.insert(0, "tests/python")
from test_run_train_features import make_view, STRIKE
from rl.run_train import deckfights, features, vocab
from rl.run_train.features import CardRef
view = make_view(ascension=10, hp=60, relics=(deckfights.BURNING_BLOOD,))
up = dataclasses.replace(view, deck=(CardRef(STRIKE, True), *view.deck[1:]))
cfg = deckfights.RolloutConfig(seeds=2, workers=1)
t = time.perf_counter()
blocks = deckfights.measured_blocks(view, [view, up], cfg, key=1, play=deckfights.fight_player(cfg))
print("fights", 2 * 2 * len(features.fight_queries(view)), "seconds", round(time.perf_counter() - t, 1))
print(blocks)
EOF
```
Expected: two 8-number blocks (no None); the printed seconds give the per-fight cost used in Task 3's estimate.

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/git/sts2 && git add rl/run_train/deckfights.py tests/python/test_run_train_deckfights.py
git commit -m "run_train: deckfights -- measured fight blocks from paired fights on shared seeds"
```

---

### Task 2: Score deck screens with measured H (`policy`)

**Files:**
- Modify: `sts2/rl/run_train/policy.py` (`floor_probs` at `:185-201`, `score_options` at `:204-250`, `LookaheadPolicy` at `:255-330`)
- Modify: `sts2/rl/run_train/selfplay.py` (`make_policy` at `:128-157`)
- Test: `sts2/tests/python/test_run_train_policy.py`

**Interfaces:**
- Consumes: Task 1's `RolloutConfig`, `Play`, `measured_blocks`, `decision_key`, `fight_player`.
- Produces:
  - `policy.DECK_KINDS: frozenset[str]`
  - `policy.floor_probs(nets, view, options, blocks: Sequence[np.ndarray | None] | None = None) -> np.ndarray`
  - `policy.measured_probs(nets, W: float, view, options, rollouts: RolloutConfig, play: Play | None = None) -> np.ndarray`
  - `policy.score_options(decision, view, model=None, *, rollouts=None, play=None)`
  - `LookaheadPolicy(checkpoint, temperature, eps=0.02, W=None, *, rollouts=None, play=None)`; `describe()` adds `"rollouts"` when set
  - policy spec key `"rollouts"` (a `RolloutConfig.to_json()` dict) read by `selfplay.make_policy`

- [ ] **Step 1: Write the failing tests** (append to `tests/python/test_run_train_policy.py`; it already imports `features`, `models`, `policy`, `selfplay`, `Decision`, `Option`, `CardRef`, `make_view`, `STRIKE`, `BASH`, `DEFEND`)

```python
class MeasuredScoringTests(unittest.TestCase):
    """Deck screens scored with measured H (deck-decision spec 3)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.npz = save_checkpoint(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def model(self) -> policy.Model:
        return policy.Model(models.load_numpy(self.npz), W=53.0)

    @staticmethod
    def smith_screen() -> tuple[Decision, Any]:
        view = make_view(ascension=10)
        options = tuple(
            Option("rest.smith", 1, deck_index=i) for i in range(len(view.deck))
        ) + (Option("rest.heal", 0),)
        return Decision("rest", options), view

    def test_failed_measurement_scores_like_fightnet(self) -> None:
        decision, view = self.smith_screen()
        model = self.model()
        plain = policy.score_options(decision, view, model)
        rollouts = deckfights.RolloutConfig(seeds=1, candidates=2)
        failed = policy.score_options(
            decision, view, model, rollouts=rollouts, play=lambda specs: [None] * len(specs),
        )
        self.assertEqual([s.value for s in failed], [s.value for s in plain])

    def test_measured_blocks_replace_fightnet_for_the_shortlist_only(self) -> None:
        decision, view = self.smith_screen()
        model = self.model()
        rollouts = deckfights.RolloutConfig(seeds=1, candidates=1)
        seen: list[int] = []

        def play(specs):
            seen.append(len(specs))
            return [
                {"seed": s["seed"], "encounter": s["encounter"], "start_hp": s["hp"],
                 "end_hp": 0, "won": False, "max_hp": s["max_hp"]}  # every fight lost
                for s in specs
            ]

        plain = policy.score_options(decision, view, model)
        measured = policy.score_options(decision, view, model, rollouts=rollouts, play=play)
        queries = len(features.fight_queries(view))
        self.assertEqual(seen, [1 * queries])  # one shortlisted option, nothing unchanged
        changed = [p.value != m.value for p, m in zip(plain, measured, strict=True)]
        self.assertEqual(sum(changed), 1)  # only the shortlisted option moved

    def test_a_screen_without_deck_options_is_not_measured(self) -> None:
        view = make_view(ascension=10)
        decision = Decision("rest", (Option("rest.heal", 0),))
        policy.score_options(
            decision, view, self.model(), rollouts=deckfights.RolloutConfig(),
            play=lambda specs: self.fail("no fights for a screen without deck options"),
        )

    def test_a_lookahead_spec_carries_its_rollouts(self) -> None:
        rollouts = deckfights.RolloutConfig(seeds=2, candidates=3)
        player = selfplay.make_policy(
            {"kind": "lookahead", "npz": str(self.npz), "tau": 0.0, "eps": 0.0,
             "W": 53.0, "rollouts": rollouts.to_json()},
        )
        self.assertEqual(player.rollouts, rollouts)
        self.assertEqual(player.describe()["rollouts"], rollouts.to_json())
```

Also add beside the file's other `rl.run_train` imports: `from rl.run_train import deckfights  # noqa: E402`, and `from typing import Any` if missing. `save_checkpoint` (`:67`) is the file's own fixture: fresh nets scaled so their outputs vary.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_run_train_policy -k Measured`
Expected: FAIL/ERROR (`score_options() got an unexpected keyword argument 'rollouts'`)

- [ ] **Step 3: Implement in `policy.py`**

Replace `floor_probs` with:

```python
def floor_probs(
    nets: models.NumpyNets,
    view: RunView,
    options: Sequence[Option],
    blocks: Sequence[np.ndarray | None] | None = None,
) -> np.ndarray:
    """Return V's floor distribution for each option's afterstate, (options, classes).

    Every option must be scorable. H is FightNet over the region's pools and the boss
    (`features.fight_queries`), which an afterstate never changes -- except for an option
    whose entry in `blocks` is a measured H (`deckfights`), which replaces FightNet's. A
    version-1 model reads the first 1843 numbers of each vector (`NumpyNets.base_size`).
    """
    if not options:
        return np.zeros((0, nets.value.floor_classes), dtype=np.float32)
    bases = np.stack([features.encode_afterstate(view, option) for option in options])
    bases = bases[:, : nets.base_size]
    block = nets.fight.summary(bases, features.fight_queries(view))
    if blocks is not None:
        for i, measured in enumerate(blocks):
            if measured is not None:
                block[i] = measured
    return nets.value.floor_probs(np.concatenate([bases, block], axis=1))


# Screens whose options change the deck (deck-decision spec 3): scored with measured H.
DECK_KINDS = frozenset(
    {
        "card.take",
        "card.skip",
        "rest.smith",
        "select.upgrade",
        "select.remove",
        "shop.card",
        "shop.remove",
        "reward.card",
    },
)


def measured_probs(
    nets: models.NumpyNets,
    W: float,  # noqa: N803 -- DESIGN.md 7's name
    view: RunView,
    options: Sequence[Option],
    rollouts: deckfights.RolloutConfig,
    play: deckfights.Play | None = None,
) -> np.ndarray:
    """`floor_probs`, with measured H for the shortlist of a screen that changes the deck.

    The shortlist is FightNet's best `rollouts.candidates` options (never a random
    `select.transform`) plus every unchanged option (skip, leave, proceed). A screen with
    no deck option, and every option off the shortlist, keeps FightNet's H.
    """
    probs = floor_probs(nets, view, options)
    if not any(option.kind in DECK_KINDS for option in options):
        return probs
    values = models.expected_floors(probs, W)
    ranked = [int(i) for i in np.argsort(-values) if options[i].kind != "select.transform"]
    shortlist = sorted(
        set(ranked[: rollouts.candidates])
        | {i for i, option in enumerate(options) if option.kind in features.UNCHANGED_KINDS},
    )
    afters = [features.apply_option(view, options[i]) for i in shortlist]
    measured = deckfights.measured_blocks(
        view,
        afters,
        rollouts,
        key=deckfights.decision_key(view),
        play=play or deckfights.fight_player(rollouts),
    )
    blocks: list[np.ndarray | None] = [None] * len(options)
    for i, block in zip(shortlist, measured, strict=True):
        blocks[i] = block
    return floor_probs(nets, view, options, blocks)
```

Add `from . import deckfights` to the module's imports (beside `features`, `models`).

In `score_options`, change the signature to
`def score_options(decision, view, model=None, *, rollouts: deckfights.RolloutConfig | None = None, play: deckfights.Play | None = None)`
and replace `probs = floor_probs(scorer.nets, view, scorable)` with:

```python
    probs = (
        floor_probs(scorer.nets, view, scorable)
        if rollouts is None
        else measured_probs(scorer.nets, scorer.W, view, scorable, rollouts, play)
    )
```

In `LookaheadPolicy.__init__`, add keyword parameters `rollouts: deckfights.RolloutConfig | None = None, play: deckfights.Play | None = None`, stored as `self.rollouts` and `self._play`. In `describe()`, after building the dict, add `if self.rollouts is not None: block["rollouts"] = self.rollouts.to_json()` (name the dict `block` and return it). In `pick`, replace `probs = floor_probs(self.nets, view, [options[i] for i in scorable])` with:

```python
        chosen = [options[i] for i in scorable]
        probs = (
            floor_probs(self.nets, view, chosen)
            if self.rollouts is None
            else measured_probs(self.nets, self.W, view, chosen, self.rollouts, self._play)
        )
```

- [ ] **Step 4: Implement in `selfplay.make_policy`**

Replace the `return LookaheadPolicy(...)` call with:

```python
        from .deckfights import RolloutConfig  # noqa: PLC0415

        rollouts = spec.get("rollouts")
        return LookaheadPolicy(
            spec["npz"],
            float(spec["tau"]),
            eps=float(spec.get("eps", 0.02)),
            W=float(spec["W"]),
            rollouts=None if rollouts is None else RolloutConfig.from_json(rollouts),
        )
```

and add to its docstring: `` A lookahead spec's optional "rollouts" (a `RolloutConfig.to_json()`) scores deck screens with measured H. ``

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_run_train_policy tests.python.test_run_train_deckfights tests.python.test_run_train_live`
Expected: OK

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/git/sts2 && git add rl/run_train/policy.py rl/run_train/selfplay.py tests/python/test_run_train_policy.py
git commit -m "policy: score deck screens with measured H when rollouts are set"
```

---

### Task 3: The deck-decision test set (`scripts/deck_benchmark.py`)

**Files:**
- Create: `sts2/scripts/deck_benchmark.py`
- Create (by running it): `sts2/rl/run_train/deck_benchmark.json`, later `sts2/rl/run_train/deck_benchmark_truth.json`
- Test: `sts2/tests/python/test_deck_benchmark.py`

**Interfaces:**
- Consumes: Task 1 (`measured_blocks`, `fight_player`, `RolloutConfig`, `decision_key`), Task 2 (`floor_probs(..., blocks)`, `DECK_KINDS`, `measured_probs`).
- Produces: `select_items(records_by_source, n, seed) -> list[dict]`, `spearman(a, b) -> float`, `metrics(model_values, truth_values) -> dict`, and the CLI `build | truth | report`.

- [ ] **Step 1: Write the failing tests**

```python
"""The deck-decision test set: selection and the metrics it reports."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rl"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_run_train_features import make_view  # noqa: E402

import deck_benchmark  # noqa: E402
from rl.run_train import shards  # noqa: E402


def record(decisions: list[dict]) -> dict:
    return {"seed": "S1", "maps": [shards.map_to_json(make_view())], "decisions": decisions}


def entry(kind: str, options: list[dict]) -> dict:
    state = shards.view_to_json(make_view(), 0)
    return {"type": kind, "floor": 3, "state": state, "options": options}


SMITH = [{"kind": "rest.smith", "action": 1, "deck_index": 0, "scorable": True},
         {"kind": "rest.smith", "action": 1, "deck_index": 2, "scorable": True},
         {"kind": "rest.heal", "action": 0, "scorable": True}]


class SelectionTests(unittest.TestCase):
    def test_only_deck_screens_with_two_scorable_options_are_kept(self) -> None:
        records = {
            "a.jsonl.gz": [record([
                entry("rest", SMITH),
                entry("map", [{"kind": "map.node", "action": 0, "node": [0, 1, 1], "scorable": True}] * 2),
                entry("rest", SMITH[:1]),
                entry("card_select", [{"kind": "select.transform", "action": 0, "deck_index": 0, "scorable": True}] * 2),
            ])],
        }
        items = deck_benchmark.select_items(records, n=10, seed=1)
        self.assertEqual([item["decision"]["type"] for item in items], ["rest"])
        self.assertEqual(items[0]["state"]["map"], 0)

    def test_selection_is_fixed_by_its_seed(self) -> None:
        records = {"a.jsonl.gz": [record([entry("rest", SMITH)] * 30)]}
        first = deck_benchmark.select_items(records, n=5, seed=7)
        again = deck_benchmark.select_items(records, n=5, seed=7)
        self.assertEqual([i["id"] for i in first], [i["id"] for i in again])
        self.assertEqual(len(first), 5)


class MetricTests(unittest.TestCase):
    def test_spearman(self) -> None:
        self.assertAlmostEqual(deck_benchmark.spearman([1, 2, 3], [10, 20, 30]), 1.0)
        self.assertAlmostEqual(deck_benchmark.spearman([1, 2, 3], [30, 20, 10]), -1.0)

    def test_metrics_report_agreement_and_value_lost(self) -> None:
        model = [[3.0, 2.0, 1.0], [1.0, 2.0]]
        truth = [[3.0, 2.5, 1.0], [5.0, 4.0]]
        m = deck_benchmark.metrics(model, truth)
        self.assertEqual(m["n"], 2)
        self.assertAlmostEqual(m["top1"], 0.5)
        self.assertAlmostEqual(m["value_lost"], 0.5)  # (0 + 1) / 2


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_deck_benchmark`
Expected: ERROR, `ModuleNotFoundError: No module named 'deck_benchmark'`

- [ ] **Step 3: Write the script**

```python
#!/usr/bin/env python3
"""Deck decisions with measured answers: how well a strategy model picks deck changes.

A fixed set of deck decisions from A10 self-play (rest-site Smith, card rewards, shop
buys and removals, card-select upgrades and removals), each option's fight block H
measured once with many paired fights (the truth, model-independent), and a report of
how a model's picks agree with V scored on that measured H.

    uv run python scripts/deck_benchmark.py build --job ~/Documents/git/sts2-wt/night/a10 --generations 84-89
    uv run python scripts/deck_benchmark.py truth --prior ~/Documents/git/sts2-wt/priors/current --workers 14
    uv run python scripts/deck_benchmark.py report --model ~/Documents/git/sts2-wt/night/a10/export
    uv run python scripts/deck_benchmark.py report --model ... --rollout-seeds 3   # decision-time H
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rl.run_train import deckfights, features, models, policy, shards  # noqa: E402

SET = Path(__file__).resolve().parents[1] / "rl" / "run_train" / "deck_benchmark.json"
TRUTH = SET.with_name("deck_benchmark_truth.json")
TYPES = frozenset({"rest", "card_reward", "shop", "card_select"})


def _deck_entry(entry: dict[str, Any]) -> bool:
    scorable = [o for o in entry.get("options") or () if o.get("scorable", True)]
    return (
        entry.get("type") in TYPES
        and len(scorable) >= 2
        and any(o["kind"] in policy.DECK_KINDS for o in scorable)
        and not any(o["kind"] == "select.transform" for o in scorable)
    )


def select_items(
    records_by_source: dict[str, list[dict[str, Any]]],
    n: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Every deck decision in the records, `n` of them drawn with `seed`, map inlined."""
    pool = []
    for source in sorted(records_by_source):
        for line, rec in enumerate(records_by_source[source]):
            for i, entry in enumerate(rec.get("decisions") or ()):
                if _deck_entry(entry):
                    state = {**entry["state"], "map": 0}
                    pool.append(
                        {
                            "id": f"{source}:{line}:{i}",
                            "state": state,
                            "map": rec["maps"][entry["state"]["map"]],
                            "decision": entry,
                        },
                    )
    random.Random(seed).shuffle(pool)
    return sorted(pool[:n], key=lambda item: item["id"])


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    if ra.std() == 0 or rb.std() == 0:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def metrics(model: Sequence[Sequence[float]], truth: Sequence[Sequence[float]]) -> dict[str, Any]:
    """Top-1 agreement, mean rank correlation, and mean value lost by the model's pick."""
    top1, rho, lost = [], [], []
    for m, t in zip(model, truth, strict=True):
        pick, best = int(np.argmax(m)), int(np.argmax(t))
        top1.append(pick == best)
        rho.append(spearman(m, t))
        lost.append(t[best] - t[pick])
    n = len(top1)
    return {
        "n": n,
        "top1": float(np.mean(top1)) if n else None,
        "spearman": float(np.mean(rho)) if n else None,
        "value_lost": float(np.mean(lost)) if n else None,
    }


def _sha(items: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


def load_set(path: Path = SET) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if _sha(data["items"]) != data["sha256"]:
        raise ValueError(f"{path} does not match its own sha256; it is frozen, restore it")
    return data["items"]


def item_view(item: dict[str, Any]) -> tuple[features.RunView, list[features.Option]]:
    view = shards.view_from_json(item["state"], [item["map"]])
    decision = shards.entry_decision(item["decision"])
    view = decision.view or view
    return view, [o for o in decision.options if o.scorable]


def build(args: argparse.Namespace) -> None:
    lo, _, hi = args.generations.partition("-")
    records: dict[str, list[dict[str, Any]]] = {}
    for g in range(int(lo), int(hi or lo) + 1):
        for path in shards.shard_paths(args.job / f"gen_{g:04d}" / "shards"):
            records[f"gen_{g:04d}/{path.name}"] = shards.read_shard(path)
    items = select_items(records, args.n, args.seed)
    SET.write_text(json.dumps({"seed": args.seed, "sha256": _sha(items), "items": items}))
    kinds: dict[str, int] = {}
    for item in items:
        kinds[item["decision"]["type"]] = kinds.get(item["decision"]["type"], 0) + 1
    print(f"wrote {len(items)} decisions to {SET}: {kinds}")


def truth(args: argparse.Namespace) -> None:
    items = load_set()
    config = deckfights.RolloutConfig(
        seeds=args.seeds, budget=args.budget, workers=args.workers, prior=str(args.prior),
    )
    play = deckfights.fight_player(config)
    import combat_prior  # noqa: PLC0415

    champion = combat_prior.find(args.prior)
    blocks: dict[str, list[list[float] | None]] = {}
    started = time.perf_counter()
    for k, item in enumerate(items):
        view, options = item_view(item)
        afters = [features.apply_option(view, option) for option in options]
        measured = deckfights.measured_blocks(
            view, afters, config, key=deckfights.decision_key(view), play=play,
        )
        blocks[item["id"]] = [None if b is None else [float(x) for x in b] for b in measured]
        if k % 10 == 0:
            print(f"{k + 1}/{len(items)} in {time.perf_counter() - started:.0f} s", flush=True)
    meta = {
        "set_sha256": _sha(items),
        "seeds": args.seeds,
        "budget": args.budget,
        "prior_sha256": None if champion is None else champion.policy_sha256,
    }
    TRUTH.write_text(json.dumps({"meta": meta, "blocks": blocks}))
    print(f"wrote {TRUTH} in {time.perf_counter() - started:.0f} s")


def report(args: argparse.Namespace) -> None:
    items = load_set()
    data = json.loads(TRUTH.read_text())
    if data["meta"]["set_sha256"] != _sha(items):
        raise SystemExit("the truth is for another test set: run `truth` again")
    import combat_prior  # noqa: PLC0415

    champion = combat_prior.find(args.prior)
    if (None if champion is None else champion.policy_sha256) != data["meta"]["prior_sha256"]:
        raise SystemExit("the truth was measured with another combat prior: run `truth` again")
    model = policy.load(args.model)
    rollouts = (
        deckfights.RolloutConfig(
            seeds=args.rollout_seeds, candidates=args.candidates, workers=args.workers,
            prior=str(args.prior),
        )
        if args.rollout_seeds
        else None
    )
    by_type: dict[str, tuple[list, list]] = {}
    offsets = []
    for item in items:
        view, options = item_view(item)
        stored = data["blocks"][item["id"]]
        if any(b is None for b in stored):
            continue
        measured = [np.asarray(b, dtype=np.float32) for b in stored]
        t = models.expected_floors(
            policy.floor_probs(model.nets, view, options, measured), model.W,
        ).tolist()
        if rollouts is None:
            p = policy.floor_probs(model.nets, view, options)
        else:
            p = policy.measured_probs(model.nets, model.W, view, options, rollouts)
        m = models.expected_floors(p, model.W).tolist()
        kind = item["decision"]["type"]
        by_type.setdefault(kind, ([], []))[0].append(m)
        by_type[kind][1].append(t)
        for option, block in zip(options, measured, strict=True):
            if option.kind in features.UNCHANGED_KINDS:
                predicted = model.nets.fight.summary(
                    features.encode_afterstate(view, option)[None, : model.nets.base_size],
                    features.fight_queries(view),
                )[0]
                offsets.append(block - predicted)
    every_m = [m for ms, _ in by_type.values() for m in ms]
    every_t = [t for _, ts in by_type.values() for t in ts]
    out = {"all": metrics(every_m, every_t)}
    out |= {kind: metrics(ms, ts) for kind, (ms, ts) in sorted(by_type.items())}
    if offsets:
        out["measured_minus_fightnet_on_unchanged"] = np.mean(offsets, axis=0).round(4).tolist()
    print(json.dumps(out, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--job", type=Path, required=True)
    b.add_argument("--generations", required=True, help="e.g. 84-89")
    b.add_argument("--n", type=int, default=500)
    b.add_argument("--seed", type=int, default=20261005)
    t = sub.add_parser("truth")
    t.add_argument("--prior", type=Path, required=True)
    t.add_argument("--seeds", type=int, default=8)
    t.add_argument("--budget", default="120/500")
    t.add_argument("--workers", type=int, default=4)
    r = sub.add_parser("report")
    r.add_argument("--model", type=Path, required=True)
    r.add_argument("--rollout-seeds", type=int, default=0)
    r.add_argument("--candidates", type=int, default=4)
    r.add_argument("--prior", type=Path, default=Path.home() / "Documents/git/sts2-wt/priors/current")
    r.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    {"build": build, "truth": truth, "report": report}[args.command](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_deck_benchmark`
Expected: OK (4 tests). (`shards.map_to_json(view)` returns the record's map entry, `shards.py:80`.)

- [ ] **Step 5: Build the frozen set (light: reads shards only)**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python scripts/deck_benchmark.py build --job ~/Documents/git/sts2-wt/night/a10 --generations 84-89`
Expected: `wrote 500 decisions to .../deck_benchmark.json: {...}` with every one of rest, card_reward, shop, card_select present.

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/git/sts2 && git add scripts/deck_benchmark.py tests/python/test_deck_benchmark.py rl/run_train/deck_benchmark.json
git commit -m "scripts: deck-decision test set -- build, measured truth, report"
```

- [ ] **Step 7 (heavy; work Mac or this Mac once the autopilot batch is done): compute the truth and the baseline report**

Run (work Mac, in `~/sts2` after `git pull --ff-only`, with the night job stopped or between its generations):
```bash
PYTHONPATH=src uv run python scripts/deck_benchmark.py truth --prior runs/prior --workers 16
PYTHONPATH=src uv run python scripts/deck_benchmark.py report --model runs/a10/export --prior runs/prior > deck_baseline.json
```
Expected: a report with `top1`, `spearman`, `value_lost` per decision type and `measured_minus_fightnet_on_unchanged` (8 numbers). **Stop and report** if any pool's E/100 offset exceeds 0.02 (2 HP) in magnitude: that is a systematic gap between arena fights and FightNet's run-engine labels to resolve before Tasks 4–6. Commit `deck_benchmark_truth.json` from the work Mac's checkout only if it was computed there; otherwise copy it here and commit:
```bash
git add rl/run_train/deck_benchmark_truth.json && git commit -m "deck benchmark: measured truth (8 seeds, 120/500, prior iteration 4)"
```

---

### Task 4: The A/B on the eval seeds (`scripts/deck_rollout_ab.py`)

**Files:**
- Create: `sts2/scripts/deck_rollout_ab.py`
- Test: `sts2/tests/python/test_deck_rollout_ab.py`

**Interfaces:**
- Consumes: Task 2 (lookahead spec key `"rollouts"`), `evaluate.evaluate`, `evaluate.compare`, `runner.playing_spec`, `runner.JobConfig`, `CombatBudget.parse`.
- Produces: `specs(npz: str, rollouts: RolloutConfig) -> tuple[dict, dict]` and the CLI.

- [ ] **Step 1: Write the failing test**

```python
"""The rollout A/B's two policy specs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rl"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import deck_rollout_ab  # noqa: E402
from rl.run_train import deckfights  # noqa: E402


class SpecTests(unittest.TestCase):
    def test_the_arms_differ_only_by_rollouts_and_play_at_argmax(self) -> None:
        rollouts = deckfights.RolloutConfig(seeds=2)
        plain, measured = deck_rollout_ab.specs("/m/model.npz", rollouts)
        self.assertEqual(plain, {"kind": "lookahead", "npz": "/m/model.npz", "tau": 0.0, "eps": 0.0})
        self.assertEqual(measured, {**plain, "rollouts": rollouts.to_json()})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_deck_rollout_ab`
Expected: ERROR, `ModuleNotFoundError: No module named 'deck_rollout_ab'`

- [ ] **Step 3: Write the script**

```python
#!/usr/bin/env python3
"""Does measured H at deck decisions win more? The champion with and without it.

Both arms play the same eval seeds at argmax; the challenger scores deck screens with
measured H (`deckfights`), the champion with FightNet's. `evaluate.compare` decides it:
adopt only if the paired CI on mean floor is above 0.

    uv run python scripts/deck_rollout_ab.py --model runs/a10/export --prior runs/prior \
        --seeds 200 --workers 16 --out deck_ab.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rl.run_train import deckfights, evaluate, runner  # noqa: E402
from rl.run_train.engine import CombatBudget  # noqa: E402


def specs(npz: str, rollouts: deckfights.RolloutConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    plain = {"kind": "lookahead", "npz": npz, "tau": 0.0, "eps": 0.0}
    return plain, {**plain, "rollouts": rollouts.to_json()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", type=Path, required=True, help="an export folder")
    parser.add_argument("--prior", type=Path, required=True, help="the combat prior folder")
    parser.add_argument("--seeds", type=int, default=200, help="the first N eval seeds")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--combat-budget", default="120/500")
    parser.add_argument("--rollout-seeds", type=int, default=3)
    parser.add_argument("--rollout-budget", default="120/500")
    parser.add_argument("--candidates", type=int, default=4)
    parser.add_argument("--seed-timeout", type=float, default=6 * 3600.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    info = json.loads((args.model / "model.json").read_text())
    config = replace(
        runner.JobConfig(),
        workers=args.workers,
        combat_budget=CombatBudget.parse(args.combat_budget),
        ascension=10,
        W=float(info["W"]),
        combat_prior=str(args.prior),
        seed_timeout=args.seed_timeout,
    )
    rollouts = deckfights.RolloutConfig(
        seeds=args.rollout_seeds,
        budget=args.rollout_budget,
        candidates=args.candidates,
        workers=1,  # inside a seed's worker: pools never nest
        prior=str(args.prior),
        ascension=10,
    )
    seeds = evaluate.load_seeds(evaluate.EVAL_SEEDS)[: args.seeds]
    plain, measured = specs(str(args.model / info["npz"]), rollouts)
    champion = evaluate.evaluate(runner.playing_spec(plain, config), seeds, config)
    challenger = evaluate.evaluate(runner.playing_spec(measured, config), seeds, config)
    comparison = evaluate.compare(challenger, champion)
    args.out.write_text(
        json.dumps(
            {
                "plain": champion.summary().to_json(),
                "measured": challenger.summary().to_json(),
                "comparison": comparison.to_json(),
                "rollouts": rollouts.to_json(),
            },
            indent=2,
        ),
    )
    print(json.dumps(comparison.to_json()["challenger"], indent=2))
    print(f"adopt measured H: {comparison.ci_rule}; wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_deck_rollout_ab`
Expected: OK

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/git/sts2 && git add scripts/deck_rollout_ab.py tests/python/test_deck_rollout_ab.py
git commit -m "scripts: A/B of measured H at deck decisions on the eval seeds"
```

- [ ] **Step 6 (heavy; work Mac): run the A/B**

Choose `--rollout-seeds` and `--candidates` from Task 3's report (with `--rollout-seeds 3` and 2, keep the cheaper setting whose `top1` is within 0.02 of the other's), then on the work Mac (`~/sts2`, night job stopped):
```bash
PYTHONPATH=src uv run python scripts/deck_rollout_ab.py --model runs/a10/export --prior runs/prior --seeds 200 --workers 16 --rollout-seeds 3 --out deck_ab.json
```
Expected: `adopt measured H: True|False`. Report the mean floors, CI, and wins of both arms.

---

### Task 5: Paired fight records (`deckfights.variants`, `deckfights.pair_records`)

**Files:**
- Modify: `sts2/rl/run_train/deckfights.py`
- Test: `sts2/tests/python/test_run_train_deckfights.py`

**Interfaces:**
- Consumes: Task 1 (`fight_spec`, `fight_seed`, `post_fight_hp`, `Play`), `shards.state_view`, `shards.view_to_json`, `vocab.card_upgradable`, `vocab.card_removable`, `vocab.card_pools`.
- Produces:
  - `variants(view: RunView, rng: np.random.Generator, pool: Sequence[int]) -> list[tuple[str, RunView]]`
  - `pair_records(records: Sequence[dict], count: int, generation: int, *, budget: str, ascension: int, rng: np.random.Generator, play: Play) -> list[dict]` — RunRecord-shaped dicts (`seed`, `generation`, `feature_version`, `ascension`, `combat.budget`, `outcome.truncated`, `maps`, `fights`) that `train.fight_rows` reads unchanged
  - `NODE_GROUP = {1: "hallway", 2: "elite", 6: "boss"}`

- [ ] **Step 1: Write the failing tests** (append to `test_run_train_deckfights.py`)

```python
from rl.run_train import FEATURE_VERSION, shards, train, vocab  # noqa: E402


def run_record(seed: str = "T1", fights: int = 3) -> dict:
    view = make_view(ascension=10, relics=(deckfights.BURNING_BLOOD,))
    state = shards.view_to_json(view, 0)
    return {
        "seed": seed,
        "generation": 5,
        "feature_version": FEATURE_VERSION,
        "ascension": 10,
        "combat": {"budget": "120/500"},
        "outcome": {"truncated": False},
        "maps": [shards.map_to_json(view)],
        "fights": [
            {"floor": 3, "node_type": 1, "encounter": 13, "weak": True, "state": state,
             "hp_before": view.hp, "hp_after": view.hp - 5, "won": True}
        ] * fights,
    }


class VariantTests(unittest.TestCase):
    def test_upgrade_remove_and_add(self) -> None:
        view = make_view()
        pool = vocab.card_pools()["Ironclad"]
        found = dict(deckfights.variants(view, np.random.default_rng(0), pool))
        self.assertEqual(set(found), {"upgrade", "remove", "add"})
        self.assertEqual(sum(c.upgraded for c in found["upgrade"].deck), 1)
        self.assertEqual(len(found["remove"].deck), len(view.deck) - 1)
        self.assertEqual(len(found["add"].deck), len(view.deck) + 1)
        self.assertIn(found["add"].deck[-1].card_id, pool)

    def test_a_one_card_deck_is_never_emptied(self) -> None:
        view = make_view(deck=(CardRef(STRIKE, True),))
        names = [name for name, _ in deckfights.variants(view, np.random.default_rng(0), ())]
        self.assertEqual(names, [])  # nothing to upgrade, nothing removable, no pool


class PairRecordTests(unittest.TestCase):
    def test_pairs_share_a_seed_and_read_as_fightnet_rows(self) -> None:
        records = [run_record()]
        play = fake_play()
        pairs = deckfights.pair_records(
            records, 2, 6, budget="120/500", ascension=10,
            rng=np.random.default_rng(1), play=play,
        )
        self.assertEqual(len(pairs), 2)
        specs = play.calls[0]
        self.assertEqual(len({s["seed"] for s in specs[:4]}), 1)  # base and variants: one seed
        pair = pairs[0]
        self.assertEqual((pair["seed"], pair["generation"]), ("T1", 6))
        self.assertEqual(pair["fights"][0]["variant"], "base")
        # Lost 10 in the arena, Burning Blood heals 6: hp_after = hp - 4.
        first = pair["fights"][0]
        self.assertEqual(first["hp_before"] - first["hp_after"], 4)
        rows = train.fight_rows(pair)
        self.assertEqual(len(rows), len(pair["fights"]))

    def test_no_fights_no_pairs(self) -> None:
        pairs = deckfights.pair_records(
            [run_record(fights=0)], 5, 6, budget="120/500", ascension=10,
            rng=np.random.default_rng(1), play=fake_play(),
        )
        self.assertEqual(pairs, [])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_run_train_deckfights`
Expected: ERROR (`module 'rl.run_train.deckfights' has no attribute 'variants'`)

- [ ] **Step 3: Implement** (append to `deckfights.py`; add `from . import FEATURE_VERSION, shards, vocab` and `from .features import CardRef` to the imports)

```python
# A recorded fight's room kind (`fights[].node_type`), as the budget's room kind.
NODE_GROUP = {1: "hallway", 2: "elite", 6: "boss"}


def variants(
    view: RunView,
    rng: np.random.Generator,
    pool: Sequence[int],
) -> list[tuple[str, RunView]]:
    """One random upgrade, one random removal and one card from `pool` added -- each only
    where the deck allows it (never an empty deck)."""
    out: list[tuple[str, RunView]] = []
    deck = view.deck
    upgradable = [i for i, c in enumerate(deck) if vocab.card_upgradable(c.card_id, c.upgraded)]
    if upgradable:
        i = int(rng.choice(upgradable))
        changed = list(deck)
        changed[i] = dataclasses.replace(deck[i], upgraded=True)
        out.append(("upgrade", dataclasses.replace(view, deck=tuple(changed))))
    removable = [i for i, c in enumerate(deck) if vocab.card_removable(c.card_id)]
    if removable and len(deck) > 1:
        i = int(rng.choice(removable))
        out.append(("remove", dataclasses.replace(view, deck=deck[:i] + deck[i + 1 :])))
    if len(pool):
        card = CardRef(int(rng.choice(pool)), False)
        out.append(("add", dataclasses.replace(view, deck=(*deck, card))))
    return out


def pair_records(
    records: Sequence[dict[str, Any]],
    count: int,
    generation: int,
    *,
    budget: str,
    ascension: int,
    rng: np.random.Generator,
    play: Play,
) -> list[dict[str, Any]]:
    """`count` recorded fight starts, each played as itself and its `variants` on one seed.

    Each pair is a record `train.fight_rows` reads like a run's: the variant's state at
    fight start, its encounter and weak flag, and HP before and after (post-fight heals
    counted). A fight whose result is missing is left out of its pair.
    """
    starts = [
        (record, fight)
        for record in records
        if not record["outcome"]["truncated"]
        for fight in record.get("fights") or ()
        if fight.get("node_type") in NODE_GROUP
    ]
    if not starts or count <= 0:
        return []
    picked = rng.choice(len(starts), size=min(count, len(starts)), replace=False)
    pool = vocab.card_pools()["Ironclad"]
    plans = []
    specs: list[dict[str, Any]] = []
    for n, s in enumerate(sorted(int(p) for p in picked)):
        record, fight = starts[s]
        base = shards.state_view(record, fight)
        seed = fight_seed(generation, n, 0)
        views = [("base", base), *variants(base, rng, pool)]
        first = len(specs)
        group = NODE_GROUP[fight["node_type"]]
        for name, view in views:
            specs.append(fight_spec(view, int(fight["encounter"]), group, seed, len(specs), budget))
        plans.append((record, fight, views, first))
    results = play(specs)
    pairs = []
    for record, fight, views, first in plans:
        fights = []
        for k, (name, view) in enumerate(views):
            result = results[first + k]
            if result is None:
                continue
            fights.append(
                {
                    "floor": fight["floor"],
                    "node_type": fight["node_type"],
                    "encounter": int(fight["encounter"]),
                    "weak": bool(fight["weak"]),
                    "state": shards.view_to_json(view, 0),
                    "hp_before": view.hp,
                    "hp_after": post_fight_hp(view.relics, result),
                    "won": bool(result["won"]),
                    "variant": name,
                },
            )
        if fights:
            pairs.append(
                {
                    "seed": record["seed"],
                    "generation": generation,
                    "feature_version": FEATURE_VERSION,
                    "ascension": ascension,
                    "combat": {"budget": budget},
                    "outcome": {"truncated": False},
                    "maps": [record["maps"][fight["state"]["map"]]],
                    "fights": fights,
                },
            )
    return pairs
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_run_train_deckfights`
Expected: OK (11 tests)

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/git/sts2 && git add rl/run_train/deckfights.py tests/python/test_run_train_deckfights.py
git commit -m "deckfights: paired fight records -- a recorded start and its variants on one seed"
```

---

### Task 6: The `pair` phase and paired fights in training

**Files:**
- Modify: `sts2/rl/run_train/loop.py` (`LoopConfig` `:104-168`, `next_phase` `:343-354`, `step` `:364-377`, `_train` `:450-476`, `Loop.__init__` `:290-326`)
- Modify: `sts2/rl/run_train/train.py` (`load_buffers` `:345-391`, `fit` `:621-...`)
- Test: `sts2/tests/python/test_run_train_loop.py`, `sts2/tests/python/test_run_train_train.py` (the `load_buffers` tests; it already imports `tempfile`, `Path`, `evaluate`, `shards`, `train`, `make_view`)

**Interfaces:**
- Consumes: Task 5's `pair_records`, Task 1's `fight_player`, `RolloutConfig`.
- Produces:
  - `LoopConfig.paired_fights: int = 0` (stored under `loop` in config.json; a `start --paired-fights N` option, generated from the field)
  - `loop.PAIRS = "pairs"` (the folder), `loop.PAIRS_MANIFEST = "pairs/manifest.json"`
  - `Loop(..., pair_play: deckfights.Play | None = None)`
  - `train.load_buffers(shards, *, pairs=(), generation, combat_budget, ascension, config)` and `train.fit(shards, prev, out, *, pairs=(), generation, ...)`

- [ ] **Step 1: Write the failing loop tests** (append to `test_run_train_loop.py`; update `fake_fit` there to accept `pairs=()` and record it in `calls` as `"pairs": [Path(p) for p in pairs]`)

```python
class PairPhaseTests(LoopTestCase):
    def paired(self, count: int = 4) -> loop.Loop:
        config = dataclasses.replace(CONFIG, paired_fights=count)
        loop.write_config(self.root, config)
        return loop.Loop(
            self.root, config, play=fake_play,
            fit=functools.partial(fake_fit, strength=3, calls=self.calls),
            eval_seeds=EVAL_SEEDS,
            pair_play=lambda specs: [None] * len(specs),
        )

    def test_no_pair_phase_by_default(self) -> None:
        run = self.make_loop()
        self.through(run, "train")
        self.assertFalse((self.gen(0) / loop.PAIRS_MANIFEST).exists())

    def test_the_pair_phase_runs_between_generate_and_train(self) -> None:
        run = self.paired()
        self.through(run, "pair")
        run.step()
        self.assertEqual(run.next_phase(0), "train")
        manifest = self.read(self.gen(0) / loop.PAIRS_MANIFEST)
        self.assertEqual(manifest["requested"], 4)
        run.step()  # train
        self.assertEqual(self.calls[-1]["pairs"], [])  # fake records hold no fights

    def test_a_trained_generation_never_gets_a_pair_phase(self) -> None:
        run = self.make_loop()
        self.through(run, "evaluate")  # generation 0 trained without pairs
        later = self.paired()
        self.assertEqual(later.next_phase(0), "evaluate")
```

Add `import dataclasses` to the test file's imports if it is missing.

- [ ] **Step 2: Write the failing training tests** (append to `test_run_train_train.py`)

```python
class PairBufferTests(unittest.TestCase):
    def test_pairs_add_fightnet_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pair = deckfights_pair_record("T1")  # helper below
            path = Path(tmp) / "p0000.jsonl.gz"
            shards.write_shard(path, [pair])
            fights, _ = train.load_buffers(
                [], pairs=[path], generation=6, combat_budget="120/500", ascension=10,
            )
            self.assertEqual(len(fights.labels), len(pair["fights"]))

    def test_pairs_of_a_reserved_seed_are_refused(self) -> None:
        reserved = evaluate.load_seeds(evaluate.EVAL_SEEDS)[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p0000.jsonl.gz"
            shards.write_shard(path, [deckfights_pair_record(reserved)])
            with self.assertRaises(ValueError):
                train.load_buffers(
                    [], pairs=[path], generation=6, combat_budget="120/500", ascension=10,
                )


def deckfights_pair_record(seed: str) -> dict:
    view = make_view(ascension=10)
    state = shards.view_to_json(view, 0)
    return {
        "seed": seed, "generation": 6, "feature_version": train.FEATURE_VERSION,
        "ascension": 10, "combat": {"budget": "120/500"}, "outcome": {"truncated": False},
        "maps": [shards.map_to_json(view)],
        "fights": [
            {"floor": 3, "node_type": 1, "encounter": 13, "weak": True, "state": state,
             "hp_before": 50, "hp_after": 44, "won": True, "variant": name}
            for name in ("base", "upgrade")
        ],
    }
```

(`FightData.labels` is the label array, `train.py:257`.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_run_train_loop tests.python.test_run_train_train -k Pair`
Expected: FAIL/ERROR (`unexpected keyword argument 'paired_fights'` / `'pairs'`)

- [ ] **Step 4: Implement in `train.py`**

In `load_buffers`, add the keyword parameter `pairs: Iterable[Path | str] = ()` after `shards`, and after the shards loop (before `return`) add:

```python
    for order, path in enumerate(pairs):
        for line, record in enumerate(read_shard(Path(path))):
            seed = canonicalize(record["seed"])
            if seed in reserved:
                raise ValueError(
                    f"{path}: pairs of run {record['seed']!r} are seed {seed} of "
                    f"{reserved[seed]}, which is held out of training (DESIGN.md 5.6)",
                )
            played = record.get("generation")
            if played is None or played > generation or not _in_era(record, budget, ascension):
                continue
            if rows := fight_rows(record):
                # After the generation's own runs: the newest fights of that generation.
                key = (played, 1_000_000 + order, line)
                fights.append(_FightChunk.of(key, record["seed"], rows))
```

Update its docstring: `` `pairs` are paired fight records (`deckfights.pair_records`): FightNet rows only. ``

In `fit`, add the keyword parameter `pairs: Iterable[Path | str] = ()` and pass `pairs=pairs` to its `load_buffers` call.

- [ ] **Step 5: Implement in `loop.py`**

Add constants beside `MANIFEST`:

```python
PAIRS = "pairs"
PAIRS_MANIFEST = f"{PAIRS}/manifest.json"
PAIR_GLOB = "p*.jsonl.gz"
```

In `LoopConfig`, add the field `paired_fights: int = 0` (after `warm_start`), validate in `__post_init__` (`if self.paired_fights < 0: raise ValueError(f"paired_fights must be at least 0, not {self.paired_fights}")`), and add `"paired_fights": self.paired_fights` to `to_json`'s `loop` dict.

In `Loop.__init__`, add the keyword parameter `pair_play: deckfights.Play | None = None` stored as `self.pair_play`, and `from . import deckfights` to the imports.

Replace `next_phase` with:

```python
    def next_phase(self, n: int) -> str:
        """Return what generation `n` does next: its first phase whose output is missing.

        A `pair` phase comes between generate and train only when the job asks for paired
        fights and the generation has not trained yet: one trained before pairs existed
        is never sent back to make them.
        """
        gen = self.gen_dir(n)
        phases = [(MANIFEST, "generate")]
        if self.config.paired_fights > 0 and not (gen / LEARNER_NPZ).exists():
            phases.append((PAIRS_MANIFEST, "pair"))
        phases += [(LEARNER_NPZ, "train"), (EVAL, "evaluate"), (PROMOTION, "promote")]
        for output, phase in phases:
            if not (gen / output).exists():
                return phase
        return "done"
```

In `step`, add `"pair": self._pair,` to the phase table.

Add the phase method after `_generate`:

```python
    def _pair(self, n: int) -> None:
        """Play paired fights from this generation's runs for FightNet (deck-decision spec 4)."""
        self.write_status("pairing")
        records = [
            record
            for path in shards.shard_paths(self.gen_dir(n) / "shards")
            for record in shards.read_shard(path)
        ]
        job = self.config.job
        play = self.pair_play or deckfights.fight_player(
            deckfights.RolloutConfig(
                budget=str(job.combat_budget),
                workers=job.workers,
                prior=job.combat_prior,
                ascension=job.ascension,
            ),
        )
        started = time.monotonic()
        pairs = deckfights.pair_records(
            records,
            self.config.paired_fights,
            n,
            budget=str(job.combat_budget),
            ascension=job.ascension,
            rng=np.random.default_rng(n),
            play=play,
        )
        folder = self.gen_dir(n) / PAIRS
        folder.mkdir(parents=True, exist_ok=True)
        if pairs:
            shards.write_shard(folder / "p0000.jsonl.gz", pairs)
        shards.write_json(
            self.gen_dir(n) / PAIRS_MANIFEST,
            {
                "requested": self.config.paired_fights,
                "pairs": len(pairs),
                "fights": sum(len(p["fights"]) for p in pairs),
                "seconds": round(time.monotonic() - started, 1),
                "written": _now(),
            },
        )
        log.info("gen %d: %d paired fight records", n, len(pairs))
```

(Add `import numpy as np` to `loop.py`'s imports if it is not there.)

In `_train`, after building `paths`, add:

```python
        pair_paths = [
            path
            for g in self.generations()
            if g <= n
            for path in sorted((self.gen_dir(g) / PAIRS).glob(PAIR_GLOB))
        ]
```

and pass `pairs=pair_paths` to `self.fit(...)`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_run_train_loop tests.python.test_run_train_train tests.python.test_run_train_deckfights`
Expected: OK

- [ ] **Step 7: Commit**

```bash
cd ~/Documents/git/sts2 && git add rl/run_train/loop.py rl/run_train/train.py tests/python/test_run_train_loop.py tests/python/test_run_train_train.py
git commit -m "run_train: a pair phase of paired fights, which FightNet trains on"
```

---

### Task 7: The work Mac makes and syncs pairs

**Files:**
- Modify: `sts2/scripts/night_train.sh` (`sync_once` `:44-49`, the `start` branch `:73-83`, the header comment `:1-21`)

**Interfaces:**
- Consumes: Task 6's `--paired-fights` start option and the `gen_XXXX/pairs/` folder.

- [ ] **Step 1: Pass the setting on start**

Beside `LR="${LR:-0.0005}"` add:

```bash
# Paired fights per generation for FightNet (deck-decision spec 4): each a recorded fight
# start played as itself and with one upgrade, removal and added card, on one seed.
PAIRED_FIGHTS="${PAIRED_FIGHTS:-4000}"
```

and add `--paired-fights "$PAIRED_FIGHTS"` to both `train start` command lines (after `--lr "$LR"`).

- [ ] **Step 2: Sync the pair files back**

In `sync_once`, add `--include='p*.jsonl.gz'` after `--include='r*.jsonl.gz'`.

- [ ] **Step 3: Document the override**

In the header's "Overrides:" sentence add `PAIRED_FIGHTS (default 4000; 0 makes none),`.

- [ ] **Step 4: Check the script parses**

Run: `bash -n ~/Documents/git/sts2/scripts/night_train.sh && echo ok`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/git/sts2 && git add scripts/night_train.sh
git commit -m "night_train: 4000 paired fights a generation, synced back with the shards"
```

---

### Task 8: Live wiring — the engine service and the autopilot

**Files:**
- Modify: `sts2/src/sts2_gym/live/strategy.py` (`_training_package` `:89-120`)
- Test: `sts2/tests/python/test_live_service.py` (`StrategyTests`, `FAKE_POLICY`)
- Modify: `spire-coach/coach/cli.py` (the autopilot command's `EngineClient(... extra_env=...)` at `:507-514`, its parser near `_add_prior_option` `:1155`)
- Test: `spire-coach/tests/test_cli.py`

**Interfaces:**
- Consumes: Task 2's `score_options(..., rollouts=)`, Task 1's `RolloutConfig`.
- Produces: environment `STS2_DECK_ROLLOUTS=1` (on), `STS2_DECK_ROLLOUT_SEEDS`, `STS2_DECK_ROLLOUT_CANDIDATES`, `STS2_DECK_ROLLOUT_WORKERS`, `STS2_DECK_ROLLOUT_PRIOR`; autopilot flag `--deck-rollouts`.

- [ ] **Step 1: Write the failing service tests** (in `test_live_service.py`: change `FAKE_POLICY`'s `score_options(decision, view, model=None)` to `score_options(decision, view, model=None, rollouts=None)` and record `rollouts` in `CALLS` as a fourth element; update the existing `CALLS` assertion to expect `None` there)

```python
    def test_rollouts_are_off_unless_the_environment_turns_them_on(self) -> None:
        champion = model_dir(self.root, generation=3)
        with self.importable(self.training_package(FAKE_POLICY)), mock.patch.dict(
            os.environ, {}, clear=False,
        ):
            os.environ.pop("STS2_DECK_ROLLOUTS", None)
            features, score = strategy._training_package(champion, {"generation": 3})
            score(features.Decision("card_reward", {}), {})
            policy = sys.modules["run_train.policy"]
            self.assertIsNone(policy.CALLS[-1][3])

    def test_the_environment_turns_rollouts_on(self) -> None:
        champion = model_dir(self.root, generation=3)
        env = {"STS2_DECK_ROLLOUTS": "1", "STS2_DECK_ROLLOUT_SEEDS": "2",
               "STS2_DECK_ROLLOUT_PRIOR": "/prior"}
        with self.importable(self.training_package(FAKE_POLICY)), mock.patch.dict(os.environ, env):
            features, score = strategy._training_package(champion, {"generation": 3})
            score(features.Decision("card_reward", {}), {})
            rollouts = sys.modules["run_train.policy"].CALLS[-1][3]
            self.assertEqual((rollouts.seeds, rollouts.prior), (2, "/prior"))
```

`_training_package` will import `run_train.deckfights` for `RolloutConfig`; add a stand-in to the fake package: in `training_package()`, also write `(package / "deckfights.py")` with:

```python
FAKE_DECKFIGHTS = '''"""A stand-in for run_train.deckfights."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RolloutConfig:
    seeds: int = 3
    budget: str = "120/500"
    candidates: int = 4
    workers: int = 1
    prior: str = ""
    ascension: int = 10
'''
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_live_service -k rollouts`
Expected: FAIL (`CALLS[-1]` has 3 elements / no rollouts)

- [ ] **Step 3: Implement in `strategy.py`**

In `_training_package`, after `champion = policy.load(model_dir)`:

```python
    rollouts = _rollouts_from_environment()
    _LOG.info(
        "strategy champion generation %s loaded from %s%s",
        info["generation"],
        model_dir,
        "" if rollouts is None else f"; deck screens with measured fights ({rollouts})",
    )
    return features, functools.partial(
        policy.score_options, model=champion, rollouts=rollouts,
    )
```

(replacing the existing `_LOG.info(...)` and `return`), and add below it:

```python
def _rollouts_from_environment() -> Any:
    """Measured H at deck decisions, when `STS2_DECK_ROLLOUTS=1` turns it on (else None).

    The autopilot sets these with `--deck-rollouts`: seeds, candidates and workers per
    deck decision, and the combat prior folder the fights are searched with.
    """
    if os.environ.get("STS2_DECK_ROLLOUTS") != "1":
        return None
    deckfights = importlib.import_module("run_train.deckfights")
    env = os.environ.get
    return deckfights.RolloutConfig(
        seeds=int(env("STS2_DECK_ROLLOUT_SEEDS", "3")),
        candidates=int(env("STS2_DECK_ROLLOUT_CANDIDATES", "4")),
        workers=int(env("STS2_DECK_ROLLOUT_WORKERS", "4")),
        prior=env("STS2_DECK_ROLLOUT_PRIOR", ""),
        ascension=10,
    )
```

- [ ] **Step 4: Run the service tests to verify they pass**

Run: `cd ~/Documents/git/sts2 && PYTHONPATH=src uv run python -m unittest tests.python.test_live_service`
Expected: OK

- [ ] **Step 5: Commit (sts2)**

```bash
cd ~/Documents/git/sts2 && git add src/sts2_gym/live/strategy.py tests/python/test_live_service.py
git commit -m "live strategy: measured H at deck decisions when STS2_DECK_ROLLOUTS=1"
```

- [ ] **Step 6: Write the failing autopilot test** (append to spire-coach `tests/test_cli.py`; it tests the new environment helper directly, so it needs no engine)

```python
def test_deck_rollouts_reach_the_engine_environment():
    from coach import cli

    env = cli.autopilot_engine_env(deck_rollouts=True, prior_dir=Path("/prior"))
    assert env["STS2_DECK_ROLLOUTS"] == "1"
    assert env["STS2_DECK_ROLLOUT_PRIOR"] == "/prior"
    assert "STS2_STRATEGY_MODEL_DIR" in env
    plain = cli.autopilot_engine_env(deck_rollouts=False, prior_dir=None)
    assert "STS2_DECK_ROLLOUTS" not in plain
```

- [ ] **Step 7: Run it to verify it fails**

Run: `cd ~/Documents/git/spire-coach && uv run pytest -q tests/test_cli.py -k deck_rollouts -p no:cacheprovider`
Expected: FAIL (`module 'coach.cli' has no attribute 'autopilot_engine_env'`)

- [ ] **Step 8: Implement in `coach/cli.py`**

Add the helper near the autopilot command:

```python
def autopilot_engine_env(*, deck_rollouts: bool, prior_dir: Path | None) -> dict[str, str]:
    """The engine service's environment for the autopilot: the full-run champion, and
    measured fights at deck decisions when asked (sts2 `deckfights`)."""
    env = {"STS2_STRATEGY_MODEL_DIR": str(autopilot.FULL_RUN_MODEL_DIR)}
    if deck_rollouts:
        env["STS2_DECK_ROLLOUTS"] = "1"
        env["STS2_DECK_ROLLOUT_PRIOR"] = str(prior_dir or "")
    return env
```

Replace `extra_env={"STS2_STRATEGY_MODEL_DIR": str(autopilot.FULL_RUN_MODEL_DIR)},` with `extra_env=autopilot_engine_env(deck_rollouts=deck_rollouts, prior_dir=prior_dir),`, thread a `deck_rollouts: bool = False` parameter from the autopilot command function down to that call, and add the option to the autopilot parser:

```python
    autopilot_parser.add_argument(
        "--deck-rollouts",
        action="store_true",
        help="measure deck decisions with paired simulated fights (slower; sts2 deckfights)",
    )
```

passing `deck_rollouts=args.deck_rollouts` where the command is dispatched (beside `prior=args.prior`). Use the parser variable name the file already uses for the autopilot subcommand.

- [ ] **Step 9: Run the spire-coach tests to verify they pass**

Run: `cd ~/Documents/git/spire-coach && uv run pytest -q tests/test_cli.py -p no:cacheprovider`
Expected: all pass

- [ ] **Step 10: Commit (spire-coach)**

```bash
cd ~/Documents/git/spire-coach && git add coach/cli.py tests/test_cli.py
git commit -m "autopilot: --deck-rollouts measures deck decisions with paired fights"
```

---

## Measurement checkpoints (from the spec's order)

1. After Task 3 Step 7: baseline `top1`, `spearman`, `value_lost`, and the arena-vs-FightNet offset. Report before continuing past Task 4's heavy step.
2. After Task 4 Step 6: the A/B verdict. If adopted, the autopilot runs with `--deck-rollouts`; if not, report why (cost, noise) before spending on it.
3. After Task 7 is live on the work Mac for three generations: `deck_benchmark.py report` on each new champion, and the eval floors, against the baseline.
