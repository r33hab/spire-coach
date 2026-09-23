# Spire Coach — Design

- **Date:** 2026-09-23
- **Status:** Draft, awaiting review
- **Owner:** Kristoffer
- **Plan scope:** this spec covers the whole system. Implementation plans are written per stage, starting with stages 0–1. Stage 1 may be split into sub-plans: 1a mod and capture, 1b snapshot loader and gates, 1c coach service and panel, 1d autopilot and review.

## 1. Goal

An offline AI for Slay the Spire 2 that:

1. **Coaches you live.** It recommends the best move, grades the move you actually make, and reviews each run afterwards.
2. **Plays for you.** Autopilot uses the same engine and sends its moves to the game.

Every decision comes from a local simulator, tree search, and models trained on this Mac. No Claude or other hosted model is involved at runtime, and nothing leaves the machine.

## 2. Decisions and constraints

| Topic | Decision |
|---|---|
| Runtime | Fully offline and local. No LLM or hosted model. |
| Game mode | Single-player only. |
| Character | Ironclad first. Other characters are added one at a time later. |
| Scope order | Act 1 first. Acts 2–3 follow as simulator coverage grows. |
| Platform | macOS, MacBook Pro M4 Max (14 cores, 36 GB RAM), Steam install. |
| Game version | v0.107.1 (game commit `59260271`). |
| Fairness | The engine only uses information the player can see. |
| UI | Floating always-on-top panel; the same page works in a browser tab. |
| Architecture | Search in a local simulator plus self-trained models, built in stages. |

## 3. Why this shape

