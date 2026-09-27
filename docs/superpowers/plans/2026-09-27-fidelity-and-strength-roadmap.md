# Roadmap: a faithful simulator, and a model that wins 85–90% at Ascension 10

**Goal:** an Ironclad agent that wins 85–90% of real Ascension 10 runs. That is close to the ceiling that near-perfect play allows: some seeds cannot be won without knowing the future. Three things must all hold:

1. **The simulator is the real game.** Otherwise the model learns a different game, and its skill does not transfer.
2. **The agent is strong enough.** Search, networks and training scale.
3. **It is proven on the real game.** Win rate is measured on real runs, not only in the simulator.

Workstreams A–D make the simulator faithful, E makes the agent strong, and F proves it. Each task lists its acceptance test. A task is done when its test passes, not when its code is written.

## Where we stand (2026-09-27)

**Coverage and live checks**

| Area | Status |
|---|---|
| Enemies | 111 of 121 game classes modelled. The real gaps are Mysterious Knight and the Decimillipede segments; the rest are test dummies. Six more enemies are not yet modelled in the live converter: their behaviour branches on buffs or chance, which `Cycle` cannot express. |
| Events | 65 of 68. The Architect is missing; the other two are deprecated. |
| Live combat checks from one real Act 1–3 session: the real game's move vs the simulator's prediction | Act 1: 46 of 62 moves comparable, 46 of 46 matching. Act 2: 8 of 69 comparable, 8 of 8 matching. Act 3: 48 of 129 comparable, 47 of 48 matching; the one mismatch is Sparkling Rouge's Dexterity. |
| Why moves are "not comparable" | Power fields the mod does not send (Skittish, Surrounded, Possess*), unhandled random fields, and remaining converter gaps. |

**The run layer**
- A10 is not modelled: the run engine covers 0–9, and A9 stands in for A10. The difference is A10's double boss in the last act.
- The run-level trace checks pass (32 of 32), but they come from Act 1 captures only.

**Known faults**
- Swift enchants only Powers, unlike the game.
- An intermittent native segfault hits about 1 run in 200.
- One gap in the table of buffs remains.

**Agent**
- The Act 1 strategy model beats the Act 1 boss 68–74% of the time, with fights played by search.
- Full-run (Acts 1–3) training has started.
- Fight learning (stage 3) is being built.

## A. Measurement: know how faithful the simulator is

- **A1. Fidelity scoreboard** (M). One command runs every check over every captured session. It reports:
  - comparable and matching moves, per act and per category (enemy, card, relic, potion, power, reward, shop, event, map);
  - the trend across sessions;
  - the same numbers on the dashboard.

  *Acceptance:* one command reproduces the table above from the logs, and the dashboard shows it.
