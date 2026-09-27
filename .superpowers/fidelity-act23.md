# Fidelity beyond Act 1: `20260926-220853.jsonl` (A10 Ironclad, Act 3 floor 48)

Replayed with `sts2/scripts/fidelity_replay.py` (branch `spire-coach-fidelity`), which
rebuilds each `check` record's before/after live snapshots from the log's `state` records
(the same `coach.snapshot.build_live_snapshot` step the live coach runs, replaying its
room_type bookkeeping) and calls `sts2_gym.live.fidelity.check`, the same function the
engine service's `check` method calls.

## Counts, before and after

| Act | Replayed | Comparable (before) | Comparable (after) | Mismatched (before) | Mismatched (after) |
| --- | -------- | -------------------- | -------------------- | --------------------- | --------------------- |
| 1   | 62       | 44 (43 matched, 1 mismatched) | 44 (44 matched, 0 mismatched) | 1 | 0 |
| 2   | 69       | 0                     | 0                     | 0                      | 0                      |
| 3   | 129      | 0                     | 0                     | 0                      | 0                      |

(39 more `check` records in the log had no fair position after to compare against and were
never candidates either way.)

## What was fixed

**Act 1 — the Waterfall Giant Vulnerable mismatch (the log's only mismatch).** Thunderclap
brought the giant to ABOUT_TO_BLOW (`SteamEruptionPower.AfterDeath` ->
`TriggerAboutToBlowState`). The game answers that death — HP to 999999999, every power but
Steam Eruption gone — inside the killing blow, before its own next effect runs; the
emulator answered it once, at the end of the whole card (`CombatEngine.HandleEnemyDeaths`),
so Thunderclap's own Vulnerable (applied to every enemy still standing right after its
damage) missed a giant this call had not yet stood back up, and the end-of-card answer then
stripped the Vulnerable already on it: 0, where the game read 1.

Fix: `CardEffects.OnEnemyDeath` now calls the engine's existing
`AnswerDeathsHoldingTheFightOpen` (already used before a step returns and before an enemy
phase begins) the instant an enemy's `Hp` hits zero, not only at those two points. It is a
no-op for an ordinary death. Two existing tests changed with it, not regressions: a Sword
Boomerang whose first hit kills the giant now lands its 2nd/3rd hits on the revived
999999999 sentinel rather than a corpse clamped at zero, and a former "known divergence"
test (Uppercut's Weak on the killing blow) flips to the game's own numbers, exactly as its
own comment said it should once the revive moved inside the damage path.

Commits: `272a8ef` (the replay script), `7f06f06` (the fix), both on `spire-coach-fidelity`.

## What remains

**Acts 2-3 stay at 0 comparable — every check there is blocked by causes outside my
assigned area** (`src/sts2_gym/live/convert.py`, `enemy_ai.py`, `selection.py`,
`fidelity.py`, and enemy/power/card behaviour in C#; another agent owns
`rl/run_train/`, `strategy.py` and the run engine's act flow/decision screens). Grouped by
reason, most frequent first:

- **`relics.py`**: PAELS_WING and THROWING_AXE (both picked up in this run) have no
  converter entry at all — `relics.py`'s own docstring says its table "covers every relic
  an Act 1 run can hold" by design — so **every** Act 2-3 check in this log carries this
  warning regardless of anything else. LIZARD_TAIL's `_wasUsed` field is a second, smaller
  gap in Act 3. Fixing these two relics alone would very likely be the single highest-value
  next step; it just isn't in this agent's file list.
- **`enemy_ai.py`** (mine): "AI state is generic" for ~20 distinct Act 2-3 enemy kinds this
  one log alone reaches (BOWLBUG_ROCK/SILK, SLUMBERING_BEETLE, THE_OBSCURA,
  THIEVING_HOPPER, CRUSHER, ROCKET, TUNNELER, PARAFRIGHT, AXEBOT, DEVOTED_SCULPTOR,
  FLAIL_KNIGHT, GLOBE_HEAD, MAGI_KNIGHT, MECHA_KNIGHT, OWL_MAGISTRATE, SCROLL_OF_BITING,
  SPECTRAL_KNIGHT, TEST_SUBJECT, THE_FORGOTTEN, THE_LOST). None of them have an
  `EnemyAI.cs` case at all (`grep` for any of their `KE.*` names in `EnemyAI.cs` is empty),
  so a per-monster AI model here is only half the fix. Tried registering a `Cycle` model
  for CRUSHER (its move machine — decompiled `Monsters/Crusher.cs` — is a plain,
  unconditional 5-ring): the model itself checks out (verified against the decompiled
  source by hand and by two new self-consistency tests), but `test_live_enemy_ai.py`'s
  aggregate tests (`SelfConsistencyTests.test_every_move_of_every_model_was_on_display`,
  `ThrowawayChooseIntentsTests.test_the_next_move_follows_from_the_rebuilt_state`) hard-assume
  the whole registry is Act 1 and fully backed by a captured fixture corpus, and fail
  immediately for any entry that isn't. Reverted rather than leave the suite red or hack
  around a wider assumption on a lean budget. Extending `enemy_ai.py` to Act 2-3 needs that
  test infrastructure (an Act 2-3 fixture corpus, or a "no corpus yet" exemption list)
  addressed first, as its own task.
- **`powers.py`**: SURROUNDED_POWER's facing, SWIPE_POWER's stolen card,
  POSSESS_SPEED/STRENGTH_POWER's stolen amount — each "is not in the live snapshot" (a
  schema gap: the field isn't in `sts2-live-snapshot/1` at all, so this also touches
  `spire-coach/coach/snapshot.py` and the STS2MCP mod, not just the converter).
- **`cards.py`**: several attacks (STRIKE_IRONCLAD, THUNDERCLAP, BASH, ULTIMATE_STRIKE,
  POMMEL_STRIKE, HEMOKINESIS, FIGHT_ME) show live cost 0 with no `cost_this_turn`/
  `cost_for_combat` field to explain it, in every act including Act 1. `_expected_cost`
  only special-cases Corruption; a player holding something like `Unrelenting`'s
  FreeAttackPower (next Attack costs 0) would produce exactly this pattern. Not confirmed
  against a capture — flagged as a lead, not a diagnosis.

No new C# mismatches to chase for step 4: Act 1 is fully matched now, and Act 2-3 never
reach the simulator step at all (gated by the causes above), so there is nothing new
comparable to check for mismatches yet.

**Step 5** (spire-coach, branch `panel-act1-note`): the panel now shows "No between-fight
advice outside Act 1 yet" for a strategy failure on an Act 2-3 screen, and still shows the
real error for an Act 1 failure (or one before any act is known). Commit `83ddbda`.
