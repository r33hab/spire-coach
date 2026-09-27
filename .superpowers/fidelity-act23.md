# Fidelity beyond Act 1: `20260926-220853.jsonl` (A10 Ironclad, Act 3 floor 48)

Replayed with `sts2/scripts/fidelity_replay.py` (branch `spire-coach-fidelity`), which
rebuilds each `check` record's before/after live snapshots from the log's `state` records
(the same `coach.snapshot.build_live_snapshot` step the live coach runs, replaying its
room_type bookkeeping) and calls `sts2_gym.live.fidelity.check`, the same function the
engine service's `check` method calls.

## Counts, start -> after Act 1 fix -> final

| Act | Replayed | Comparable (start) | (after Act 1 fix) | (final) | Mismatched (final) |
| --- | -------- | ------------------- | ------------------ | ------- | ------------------- |
| 1   | 62       | 44 (43 matched, 1 mismatched) | 44 (44 matched, 0) | 46 (46 matched, 0) | 0 |
| 2   | 69       | 0                    | 0                   | 8 (8 matched, 0)   | 0 |
| 3   | 129      | 0                    | 0                   | 48 (47 matched, 1) | 1 |

(39 more `check` records in the log had no fair position after to compare against and were
never candidates either way.)

## What was fixed

**Act 1 — the Waterfall Giant Vulnerable mismatch.** Thunderclap brought the giant to
ABOUT_TO_BLOW (`SteamEruptionPower.AfterDeath` -> `TriggerAboutToBlowState`). The game
answers that death — HP to 999999999, every power but Steam Eruption gone — inside the
killing blow, before its own next effect runs; the emulator answered it once, at the end of
the whole card (`CombatEngine.HandleEnemyDeaths`), so Thunderclap's own Vulnerable missed a
giant this call had not yet stood back up, and the end-of-card answer then stripped the
Vulnerable already on it. Fix: `CardEffects.OnEnemyDeath` now calls the engine's existing
`AnswerDeathsHoldingTheFightOpen` the instant an enemy's `Hp` hits zero, not only before a
step returns / before an enemy phase. No-op for an ordinary death. (`272a8ef`, `7f06f06`)

**Relics — PAELS_WING and THROWING_AXE had no converter entry**, which alone gated
*every* Act 2-3 check in this log (`relics.RELICS`'s "no converter entry" warning fires
regardless of anything else on the position). PAELS_WING: `RelicEffects.cs` has no case
for it at all (its whole effect is a card-reward-screen alternative, never touched in
combat) — `_STATELESS`, the table's existing idiom for exactly that. THROWING_AXE: a real
case already exists (`ConsumeThrowingAxePlay`), reading `_usedThisCombat` the same way
RUINED_HELMET and CENTENNIAL_PUZZLE already do (`_flag`). (`6df707a`)

