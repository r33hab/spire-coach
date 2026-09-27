# Fidelity beyond Act 1 — report

Status: Act 1's one mismatch fixed and verified; Acts 2-3 diagnosed in full but not
moved — every cause traces to files outside this agent's assigned area, or to test
infrastructure too large to extend on this budget. Full detail:
`spire-coach/.superpowers/fidelity-act23.md`.

Commits (none merged; left for the controller):
- sts2 `spire-coach-fidelity` (from `spire-coach`, 4f6a485): `272a8ef` diagnose script,
  `7f06f06` Waterfall Giant fix.
- spire-coach `panel-act1-note` (from `dev`): `83ddbda` panel note.

Comparable / mismatched, before -> after:
- Act 1: 44/62 comparable (43 matched, 1 mismatched) -> 44/62 (44 matched, 0 mismatched).
- Act 2: 0/69 -> 0/69. Act 3: 0/129 -> 0/129.

What remains: Acts 2-3 are gated almost entirely by `relics.py` (PAELS_WING/THROWING_AXE
have no converter entry — every Act 2-3 check carries that warning) and `powers.py`
(SURROUNDED/SWIPE/POSSESS_* fields missing from the live snapshot schema itself), neither
in my area. `enemy_ai.py`'s "AI state is generic" (~20 Act 2-3 kinds, none with an
`EnemyAI.cs` case) is mine but blocked: a trial Crusher model checked out against
decompiled source, but two `test_live_enemy_ai.py` aggregate tests hard-assume an
Act-1-only, fully-fixtured roster; reverted rather than leave the suite red. `cards.py`'s
cost-0 discrepancy (several attacks, every act) is a lead, not a diagnosis. All three
suites green.