- **Search wins combat.** In Slay the Spire 1, six attempts to distill combat into a network stalled at or below floor ~14, while search reached floor 23–42 ([sts-rl-agent](https://github.com/destire-mio/sts-rl-agent)). The StS2 simulator's own experiments agree: a trained policy network helps as a search prior (+2.0 HP per fight) and a trained value head does not beat rollouts (`rl/FINDINGS.md` in [evangambit/sts2](https://github.com/evangambit/sts2)).
- **Small learned networks win judgment calls.** In StS1 a ~100k-parameter policy for card picks, pathing, shops and rests added ~11 floors at equal search budget and trained in about 10 minutes on a laptop.
- **A faithful StS2 simulator exists.** evangambit/sts2 (MIT, C# NativeAOT with Python bindings, macOS) targets v0.107.1. It implements every character's card pool, verified against the live game (Ironclad 85 of 87), 250 of 296 relics (all 131 an Act 1 run can hold), 111 monsters and 57 events. Act 1 is solid, Act 2 is being brought up, and Act 3 is not yet validated. It has no API for loading an arbitrary position.
- **There is no useful public StS2 play data.** Only card metadata exists, plus a few hundred LLM-played runs. Training therefore means self-play inside the simulator.
- **The game bridge exists but is fragmented.** STS2MCP exposes game state and actions over localhost. The most useful work lives in forks rather than upstream (section 5.1).

## 4. Architecture

```
 Slay the Spire 2 (your game)
   └── STS2MCP mod (our fork) ── localhost:15526 ──┐
                                                   │ state ~5×/s │ actions (autopilot only)
 Coach service (Python, local) ◀───────────────────┘
   ├── bridge     reads state, detects decision points, checks versions
   ├── snapshot   game state → simulator snapshot (visible information only)
   ├── engine     time-budgeted search in the simulator (our fork of evangambit/sts2)
   ├── grader     compares your move with the engine's best
   ├── fidelity   compares predicted vs actual outcomes → gap backlog
   ├── autopilot  executes engine moves through the mod
   ├── runlog     records every state, recommendation, move and grade
   └── server ── WebSocket ──▶ Panel (pywebview always-on-top window, or a browser tab)

 Offline tools: training (stage 2–3), capture, gap backlog, simulator gap-filling
```

## 5. Components

### 5.1 Mod: fork of STS2MCP

- **Repo:** the existing clone at `~/Documents/git/STS2MCP`, on a new `spire-coach` branch, with remotes added for the two base forks.
- **Base:** merge two forks, both of which descend from upstream `Gennadiyev/STS2MCP@55e0648` and are 0 commits behind it:
  - `evangambit/STS2MCP@608fd7a`: debug actions used by the simulator's live-capture tests (staging cards, energy and powers; starting a chosen event or shop; enchanting a card), ordered piles, and the player's side of the board.
  - `cat12079801/STS2MCP@9853cba`: real power stack counts, never-dropped powers, stable card `uid`s, stable enemy `entity_id`s, an `is_resolving` flag, `wait=true` settled POSTs, the deck on every non-combat screen, dead-but-reviving enemies kept in state, `mod_version` and `schema_version` in every payload, a warnings list, and v0.111 support.
- **Cherry-picks.** Each diff is read before merging, and is skipped if the base already covers it:
  - `set_time_scale` (cicero225/STS2MCP), so autopilot runs faster.
  - Action-readiness fields (enderzcx/sts2-bridge). Likely superseded by `is_resolving` and `wait=true`.
  - Stunned-enemy reporting and card-grid selection fixes (WhoNeedsRAMAnyways/STS2MCP-fork).
  - Any v0.111 fixes not already present (upstream PR #132, timothyhioe/STS2MCP).
- **Our additions:**
  - Per enemy: the current move id and the AI's internal state (move history, counters, flags), read by reflection from the live monster. The mod already reads `monster.NextMove`. Which fields matter is decided per monster type from the decompiled AI code.
  - Any other visible field the snapshot needs that the base forks don't report, such as relic counters. The round-trip gate (section 6.5) is what finds these.
  - A recent player-action log: card `uid`, target, potion slot, in-combat selections and end turn, captured from the game's own play path.
  - A shared-secret token required on every HTTP request. Upstream PR #87 adds one, but bundles it with about 2,000 lines of unrelated changes, so we write a small check instead of cherry-picking it.
- **Hidden-information fields.** Some fields reveal what the player cannot see: true pile order, RNG state, card and potion pools, upcoming encounters. They are used only by tests and capture tools, never by the coach (section 6.2).

### 5.2 Simulator: fork of evangambit/sts2

- **Repo:** cloned to `~/Documents/git/sts2`, `spire-coach` branch, pinned at `evangambit/sts2@37e4264`. Upstream changes are merged deliberately, never automatically.
- **Additions:**
  - `LoadCombatSnapshot`, a native export plus Python wrapper that builds a combat position from a snapshot document (section 6.1), resampling hidden information at load.
  - A visible-state export for round-trip checks.
  - Target selection in combat search. Today `native.step` takes a target and the search ignores it (FINDINGS "Targeting").
  - A live-search entry point with a wall-clock budget that returns per-root-action statistics.
  - Content gap fills (section 6.4).
- **Decompiled game code** is generated locally with `scripts/decompile.sh` from your own game install. It is gitignored and never committed or pushed anywhere.

### 5.3 Coach service: new repo `spire-coach`

Python 3.13, managed with uv. FastAPI and uvicorn serve HTTP and WebSocket; pywebview hosts the floating window. The simulator's Python package (`src/sts2_gym`) is a local path dependency, and it loads the native library built from our fork.

| Module | Responsibility |
|---|---|
| `bridge` | Mod HTTP client with auth token. Polls about 5×/s, detects settled states (`is_resolving` false) and decision points, checks game version, mod `schema_version` and simulator `Sts2_NativeApiVersion` at startup. |
| `snapshot` | Maps mod JSON to a snapshot document through an explicit allowlist of visible fields. |
| `engine` | Runs the live search in worker processes at reduced priority (default 4 workers, 1.5 s budget) and merges root statistics. Returns the best line and per-action value estimates with confidence intervals. |
| `grader` | Identifies your move from the mod's action log (falling back to a state diff), computes the value gap to the best move, and assigns a grade. |
| `fidelity` | After each move, compares the simulator's predicted outcome with the game's and writes gap reports. |
| `autopilot` | Modes off, combat only, and full Act 1. Executes moves through the mod with `wait=true` and enforces stop conditions. |
| `rules` | Stage-1 rules for non-combat screens: card reward, map, shop, rest, event, treasure. |
| `runlog` | One JSONL file per run holding states, recommendations, moves and grades. It can also run on its own as a log-only **capture mode**, collecting real positions for the gates before the engine exists. |
| `review` | Builds the post-run review and re-analyzes positions in the background at 10× the live budget. |
| `server` | Serves the panel and review pages and pushes updates over WebSocket. |

### 5.4 Panel

A static HTML, CSS and JS page served by the coach, with no build step in v1. It is shown in a pywebview always-on-top window or any browser tab. Contents are in section 7.4. v1 targets windowed play, which is how the game currently runs; floating over a fullscreen Space is not a v1 goal.

### 5.5 Local data (gitignored, under `data/`)

- `runs/<run_id>.jsonl`: the run log.
- `gaps/<id>.json`: one fidelity mismatch each, holding the snapshot, move, predicted outcome and actual outcome.
- `captures/`: positions captured for importer tests.
- `checkpoints/`: trained networks.

## 6. Keeping the simulator in sync with the game

### 6.1 A fresh snapshot at every decision

At each decision point the coach builds a new simulator position from the settled game state. The snapshot contains:

- **Player:** HP, max HP, block, energy, powers with exact stacks, relics with counters, potions.
- **Piles:** hand (in order), draw pile (as a multiset), discard pile and exhaust pile. Each card carries its id, upgrade and enchantments.
- **Enemies:** id, HP, max HP, block, powers, current move id, AI state.
- **Combat:** turn number and any pending in-combat selection.

A position is never carried over from the previous one. One mismatch therefore cannot corrupt the rest of the run, and the coach works when started mid-run.

### 6.2 Fair information

- The snapshot allowlist admits only information visible to the player.
- **Draw order and enemy RNG are resampled** in every simulation, using the simulator's existing hidden-state resampling.
- **Known draw-pile positions are kept.** When you knowingly put a card at a known position, such as on top of the draw pile, the coach records it in a per-combat list. It works out those positions by replaying your logged move in the simulator (section 6.3), passes them in the snapshot, and keeps them fixed during resampling. The rest of the draw pile is unordered.
- **Future enemy moves** beyond the shown intent are sampled from the enemy's AI.
- A test fails if any hidden-information field (section 5.1) is read in coach or autopilot mode.
- Grades judge a move on what you could have known.

### 6.3 Continuous fidelity check

- After each of your moves, `fidelity` applies the same move in the simulator and compares the visible result with the game's: damage, block, powers, energy and card movement.
- A deterministic mismatch writes a gap report (section 5.5).
- **Random outcomes are checked for plausibility only.** A field that varies across resampled simulator runs of the same move (random targets, generated cards) counts as random. The game's value only has to be one the simulator can produce.
- Unknown ids at import (a card, relic, power or enemy the simulator does not model) mark the position as unreliable.
- The panel shows a warning line whenever the current fight involves unmodelled content or a mismatch from the last 10 moves.

### 6.4 Gap-filling workflow

For each gap report or unmodelled id:

1. Read the decompiled game code for it.
2. Implement or fix it in the simulator fork.
3. Write a C# test with expected values taken from that code.
4. Stage the same situation in the live game with the mod's debug actions, capture the result, and confirm the simulator matches.

Fixes can also be offered to the simulator's author.

### 6.5 Gates before advice is trusted

Both checks run on positions captured from your own Ironclad games:

- **Round trip:** load a snapshot, export its visible state, and get an identical result for every position without unmodelled content.
- **One-step prediction:** load, apply the recorded move, and match the game's next visible state. Every mismatch must trace to a logged gap.

## 7. Coach behavior (stage 1: combat)

### 7.1 Decision points

- **Combat decision:** `state_type` is `monster`, `elite` or `boss`, the play phase is active, and `is_resolving` is false. This fires at turn start and after every card or potion you play.
- **In-combat selection:** `state_type` is `hand_select`.
- **Everything else:** the coach is idle in stage 1. Autopilot uses `rules`.
- If you act before a search finishes, the coach starts on the new position. The previous search keeps running in the background, only to grade your move.

### 7.2 Value in stage 1

- `V = HP at the end of the fight` if alive, and `V = -100` if dead. Each potion still held adds a fixed value, 10 HP by default, from a config table.
- The panel shows expected HP lost and risk of dying separately.
- **Known limit:** this is short-sighted about potions and scaling setups. Stage 2 replaces it with the learned run value.

### 7.3 Search

- Root-parallel MCTS, starting from the simulator's best measured settings: exploration constant 0.1, tempo-weighted rollout policy, common random numbers at the root, final choice by visit count.
- The default budget of 1.5 s across 4 workers gives roughly 8,000 simulations per decision, based on the measured ~714 µs per simulation.
- Every legal root action receives at least 200 simulations so it can be graded. Identical cards collapse into one action.
- Targets are chosen by the search.

### 7.4 Panel contents

- **Best move** and the rest of the turn, for example "Bash → Jaw Worm, Strike → Jaw Worm, End turn".
- **Expected result of the fight:** HP lost and risk of dying.
- **Two or three alternatives**, each with its cost relative to the best move.
- **A warning line** for unmodelled content or recent mismatches.
- **Show / Quiz switch.** Show (the default) displays the best move immediately. Quiz hides it until you act, then shows the grade and the best move.
- **Compact and expanded layouts.**

### 7.5 Grades

The gap is `V(best) − V(played)` in expected HP. The grade uses the lower bound of the gap's 90% confidence interval, so noise never produces a harsh grade.

| Grade | Lower bound of the gap |
|---|---|
| Best | ≤ 0.5 HP, or the same move |
| Good | < 2 HP |
| Inaccuracy | 2–5 HP |
| Mistake | 5–10 HP |
| Blunder | > 10 HP, or risk of dying up by ≥ 10 percentage points |

Thresholds live in config.

### 7.6 Post-run review

- A page that lists each fight's graded moves, worst first. Each entry shows a simple board view (hand, enemies, intents), your move, the best line and its cost.
- Per-fight accuracy (share of moves graded Best or Good) and total expected HP given up.
- Positions are re-analyzed in the background at 10× the live budget, and the review uses the deeper result.

### 7.7 Autopilot

- **Off:** the coach only reads state.
- **Combat only:** plays your fights; you handle every other screen.
- **Full Act 1:** also plays non-combat screens using `rules` until stage 2. Its main purpose is unattended testing.
- Autopilot runs happen in a separate modded save slot (profile 3), seeded with a copy of your progress, so they never touch your own run history.
- **It stops and hands control back on:**
  - unmodelled content;
  - a fidelity mismatch;
  - an unknown screen;
  - a mod error;
  - no settled state within 10 s of an action.

## 8. Training

### 8.1 Stage 2: strategy model (Act 1 first)

Everything is measured in one currency: the expected final floor of the run. Win chance is also shown once self-play wins at least 5% of runs; below that it carries no signal. The steps follow the simulator's `rl/ARCHITECTURE.md`:

1. **Hand-written run value** from HP fraction, deck, relics, act and floor, as a baseline.
2. **Fight-outcome network.** It predicts the result of a fight (end HP distribution, risk of dying) from deck, relics, HP and encounter. It is trained on fights played by the search in the simulator, with positions drawn from the simulator's existing Act 1 Ironclad position sampler.
3. **Strategy network.** A small network trained by self-play of whole Act 1 runs in the simulator makes card, path, shop, rest and event choices. It replaces `rules`, and a learned run value replaces the hand-written one.

The search's leaf value then becomes the learned run value. The panel keeps showing HP and risk of dying for combat, and shows expected floors for strategy decisions (for example "taking Inflame: −0.4 expected floors").

Past runs can be re-graded. Run files record `card_choices`, `cards_gained`, `cards_removed`, `cards_transformed`, `cards_enchanted`, `upgraded_cards`, and relic, potion, event and rest choices for every floor, so the deck at each decision can be reconstructed.

### 8.2 Stage 3: combat networks through self-play

Policy and value networks steer the combat search in an AlphaZero-style loop: search generates games, the networks train on them, and the stronger search generates the next batch. A new network version is promoted to the coach only if it beats the previous one on the fixed benchmark fights (section 10).

### 8.3 Compute

Everything trains locally: simulation on the performance cores and networks in PyTorch on CPU or the Apple GPU. Long jobs run overnight.

## 9. Error handling and operations

- **Version mismatch:** if the game, mod schema or simulator API version does not match the expected set, the panel shows a banner and advice is off. A manual override exists for experimentation.
- **Game patches:**
  - Update the simulator with its decompile, extract and test pipeline.
  - Update the mod from the base forks or our own fixes.
  - Re-run the gates in section 6.5.
  - v0.111 is on the beta branch now; expect a period without advice when it reaches the main branch.
- **Game closed or mod not responding:** the panel shows "waiting for game" and the bridge reconnects automatically.
- **Unknown screen:** the coach idles and autopilot stops.
- **Saves:** mods switch the game to a separate save set.
  - Setup backs up the normal profile, then copies it into modded profile 1 (yours) and modded profile 3 (autopilot), with the game closed.
  - If the game rejects the copied saves, restore the backup and play on a fresh modded profile.
  - If Steam Cloud reports a conflict afterwards, keep the local copy.
- **Security:** the mod's API listens on localhost only and requires the auth token.
- **Performance:** search workers run at reduced priority. The worker count is tuned so the game's frame rate is unaffected.

## 10. Testing

- **Coach service:** test-driven development with pytest. Covers bridge parsing (recorded mod payloads as fixtures), decision-point detection, snapshot mapping and the allowlist, grading math and confidence handling, and autopilot stop conditions.
- **Simulator:** C# tests for the snapshot loader, the visible-state export and every gap fill. The existing test suite must stay green.
- **Mod:** scripted smoke tests against the live game for each added field and cherry-pick.
- **Benchmarks:**
  - The simulator author's fixed set of Act 1 fights, measuring death rate and HP.
  - 20 hand-picked sanity positions with an unambiguous best move (a lethal line, a big incoming hit, and so on).

## 11. Milestones and definitions of done

### Stage 0: setup (part of stage 1)

- Install the .NET 9 SDK, plus Xcode Command Line Tools if missing (needed to link the NativeAOT library).
- Build the merged mod and install it into `SlayTheSpire2.app/Contents/MacOS/mods/`.
- Enable mods in the game.
- Back up and copy saves.
- Build the simulator's native library for osx-arm64 and decompile the game locally.
- List the unimplemented Ironclad and curse/status cards with the simulator's coverage scripts.

### Stage 1: combat coach (roughly 4–6 weeks)

Done when all of these hold:

- Round trip is exact on at least 300 captured positions without unmodelled content, and every one-step mismatch traces to a logged gap.
- Advice appears within 2 s of the game settling in at least 90% of decisions.
- The coach picks the right move in all 20 sanity positions.
- The live search has a death rate of 0.092 or lower on the 240-fight benchmark in `rl/FINDINGS.md`, reproduced with its scripts. That is the author's best published result.
- Combat-only autopilot finishes 20 real Act 1 fights without getting stuck.
- The post-run review works for a real run.

### Stage 2: strategy model (roughly 3–4 weeks plus training time)

Done when all of these hold:

- Over 500 held-out simulator seeds, the strategy network beats `rules` by at least 1.0 mean floor and has a higher Act 1 boss-kill rate. Both differences must lie outside the 95% confidence interval.
- Card-pick and path grades appear live and in reviews.
- Full-Act-1 autopilot uses the model.
- Past-run re-grading works on your Act 1 decisions.

### Stage 3: combat networks (open-ended)

Each promoted network beats its predecessor on the benchmark fights.

## 12. Repos and layout

```
~/Documents/git/spire-coach/        new repo (this spec)
  coach/                            Python package (modules in section 5.3)
  panel/                            static web UI: panel + review pages
  training/                         stage 2–3 training and evaluation
  tools/                            setup (mod install, save backup/copy), capture, gap backlog CLI
  tests/
  data/                             gitignored: runs, gaps, captures, checkpoints
  docs/superpowers/specs|plans/
~/Documents/git/sts2/               simulator fork, branch spire-coach (decompiled/ gitignored)
~/Documents/git/STS2MCP/            mod fork, branch spire-coach
```

Pushing any of these to GitHub is optional and happens only on request.

## 13. Risks

| Risk | Mitigation |
|---|---|
| Enemy AI state differs per monster and some of it may be hard to read | Map the fields per monster type from the decompiled AI code and read them by reflection. Otherwise resample from the enemy's move table given the shown intent, and flag the position. |
| Snapshot loading misses a mechanic (enchantments, temporary cost changes, statuses) | The round-trip and one-step gates catch it; the fidelity check keeps catching it in live play. |
| Python search loop too slow | The measured budget already exceeds the author's settings by more than 10×. If not, move the MCTS loop into C#. |
| Upstream simulator churn | Pin commits and merge upstream deliberately. |
| Game patch breaks the mod or simulator | Version gate turns advice off; the update procedure is in section 9. |
| Copied saves rejected | Restore the backup and use a fresh modded profile. |
| Hidden information leaks into advice | Allowlist plus a failing test (section 6.2). |
| Always-on-top panel does not float over the game | v1 supports windowed play; the panel also works in a browser tab. |

## 14. Out of scope for now

- Other characters (added one at a time after stage 2).
- Acts 2–3 advice (until the simulator covers them).
- Multiplayer.
- In-game overlay.
- LLM-written explanations.
- Windows support.
- Cloud training.

## 15. Sources

- STS2MCP: https://github.com/Gennadiyev/STS2MCP, plus forks `evangambit/STS2MCP` and `cat12079801/STS2MCP`.
- StS2 simulator and combat-agent findings: https://github.com/evangambit/sts2 (`rl/FINDINGS.md`, `rl/ARCHITECTURE.md`, `docs/agent-interface.md`).
- StS1 learned non-combat policy and negative combat results: https://github.com/destire-mio/sts-rl-agent
- StS1 MCTS-plus-strategy agent: https://github.com/AttemorySystem/spire-agent
- StS2 datasets (card data only): https://huggingface.co/blog/t22000t/slay-the-spire-ai-collection
