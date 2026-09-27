# Fidelity beyond Act 1 — report

Status: scope expanded mid-task (relics.py/powers.py/cards.py/enemy_ai.py test infra
authorized). Acts 2-3 unblocked: 0 -> 8 and 0 -> 48 comparable. Full detail:
`spire-coach/.superpowers/fidelity-act23.md`.

Commits (none merged; left for the controller), all on sts2 `spire-coach-fidelity`
(from `spire-coach`@4f6a485): `272a8ef` diagnose script, `7f06f06` Waterfall Giant fix,
`6df707a` relics (PAELS_WING/THROWING_AXE), `e5b6c5e` 14 enemy AI models + test-infra
fix, `2cd72d7` Rage unpowered-block fix, `00b1419` cards.py Free\*Power cost, `40ef8bd`
2 more enemy-AI corrections. spire-coach `panel-act1-note` (from `dev`): `83ddbda` panel.

Comparable/mismatched, start -> final: Act 1 44/62 (43m,1mm) -> 46/62 (46m,0). Act 2
0/69 -> 8/69 (8m,0). Act 3 0/129 -> 48/129 (47m,1mm).

What remains: 1 Act 3 mismatch (`player.powers.DEXTERITY_POWER`) traced to
SparklingRouge's turn-3 grant, Strength lands but Dexterity doesn't, not root-caused.
6 enemies still ungated (buff-gated or random-branch machines `Cycle` can't express;
TEST_SUBJECT is the highest-value next one). Several powers/relics confirmed as
live-snapshot/mod gaps, not converter gaps (checked each against the raw capture, not
just inferred) — SWIPE_POWER's stolen card is recoverable from the mod's own data
(`keywords[].id`), the rest need a new mod field. Suites green: dotnet test
5411/1 skipped; sts2 unittest 1340, 3 pre-existing failures (2 confirmed load-sensitive
via `uptime` load 57-64/14 cores; 1 confirmed byte-identical to spire-coach@4f6a485);
spire-coach pytest 587.
