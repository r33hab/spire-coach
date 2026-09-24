# Autopilot, Non-Combat Rules, Run Management and Post-Run Review — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The coach can play for the user. It has three modes:
- **combat only:** it plays your fights and leaves every other screen to you;
- **full Act 1:** it also handles every non-combat screen, with the strategy model when available and rules otherwise;
- **loop N runs:** unattended, on a separate save slot, for testing and fidelity data.

Every run ends with a review page listing the player's graded moves, worst first, re-analyzed at 10× the live budget.

**Architecture:** This builds on the live coach (`2026-09-24-coach-live.md`):
- `coach/autopilot.py` executes engine recommendations through the mod.
- `coach/rules.py` holds the non-combat heuristics.
- `coach/runs.py` handles profile switching and run start through `menu_select`.
- `coach/review.py` plus `panel/review.html` build the post-run review with deep re-analysis.

**Tech Stack:** Python 3.12, the existing coach modules, pytest.

**Spec:** `docs/superpowers/specs/2026-09-23-spire-coach-design.md` §7.6–7.7, §8.1, §9 (binding).

## Global Constraints

- Repo `~/Documents/git/spire-coach`, branch `dev`. Tests use fakes only.
- **Autopilot runs happen only in modded save slot 3**, seeded with a copy of the user's progress. `runs.py` switches to profile 3 from the main menu before starting, and refuses to autopilot on profile 1 unless `--allow-profile-1` is given.
- **Autopilot stops and hands control back** on any of these, with a one-line reason in the panel:
  - an unreliable analysis (unmodelled content);
  - a fidelity mismatch;
  - an unknown or `overlay` screen;
  - a mod error;
  - no settled state within 10 s of an action;
  - a version mismatch.
- Moves go through the mod's actions with `wait=true`. After each action the autopilot verifies the state changed. During autopilot it sets `set_time_scale` 3.0 and restores 1.0 on stop.
- Moves with `source = "api"` are never graded. They still get fidelity checks: amend the live loop so checks run for api moves too.
- No hidden information: the autopilot sees only what the coach sees.

## Review Focus

1. **The mod rejects an action** (for example "card cannot be played"). The autopilot stops with that message. It never retries blindly in a loop.
2. **Game over or victory.** In loop mode it returns to the main menu and starts the next run. Otherwise it stops.
3. **A screen the rules do not know.** It stops and names the screen. It never guesses.
4. **The user moves the mouse or plays during autopilot.** A human-source action in the log pauses the autopilot, with "you took over; autopilot paused".
5. **Review re-analysis of a long run.** It runs in the background at low priority, shows progress, and can be interrupted and resumed.

---

### Task 1: Autopilot core (combat)

**Files:** create `coach/autopilot.py` and `tests/test_autopilot.py`; modify `coach/live.py` (fidelity for api moves) and its tests.

**Interfaces:**
- Produces:
  - `Autopilot(client, session, *, mode: "off"|"combat"|"act1", rules=None, on_stop: Callable[[str], None])`, with `step() -> bool` (True if it acted), `stop(reason)`, and a `mode` property.
  - `to_mod_action(move: Move) -> dict`:
    - card → `{"action":"play_card","card_index":..,"target":..}`, preferring `card_uid` when the mod accepts it;
    - potion → `use_potion`;
    - end_turn;
    - select → `combat_select_card` then `combat_confirm_selection`.

Requirements:
1. When the session publishes a reliable recommendation for the current settled position and it is the player's turn, execute `best` and wait until settled. Any stop condition from the Global Constraints halts it.
2. A human-source action seen while the autopilot is on pauses it (Review Focus 4).
3. Tests with a fake bridge and a fake engine:
   - it plays a full scripted fight to victory;
   - it stops on an unreliable analysis, on a mod error, on no settle within 10 s, and on a mismatch;
   - it pauses on a human action;
   - the action mapping for each move kind.
4. Commit `feat(autopilot): play combats from engine recommendations with safety stops`.

### Task 2: Non-combat rules and the strategy-model hook

**Files:** create `coach/rules.py`, `coach/data/ironclad_card_tiers.json` and `tests/test_rules.py`.