**Enemy AI models for 14 Act 2-3 enemies.** Every one of them already has a real
`EnemyAI.cs` case (`SelectIntent` + `ExecuteIntent`) — the earlier "no case yet" read of
Crusher was a grep for the live id (`CRUSHER`) against C#'s PascalCase enum member
(`KE.Crusher`), which matches nothing for any enemy. What was actually missing was the
Python side: a `Cycle` model to verify the AI state against, so "AI state is generic" no
longer gates comparability. Added: CRUSHER, BOWLBUG_SILK, TUNNELER, THIEVING_HOPPER,
DEVOTED_SCULPTOR, GLOBE_HEAD, OWL_MAGISTRATE, THE_LOST, THE_FORGOTTEN, ROCKET,
MECHA_KNIGHT, MAGI_KNIGHT, AXEBOT (its own `_Axebot(Cycle)`: BOOT_UP only opens a
stock-override respawn, a fresh bot skips straight to MoveIndex 1), PARAFRIGHT (summoned
by The Obscura's ILLUSION_MOVE). `test_live_enemy_ai.py`'s four aggregate tests hard-assumed
the whole registry was Act 1 and fully fixture-backed; `NO_ACT1_CORPUS` names the new
entries once and each test excludes it, rather than reverting a correct model. (`e5b6c5e`)

Two more fixes surfaced once Act 2-3 checks could actually reach the simulator step:

- **Rage's block gain was Dexterity-powered; the game has it Unpowered**
  (`RagePower.AfterCardPlayed: ValueProp.Unpowered`). Both `CombatEngine.PlayCard` call
  sites used `GainBlock` instead of `GainUnpoweredBlock`. Found from a live Body Slam+
  showing `player.block` 27 -> 32 (Rage 5) where the emulator predicted 34 (Dexterity 2
  folded in). Test first (`RageTests.DexterityDoesNotIncreaseTheBlock`). (`2cd72d7`)
- **MECHA_KNIGHT's FLAMETHROWER_MOVE and THIEVING_HOPPER's stun.** FLAMETHROWER is
  StatusIntent(4), which reads live as `STATUS_CARD` (maps to the emulator's own Debuff,
  so `EnemyAI.cs`'s `IntentType.Debuff` was already right — only the model's live spelling
  was wrong; PhrogParasite's INFECT_MOVE already has the same fix). THIEVING_HOPPER's
  pending ESCAPE_MOVE resumes rather than advances across a stun. (`40ef8bd`)

**Cards — the cost-0 lead, confirmed.** Every "shows cost 0 but its printed cost and
fields make N" warning in the log (all acts) is the same cause: the player held
`FreeAttackPower` (Unrelenting) when the card was checked. `cards.py`'s own docstring
already named the family ("Free\*Power") as an unmodelled `EffectiveCost` term.
`_expected_cost` now takes `free_card_types`, computed from the player's live powers
(`FreeAttackPower`/`FreeSkillPower`/`FreePowerPower`, none of which map to a `BuffId` the
converter's buffs already carry) and applied to the **hand only** — the buff is spent by
whichever card is played, not by pile, so cards in discard/draw still show their ordinary
cost. `EffectiveCost`'s own check has no memory of which card asks, so with the buff held
every card of that CardType in hand reads 0, not only the one that will spend it. Test
first (`test_free_attack_power_explains_a_free_attack`). (`00b1419`)

**Step 5** (spire-coach, branch `panel-act1-note`): the panel now shows "No between-fight
advice outside Act 1 yet" for a strategy failure on an Act 2-3 screen, and still shows the
real error for an Act 1 failure (or one before any act is known). (`83ddbda`)

## What remains

**Act 3's one mismatch: `player.powers.DEXTERITY_POWER` (predicted 1, actual 2), on an
`end_turn` crossing into round 3.** `SparklingRouge.AfterBlockCleared` fires on
`TurnNumber == 3` exactly and grants Strength 1 *and* Dexterity 1 in the same method
(`RelicEffects.cs:1997-2000`, both lines present: `GainPlayerStrength` and
`BuffSystem.Apply(..., BuffId.Dexterity, 1)`). The player's Strength (8 -> 12) predicted
correctly across this same transition, so whatever the Strength total's other sources are,
the emulator already gets them right; only the paired Dexterity grant did not land.
Investigated but not root-caused on this budget — worth checking whether something else
touches `BuffId.Dexterity` at the same start-of-turn point and overwrites rather than adds.

**Not fixed, causes traced to a live-snapshot/mod gap** (checked each against the raw mod
capture in the log, not just inferred):

- `powers.py`: **SKITTISH_POWER**'s spent-this-turn flag and **SURROUNDED_POWER**'s facing
  are not present anywhere in the raw state (checked the power's own `keywords` list, which
  is empty/generic for both) — a real capture gap, not a converter one.
- **SWIPE_POWER**'s stolen card *is* present, just not where the converter looks: the raw
  power's `keywords[0].id` carries it as `"CARD.<ENTRY>[+]"` (confirmed: `Fight Me!+` ->
  `CARD.FIGHT_ME+`). A `coach/snapshot.py` change could carry this into the live schema's
  power `fields`; the sts2 side doesn't need anything new once it does.
- **POSSESS_SPEED_POWER/POSSESS_STRENGTH_POWER**'s stolen amount: the raw power's
  `keywords` only carries the power's own static description (e.g. "Dexterity improves
  Block..."), not a quantity — no hiding place found in this log's capture; likely needs a
  new mod-side field.
- `relics.py`: **LIZARD_TAIL**'s `_wasUsed` and **THROWING_AXE**'s `_usedThisCombat` are
  read for but never populated in `fields` in this log (`"fields": {}` on every instance) —
  `_flag`'s existing "field missing" path already covers this gracefully, it just can't
  read the value, so the check stays not-comparable while giving the exact reason.

**"AI state is generic" still gates every check for 6 enemies**, whose move machines are
buff-gated or randomly-branching rather than a `Cycle`: BOWLBUG_ROCK (OffBalance depends on
whether its own attack was blocked, not on the log), SLUMBERING_BEETLE (SNORE vs ROLL_OUT
is the *same* move state, gated on the Slumber buff rather than MoveIndex — `Move` has no
hook for that), THE_OBSCURA, FLAIL_KNIGHT, SPECTRAL_KNIGHT, SCROLL_OF_BITING (all
`rng=True` random-branch monsters), and TEST_SUBJECT (three buff-gated forms, respawn-
parked MoveIndex offsets, a dynamic hit-count formula — the single highest-frequency Act 3
enemy in this log, and the one most worth the next agent's time).

Suites green on their respective branches: `dotnet test` 5411 passed/1 skipped;
sts2 `unittest` 1340 tests, 3 pre-existing failures confirmed unrelated (below);
spire-coach `pytest` 587 passed.

### The 3 sts2 `unittest` failures

- `test_live_search.ConfirmationTests.test_a_slow_first_playout_does_not_end_the_search`
  and `test_live_search.ParallelSearcherTests.test_a_pooled_call_takes_its_budget`: both
  assert on a search's real-time budget. Re-ran isolated (just these two, nothing else of
  mine running) and both still failed — but `uptime` at the time read load averages
  57-64 on a 14-core box, so the machine itself is saturated regardless of what else is in
  *this* process. Consistent with load, not with anything in this diff (neither test's
  file, nor anything it imports, changed on either branch).
- `test_sweep_buff_map.SweepBuffMapTests.test_display_amount_powers_are_compared_by_presence`:
  asserts `TENDER_POWER` is in `combat_sweep.PRESENCE_ONLY_POWERS`, which only has
  `SURROUNDED_POWER`/`POSSESS_SPEED_POWER`/`POSSESS_STRENGTH_POWER`. Confirmed identical at
  `spire-coach`@4f6a485: `git diff 4f6a485 HEAD -- tests/python/test_sweep_buff_map.py
  scripts/combat_sweep.py scripts/buff_map.py data/` is empty on this branch, so the test's
  code and every input it reads are byte-identical to the base commit. Pre-existing.