- **A2. Capture what the checks need** (M; touches the mod). Extend the mod and the snapshot schema:
  - the missing power fields (Skittish, Surrounded, Possess*, Swipe's stolen card);
  - the run seed and, if the game exposes them, its RNG counters;
  - full reward, shop and event screens, including options not taken.

  Recording turns on automatically whenever the coach runs. *Acceptance:* no check is "not comparable" because a field is missing.
- **A3. Seed-exact run replay** (L). From a real session (seed plus choices), replay the run in the simulator and diff every offering: map, encounters, card rewards, relic and potion offers, shop stock and prices, event options and outcomes, Ancients, boss relics. This extends `check_run_traces` to real Act 1–3 sessions. *Acceptance:* a real session replays with every difference listed; clean runs show zero differences.

## B. Combat fidelity

- **B1. Make Acts 2–3 comparable** (M). Close the causes that A1 and A2 surface: converter tables, power fields, and how random outcomes are handled. *Acceptance:* at least 90% of real moves comparable in every act.
- **B2. Model the remaining enemies and events** (M–L):
  - Extend the enemy-behaviour machinery for behaviour that branches on buffs or chance, then model the six missing enemies (Test Subject first), Mysterious Knight and the Decimillipede segments.
  - Add The Architect event.

  *Acceptance:* every live-seen enemy is modelled, and each is checked against the decompiled source and a real capture.
- **B3. Fix known deviations** (S): Sparkling Rouge's Dexterity, Swift's enchant scope, and the gap in the buff table. *Acceptance:* each has a regression test and matches the game.
- **B4. Audit every card, relic, potion and power** (L). Build audits like `audit_enemy_moves.py` that compare the simulator with the decompiled source: numbers, triggers, timing, targeting and ordering. Fix the work lists they produce. *Acceptance:* zero open flags for the current game version.
- **B5. Root-cause the intermittent segfault** (M). Reproduce it under stress, get a native stack, and fix it at the cause. A memory fault can also corrupt results silently. *Acceptance:* 10,000 or more runs with no native crash.
- **B6. Zero mismatches at scale** (ongoing). *Acceptance:* no deterministic mismatch in 10,000 or more comparable real moves across all acts at A10. Autopilot captures (F3) provide the volume.

## C. Run-level fidelity: rewards, relics, shops, events, map

- **C1. Every run-level random roll matches the game's seeded RNG** (L):
  - card rewards, including rarity and upgrade odds and their pity counters;
  - relic pools and tiers, and potion drops;
  - shop stock, prices and removal cost;
  - event selection and outcomes, encounter selection, map generation, and the Neow and Ancient offers.

  *Acceptance:* A3 shows zero differences on at least 20 real full runs.
- **C2. All events, Acts 1–3** (M). Multi-step events, their outcome odds, and the Crystal Sphere and Fake Merchant screens, all checked against real captures. *Acceptance:* every event seen in captures replays exactly.
- **C3. Ascension 10 in the run engine** (M). Every ascension modifier from A1 to A10, including the final double boss. Then drop A9-as-a-stand-in: training moves to real A10, which starts a new data era. *Acceptance:* real A10 runs replay exactly under A3.
- **C4. Act transitions** (S–M). Boss relic choices, the Ancients at the start of each act, healing between acts, and the Act 3 final boss or bosses. *Acceptance:* checked against real Act 2 and Act 3 transitions.

## D. Keeping up with the game

- **D1. Game-update pipeline** (M). When the game updates:
  1. decompile, and regenerate the data tables (`diff_patch.py`);
  2. run every audit (B4) and the scoreboard (A1) on the latest captures;
  3. check that the mod still works;
  4. list the regressions.

  Retrain when the rules change materially. *Acceptance:* a game update produces a regression report without manual steps.

## E. Agent strength

- **E1. Fight learning (stage 3)** (L; in progress). A learned prior and value guide the fight search. *Acceptance:* at the same budget, on the same held-out fights, less HP lost and fewer deaths.
- **E2. Use the fight network everywhere** (M). Inside full-run self-play (§6.5 of the training design) and in the coach's live fight advice. *Acceptance:* the win rate rises at equal compute.
- **E3. Train the full-run model to convergence on the verified simulator** (L; mostly compute):
  - Scale the network and features when learning plateaus.
  - Promote on at least 1,000 test seeds, which removes the noise and the "lucky champion" effect we saw with 200.
  - Retrain whenever B or C changes the game materially.

  *Acceptance:* a stable plateau measured on 1,000 seeds.
- **E4. Search at play time** (M). The autopilot can afford far bigger budgets than training. Add better handling of hidden information (sampled draw orders and enemy intents), and rollouts at the run level for pivotal picks. *Acceptance:* a measurable gain in win rate over E3's policy at the same seeds.
- **E5. Other ascensions and characters** (later). Only after Ironclad at A10 is strong.

## F. Proof on the real game

- **F1. Autopilot** (L; the project's original goal a). It plays real runs through the mod on the modded profile, with no hidden information, turning the advice into actions, and runs unattended. *Acceptance:* it completes 20 real runs without getting stuck.
- **F2. Real win-rate measurement** (M). Autopilot runs at A10:

  | Runs | Resolution |
  |---|---|
  | 100 | ±7–10 points |
  | 400 | ±4–5 points |

  Compare with the simulator's prediction for the same policy: the gap between the two is the final fidelity metric. *Acceptance:* at least 400 real runs, a 95% CI reported, and the gap known.
- **F3. The capture flywheel** (S once F1 exists). Every autopilot run is captured and fed into A1, A3 and B6. Fidelity data then grows by hundreds of runs a week without anyone playing.

## Order and milestones

| Milestone | Tasks | What it buys |
|---|---|---|
| M1: foundation | A1, A2, B1, B3, B5; E1 continues | The numbers, the data, and combat that can be compared in every act |
| M2: faithful runs | A3, C1–C4, B2, B4 | Seed-exact rewards, relics, shops, events and maps, plus real A10 |
| M3: real play | F1, F3, E2, E3 restarted on the M2 simulator | The autopilot, capture at scale, and the strongest training |
| M4: proof | F2, E4, B6, D1 | A real A10 win rate, and staying correct across game updates |

**Honest expectations:**
- **M1–M2** make the simulator's win rate trustworthy.
- **M3** is where the win rate should climb most, through fight learning on a faithful game.
- **85–90%** is the ceiling estimate. Reaching it likely needs E4's bigger play-time search and a larger model trained for a long time on this Mac.
- **F2 tells us how far off we are.**
- **Size:** several weeks of agent work in total, with M2 and E the largest parts. Every milestone improves the coach along the way.
