# Deck decisions from measured fights — design

Date: 2026-10-05. Status: approved in conversation; this is the written spec.

## Goal

The full-run strategy model should tell good deck changes from weak ones — upgrade Bash
or Strike, which card to take, which card to remove — so that runs go further and win
more. The aim is the highest A10 win rate; compute and time are not constraints.

## What exists, and why it falls short

- `rl/run_train` scores every option by the value of its afterstate (`policy.floor_probs`).
  The value net V reads the encoded view plus a fight block **H**: for the region's weak,
  normal and elite pools and the boss, the expected HP lost and the death chance
  (`features.fight_queries`, `features.fight_blocks`, 8 numbers).
- H comes from **FightNet** (`models.py`), which predicts a fight's outcome class (death,
  or HP lost in buckets) from the view at fight start. The deck encoding separates base
  and upgraded copies (block E), so FightNet can see an upgrade.
- FightNet learns only from fights self-play happened to play (`train.fight_rows`): each
  fight once, with the deck that was there. One upgrade moves a fight's HP loss by a few
  points; fight-to-fight noise is far larger. So small deck changes barely move H.
  Live (2026-10-05): rest-site Smith options differ by 0.04–0.5 floors; the model upgrades
  Strikes about as often as anything else.
- Recorded fights are replayable exactly: `real_fights.fight_spec` turns a recorded fight
  start into a spec `loop.evaluate_fights` plays with the fight search and prior at any
  budget, and a spec's `seed` fixes the fight's randomness.

## Design

### 1. Paired fight simulation (shared building block)

`rl/run_train/deckfights.py`. Given a view and a list of candidate afterstates (views
after a deck change), play the same upcoming fights for each candidate on the **same
seeds** (common random numbers), and return, per candidate, H as `features.fight_blocks`
lays it out.

- Fights: every encounter of `features.fight_queries(view)` — the set H already averages
  over — times `seeds` seeds each. Seed `k` of encounter `e` is derived from a decision
  key, `e` and `k`, identical for every candidate.
- A fight spec is built from the candidate view the way `fight_spec` builds one from a
  recorded state (deck as signed ids, enchantments, relics, potions, HP, max HP, floor,
  act, potion slots), with the query's encounter and weak flag.
- Played by `loop.evaluate_fights` with a configurable budget (default the job's
  training budget, `120/500`) and the job's combat prior.
- Settings (`RolloutConfig`): `seeds` (default 3), `budget`, `candidates` (score at most
  this many options by measured H, the model's top ones plus every unchanged option such
  as skip or leave; default 4), `workers`.
- Death counts as losing all HP brought, as `models.expected_loss` treats it.

### 2. Deck-decision test set (built first)

`scripts/deck_benchmark.py` plus a frozen set `rl/run_train/deck_benchmark.json`.

- 500 deck decisions drawn with a fixed seed from recent A10 shards: rest-site Smith,
  card rewards (take or skip), shop card buys and removals, and card-select upgrade or
  remove screens, each with at least two scorable deck options. Stored as the shard
  state, the decision and its options, like `eval_seeds.json` stores seeds.
- Truth, computed once and cached beside it with its budget, seeds and prior hash: every
  option's value from V with **measured** H (the paired simulation at `seeds = 8`).
- Report for any model: top-1 agreement with the truth, Spearman rank correlation per
  decision (mean), and the mean value lost by the model's pick against the truth's best,
  overall and per decision kind.

### 3. Decision-time measured H

A switch on scoring: for a screen whose options change the deck (`card.take`,
`card.skip`, `rest.smith`, `select.upgrade`, `select.remove`, `select.transform`,
`shop.card`, `shop.remove`, `reward.card`), the shortlisted options are scored by V with H
from the paired simulation instead of from FightNet. Every other screen, and options
outside the shortlist, are scored as today.

- `policy.score_options(..., rollouts=RolloutConfig | None)` and the same on
  `LookaheadPolicy`; `None` is today's behaviour.
- Inside an eval or self-play worker the fights run in that process (pools never nest),
  so a deck decision costs roughly candidates x encounters x seeds fights. Step 1
  measures that cost; the A/B's settings are chosen from it.
- **A/B on the simulator**: the champion with and without rollouts on the same eval
  seeds (the first 200 of the 1,000 by default, `--seeds`), compared by
  `evaluate.compare` (paired CI on mean floor; wins reported). Adopted only if the CI on
  mean floor is above 0.
- Live: the engine service turns it on with `STS2_DECK_ROLLOUTS=1` (settings by
  environment); the autopilot passes `--deck-rollouts`. Fights run in the service's own
  worker pool.
- Self-play: off by default; a job setting turns it on once the A/B says it pays for the
  slower generations.

### 4. Paired fights in training

Each generation also produces paired fight rows for FightNet.

- Sample `paired_fights` recorded fight starts (default 2,000) from the generation's
  shards. For each, a base and up to three variants: one random upgradable card
  upgraded, one random removable card removed, one card from the character's reward pool
  added. All are played on the same seed at the training budget with the prior.
- Each played fight becomes a FightNet row exactly like `train.fight_rows`: the variant's
  encoded view at fight start, the encounter, the weak flag, and the outcome class.
- Written to `gen_XXXX/pairs/p*.jsonl.gz` (synced to the Mac with the shards); loaded by
  `train.load_buffers` beside the shards' fights, weighted by `paired_weight`
  (default 1.0).
- Job settings `paired_fights` and `paired_weight` are runtime fields, so a running job
  can start using them; `paired_fights = 0` is today's behaviour.

## Order and measurement

1. Paired simulation and the test set. Report the champion's agreement today.
2. Decision-time measured H, and its A/B on the eval seeds.
3. Paired training data. Report agreement and eval floors for the first generations
   trained with it.
4. Live wiring of whatever the A/B and the test set support.

Each step reports its effect before the next starts.

## Success

- Test-set agreement (top-1, rank correlation) rises above the champion's baseline.
- On the eval seeds: mean floor up with a paired CI above 0; win rate up.
- Live: later floors than the batches before it.

## Error handling

- A fight that times out or crashes is dropped from its average (the generator's
  existing quarantine); a candidate missing more than a quarter of its fights falls back
  to FightNet's H for that decision, logged.
- The cached truth is invalid when its budget, seeds or prior hash differ from the
  current settings; the benchmark recomputes it.

## Not in scope

- Changing the fight search, the prior loop, or the simulator's C#.
- New features in the encoding. If measured H shows FightNet cannot represent a deck
  effect at all, that is a follow-up.
