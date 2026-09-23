# Training Stage 2 (Strategy Model) + Stage 3 (Combat Prior) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A training system that runs for days in parallel on this Mac and keeps producing better models:
- a **strategy model** (card rewards, map path, shop, rest site, events, card removal) whose per-option values the coach can show as "expected floors";
- a **combat prior** (policy network) that sharpens the live MCTS.

Every generation is evaluated on fixed held-out seeds. Only improvements are promoted.

**Architecture:** This follows the simulator's own design (`rl/ARCHITECTURE.md`) and the StS1 evidence (small learned nets win judgment decisions; search wins combat).
- **Self-play workers** play full Act 1 Ironclad runs in the native run engine (`Sts2Run_*`, `src/sts2_gym/run_env.py`). Combat is played by the existing combat MCTS (`rl/combat_mcts.py` settings: c = 0.1, tempo rollout, common random numbers). Non-combat decisions come from the current strategy policy with exploration.
- **Learning:**
  - `V_run`, a value network predicting the run outcome from a **feature vector computable from player-visible state alone**;
  - a **fight-outcome network** predicting HP lost and death risk for (deck, relics, HP, encounter);
  - the policy, which picks the option maximizing `V_run(features(state after option))`: one-step lookahead on features, with a softmax temperature during self-play.
- **A generation loop:** generate → train → evaluate on held-out seeds → promote the champion. It runs until stopped.
- **Stage 3,** on separate cores: the upstream combat-prior pipeline (`rl/train_combat.py`, `rl/selfplay.py`, `rl/combat_net.py`), iterated, feeding a PUCT prior into search.