**Interfaces:**
- Produces:
  - `decide(state: dict, *, engine=None) -> dict | None`: the mod action for a non-combat screen, or None when unknown.
  - `RULES_VERSION`.

Requirements:
1. Screens, with their rule:
   - **rewards:** claim gold, relics, and potions when a slot is free; open the card reward.
   - **card_reward:** Ironclad tier list plus deck-size and curve heuristics; skip when every card is below threshold.
   - **map:** path heuristic:
     - rest when HP < 50%;
     - elite when HP > 70% and the fight-strength proxy is OK;
     - shop when gold ≥ 150;
     - otherwise prefer monster or unknown.
   - **rest_site:** heal when HP < 60%; otherwise smith the top-priority upgrade.
   - **shop:** card removal if it is affordable and a Strike, Defend or curse exists; otherwise one top-tier card if affordable; else leave.
   - **event:** the first option that costs no more than 20% of max HP and no card loss; else the safest option; then proceed.
   - **treasure:** claim.
   - **card_select** (upgrade, remove, transform): pick by priority lists.
   - **relic_select:** priority list.
   - **Neow/ancient:** the safest option.
   - **crystal_sphere:** proceed.
2. **The strategy hook.** If `engine.strategy(live_run, decision)` returns options, take the top-valued option for supported decision kinds (card reward, map, rest, shop, removal) and fall back to the rules otherwise. The live-run input format is defined in the training design; see the training worktree's `rl/run_train/DESIGN.md` §3.
3. Tests: one per screen rule, driven by fixtures; an unknown screen → None; the strategy hook used when options exist and the fallback when `no_model`.
4. Commit `feat(rules): non-combat heuristics with strategy-model hook`.

### Task 3: Run management and the `autopilot` command

**Files:** create `coach/runs.py` and `tests/test_runs.py`; modify `coach/cli.py` and its tests.

**Interfaces:**
- Produces:
  - `ensure_profile(client, profile_id: int)`, which switches only from the main menu.
  - `start_run(client, character="IRONCLAD", ascension=0)`, via the `menu_select` sequence, handling `selection_busy` from the character-select race fix.
  - `return_to_menu(client)`.
  - The CLI command `spire-coach autopilot --mode combat|act1 [--runs N] [--profile 3] [--allow-profile-1]`.

Requirements:
1. Loop mode:
   - make sure profile 3 is active;
   - start a run;
   - autopilot in act1 mode until game over or the Act 1 boss;
   - return to the menu;
   - repeat N times;
   - summary: runs, floors reached, boss kills, stop reasons.
2. The command shares the coach panel: it runs the CoachSession and panel with the autopilot on top.
3. Tests with a fake bridge: the menu sequences, refusing profile 1 without the flag, a loop of 2 runs, and stop reasons counted.
4. Commit `feat(runs): profile guard, run start and autopilot command`.

### Task 4: Post-run review

**Files:** create `coach/review.py`, `panel/review.html`, `panel/review.js` and `tests/test_review.py`; modify `coach/server.py` (the `/review` routes) and `coach/cli.py` (`spire-coach review [RUNLOG]`).

**Interfaces:**
- Produces:
  - `build_review(runlog_path: Path) -> Review`. Review holds the graded moves sorted by cost (lower bound desc), each with its board summary (hand, enemies with intents, player HP/block/energy), the played move, the best line and the grade; per-fight accuracy (share of Best+Good); total expected HP given up.
  - `deep_reanalyze(review, engine, *, budget_s=15, confirm_pairs=500, min_child_sims=2000, progress)`: re-grades every graded position and stores the results beside the run log. It is resumable.
  - `GET /review?run=<file>` and `GET /review/data?run=<file>`.

Requirements:
1. When a run ends, the coach kicks off the deep re-analysis in the background at low priority. The review page shows live numbers, then the deep ones when ready.
2. Tests: a review built from a synthetic run log (ordering, accuracy, totals); deep re-analysis with a fake engine (resume after interruption, progress callbacks); the routes serve the data.
3. Commit `feat(review): post-run review with deep re-analysis`.