**Tech Stack:** Python 3.12, PyTorch (the version pinned in the simulator's `pyproject.toml`), numpy, multiprocessing (one native library per process, because the emulator uses static scratch buffers and is not thread-safe), the emulator's ctypes bindings.

**Spec:** `docs/superpowers/specs/2026-09-23-spire-coach-design.md` §8 (binding). The simulator's `rl/ARCHITECTURE.md` and `rl/FINDINGS.md` are required reading before any code.

## Global Constraints

- Work in the worktree `~/Documents/git/sts2-wt/training` on branch `spire-coach-training`, created from `spire-coach` at `37e4264`. New training code lives in `rl/run_train/` as a package; stage 3 glue lives in `rl/prior_loop/`.
- Environment: `source ~/Documents/git/spire-coach/tools/env.sh`; `uv sync --python 3.12`; build the native library in the worktree with `bash scripts/build.sh osx-arm64`.
- Hardware: Apple M4 Max with 14 cores (10 performance, 4 efficiency) and 36 GB RAM.
  - The default worker counts leave 2 performance cores free for the user and the live coach: stage 2 uses 8 workers and stage 3 uses 2.
  - Workers run at reduced priority (`os.nice(10)`).
  - Every count is configurable.
- Scope: Ironclad, Act 1 (the run ends when the Act 1 boss dies or the player dies), ascension 0 first, with ascension as a config value.
- **The feature contract (critical):**
  - Every model input is computable from player-visible state alone: deck, relics, potions, HP/max HP, gold, act, floor, the map ahead (node types and connections), and the options on screen.
  - It never uses RNG, hidden pools or the upcoming encounter identity.
  - One module, `rl/run_train/features.py`, owns the feature definition. It has two front ends: `from_run_engine(...)` for training, and `from_live(live_run: dict)` for the coach. The live-run input format is defined in Task 1's design doc.
  - A test asserts both front ends agree on states that can be expressed in both.
- Checkpoints and data go to `~/Documents/git/sts2-wt/training/runs/` (gitignored; add it to `.gitignore`). Never commit model weights or data shards.
- Every long job must be resumable after a crash or reboot from the last completed generation, and must write a human-readable `status.json` (generation, games played, eval metrics, champion path, last update time).
- Evaluation uses a fixed held-out seed set that is never used for training: 200 seeds, stored in `rl/run_train/eval_seeds.json`. Headline metrics: mean floor reached and Act 1 boss-kill rate, each with a 95% confidence interval.

## Review Focus

1. **A worker process crashes, or the native library raises mid-run.** The runner logs it, restarts the worker, and never loses completed shards or corrupts the generation.
2. **The training job is killed and restarted.** It resumes from the last completed generation with no duplicated or lost data.
3. **Feature drift between training and live use.** The run-engine and live front ends disagree on a shared state. Tests catch it.
4. **A new generation that is worse than the champion.** It is not promoted, and the coach keeps using the champion.
5. **Non-combat decision types the engine offers that the policy does not know.** They are handled (a heuristic fallback plus a logged count), never a crash, and they stay visible in `status.json`.

---

### Task 1: Investigation and technical design

**Files:**
- Create: `rl/run_train/DESIGN.md`
- Create: `rl/run_train/probe_throughput.py` (a measurement script)

Deliver `DESIGN.md`. It must be precise enough that Tasks 2–5 need no further design decisions:
1. **How the run engine exposes decisions.**
   - The phases (`Sts2Run_GetPhase`), the action mask (`Sts2Run_ActionMask`), how each non-combat decision type (Neow, card reward, map, shop, rest, event, treasure, card removal/upgrade/transform selections) is encoded in actions, and how to enumerate and describe each option.
   - How combat runs inside a run (`Sts2Run_StartCombat`?), and how `combat_mcts` can search a combat that happens inside a run: cloning cost, and whether to search on run clones or hand off to a combat handle.
   - Cite `src/sts2_gym/run_env.py`, `commands.py`, `run_constants.py` and `src/Sts2Emulator/Interop/RunNativeExports.cs` with file:line.
2. **Measured throughput** (`probe_throughput.py`): single-process wall time for one full Act 1 run with combat MCTS at 30, 60 and 120 simulations per decision, and the rules policy for non-combat. Extrapolate runs/hour for 8 workers. Pick the default budget that gives ≥ 300 runs/hour on 8 workers, or explain why that is impossible.
3. **Feature specification.**
   - Exact feature vector layout: deck as a count vector over card ids and upgrade state, plus aggregate stats; relics as a multi-hot vector; potions; scalars; map-ahead summary (counts and distances of elites, rests, shops and unknowns on reachable paths from each candidate node); option descriptors per decision type.
   - The **live-run input format** the coach will send to `features.from_live`, written as a JSON schema. Its fields must be obtainable from the STS2MCP mod's state: deck, relics, potions, HP, gold, act/floor and map on non-combat screens.
4. **Option evaluation.**
   - How each decision type's options become "state after option" feature vectors without the run engine. Deterministic options (take card X, skip, buy Y, remove card Z, rest, smith card W) apply directly to features. Stochastic options (events, unknown nodes) use expected-value features or a learned per-option head; state which, and why.
   - Map choices use the map-ahead summary.
5. **Models and learning.**
   - Architectures: MLPs are acceptable, but justify sizes.
   - Targets: final floor, boss kill and end HP; a `V_run` target in expected-floor units; the fight-outcome target.
   - Losses and replay buffer size.
   - Exploration schedule (softmax temperature decay).
   - Generation size and promotion rule (champion replaced only if the challenger's mean-floor 95% CI lower bound exceeds the champion's mean, on the 200 held-out seeds).
6. **The stage 3 plan:**
   - how to run the upstream combat-prior pipeline repeatedly (`train_combat.py --generate --prior --sims N`, retrain, evaluate on the FINDINGS benchmark);
   - how the resulting prior plugs into combat MCTS as PUCT with c_puct = 0.2;
   - the promotion gate (beat the previous prior's death rate and HP on the fixed fight set).
7. **A revised task list for Tasks 2–5.** Adjust file names and interfaces if the investigation shows a better shape. Changes that contradict this plan's Global Constraints need a stated reason.

Commit `docs(train): run-level training design and throughput probe`.

---

### Task 2: Self-play workers, data shards and evaluation harness

**Files:** following DESIGN.md. At minimum `rl/run_train/features.py`, `rl/run_train/selfplay.py`, `rl/run_train/runner.py` (the multiprocess pool) and `rl/run_train/evaluate.py`, with tests in `tests/python/test_run_train_*.py`.

**Interfaces:**
- Produces:
  - `features.from_run_engine(...) -> np.ndarray` and `features.from_live(live_run: dict) -> np.ndarray`
  - `selfplay.play_run(seed: int, policy, combat_budget: int) -> RunRecord`
  - `runner.generate(n_runs, workers, policy_spec, out_dir) -> list[Path]`
  - `evaluate.evaluate(policy_spec, seeds) -> EvalResult` (mean_floor, boss_kill_rate, cis)

Requirements:
1. The rules policy (the baseline) handles every non-combat decision type. It is also the fallback for unknown types.
2. Workers write shards atomically: write to a temp file, then rename.
3. Tests:
   - features are deterministic;
   - the two front ends agree on shared states;
   - `play_run` finishes for 3 seeds at a tiny budget;
   - `runner.generate` with 2 workers produces the expected shard count;
   - evaluation metrics on a synthetic record set are correct;
   - a worker crash is restarted (simulate it with a policy that raises once).
4. Run the rules baseline on the 200 eval seeds and record it in `DESIGN.md`.

Commit `feat(train): self-play workers, shards and evaluation harness`.

---

### Task 3: Models, the training step, and the lookahead policy

**Files:** following DESIGN.md. At minimum `rl/run_train/models.py`, `rl/run_train/train.py` and `rl/run_train/policy.py`, with tests.

**Interfaces:**
- Produces:
  - `policy.LookaheadPolicy(checkpoint, temperature)`, which implements the policy interface used by `selfplay`;
  - `policy.score_options(decision: dict, live_run_or_engine_state) -> list[tuple[option, value]]`, the coach-facing API with values in expected floors;
  - `train.fit(shards, prev_checkpoint) -> checkpoint`.

Requirements:
1. `V_run` and fight-outcome training on shards, with a train/validation split and early stopping.
2. `LookaheadPolicy` picks by one-step feature lookahead, samples from a softmax during self-play, and takes the argmax in evaluation.
3. Tests:
   - a training step reduces loss on a tiny synthetic set;
   - `score_options` returns one value per option, with the best option first at temperature 0;
   - a checkpoint round-trips.

Commit `feat(train): value models, training step and lookahead policy`.

---

### Task 4: Generation loop, promotion and long-running operation

**Files:** `rl/run_train/loop.py` and `rl/run_train/cli.py` (`python -m rl.run_train {start,status,stop,export}`), with tests.

Requirements:
1. `start` launches the loop in the background:
   - detached, so it survives the terminal closing;
   - it writes a PID file, `status.json` and a log;
   - it resumes from the last completed generation.
2. Promotion follows the DESIGN.md rule. `export` copies the champion to `~/Documents/git/spire-coach/data/models/strategy/` along with a `model.json` (feature version, metrics, generation).
3. Tests:
   - resume after a simulated kill;
   - no promotion when the challenger is worse;
   - `status` reads a running job;
   - `stop` terminates the workers cleanly.
4. **Launch it for real:** start the loop with the DESIGN.md defaults, confirm it completes generation 1 and writes `status.json`, and leave it running. Report the PID, the log path and the first-generation metrics.

Commit `feat(train): resumable generation loop with champion promotion`.

---

### Task 5: Stage 3 combat-prior loop

**Files:** `rl/prior_loop/` (a loop script and CLI mirroring Task 4), with tests.

Requirements:
1. Wrap the upstream pipeline (`rl/train_combat.py`, `rl/selfplay.py`) in a resumable iterate-train-evaluate loop, following DESIGN.md §6.
2. Promote a prior only if it beats the previous one on the fixed fight benchmark.
3. Export the champion prior to `~/Documents/git/spire-coach/data/models/combat_prior/` with a `model.json`.
4. Launch it with 2 workers, alongside Task 4's loop, and leave it running. Report the PID, log path and first-iteration metrics.

Commit `feat(train): resumable combat-prior loop`.
