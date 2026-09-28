"""The real game's Act 1-3 encounter roster, and how the fight lab picks among it.

Sourced from the game's own act definitions (sts2's decompiled
`MegaCrit.Sts2.Core.Models.Acts/*.cs`, `GenerateAllEncounters()` and `BossDiscoveryOrder`),
not guessed or hand-transcribed from memory: every `name` below is a real `EncounterModel`
class name, exactly what `debug_start_encounter`'s `encounter` field takes. Act 1 is two
zones the game picks between per run (Overgrowth, Underdocks; both `ActModel.Index == 0`);
Act 2 (Hive) and Act 3 (Glory) have one zone each. Event-only and deprecated encounters
(FakeMerchant, MysteriousKnight, BattlewornDummy, PunchOff, DenseVegetation, ...) are left
out: a map node never offers them, so they are not what "Act 2-3, elites and bosses first,
then the rest" means.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

MONSTER = "monster"
ELITE = "elite"
BOSS = "boss"
_TIER_RANK = {BOSS: 0, ELITE: 1, MONSTER: 2}


@dataclass(frozen=True)
class Encounter:
    """One encounter `debug_start_encounter` can jump straight to."""

    name: str
    act: int  # 1-3, matching the fidelity scoreboard's "act 1"/"act 2"/"act 3" keys
    zone: str
    tier: str  # monster | elite | boss


ENCOUNTERS: tuple[Encounter, ...] = (
    # --- Act 1: Overgrowth (decompiled/.../Acts/Overgrowth.cs) ---
    Encounter("CubexConstructNormal", 1, "Overgrowth", MONSTER),
    Encounter("FlyconidNormal", 1, "Overgrowth", MONSTER),
    Encounter("FogmogNormal", 1, "Overgrowth", MONSTER),
    Encounter("FuzzyWurmCrawlerWeak", 1, "Overgrowth", MONSTER),
    Encounter("InkletsNormal", 1, "Overgrowth", MONSTER),
    Encounter("MawlerNormal", 1, "Overgrowth", MONSTER),
    Encounter("NibbitsNormal", 1, "Overgrowth", MONSTER),
    Encounter("NibbitsWeak", 1, "Overgrowth", MONSTER),
    Encounter("OvergrowthCrawlers", 1, "Overgrowth", MONSTER),
    Encounter("RubyRaidersNormal", 1, "Overgrowth", MONSTER),
    Encounter("ShrinkerBeetleWeak", 1, "Overgrowth", MONSTER),
    Encounter("SlimesNormal", 1, "Overgrowth", MONSTER),
    Encounter("SlimesWeak", 1, "Overgrowth", MONSTER),
    Encounter("SlitheringStranglerNormal", 1, "Overgrowth", MONSTER),
    Encounter("SnappingJaxfruitNormal", 1, "Overgrowth", MONSTER),
    Encounter("VineShamblerNormal", 1, "Overgrowth", MONSTER),
    Encounter("BygoneEffigyElite", 1, "Overgrowth", ELITE),
    Encounter("ByrdonisElite", 1, "Overgrowth", ELITE),
    Encounter("PhrogParasiteElite", 1, "Overgrowth", ELITE),
    Encounter("CeremonialBeastBoss", 1, "Overgrowth", BOSS),
    Encounter("TheKinBoss", 1, "Overgrowth", BOSS),
    Encounter("VantomBoss", 1, "Overgrowth", BOSS),
    # --- Act 1: Underdocks (decompiled/.../Acts/Underdocks.cs) ---
    Encounter("CorpseSlugsNormal", 1, "Underdocks", MONSTER),
    Encounter("CorpseSlugsWeak", 1, "Underdocks", MONSTER),
    Encounter("CultistsNormal", 1, "Underdocks", MONSTER),
    Encounter("FossilStalkerNormal", 1, "Underdocks", MONSTER),
    Encounter("GremlinMercNormal", 1, "Underdocks", MONSTER),
    Encounter("HauntedShipNormal", 1, "Underdocks", MONSTER),
    Encounter("LivingFogNormal", 1, "Underdocks", MONSTER),
    Encounter("PunchConstructNormal", 1, "Underdocks", MONSTER),
    Encounter("SeapunkNormal", 1, "Underdocks", MONSTER),
    Encounter("SeapunkWeak", 1, "Underdocks", MONSTER),
    Encounter("SewerClamNormal", 1, "Underdocks", MONSTER),
    Encounter("SludgeSpinnerWeak", 1, "Underdocks", MONSTER),
    Encounter("ToadpolesWeak", 1, "Underdocks", MONSTER),
    Encounter("TwoTailedRatsNormal", 1, "Underdocks", MONSTER),
    Encounter("PhantasmalGardenersElite", 1, "Underdocks", ELITE),
    Encounter("SkulkingColonyElite", 1, "Underdocks", ELITE),
    Encounter("TerrorEelElite", 1, "Underdocks", ELITE),
    Encounter("LagavulinMatriarchBoss", 1, "Underdocks", BOSS),
    Encounter("SoulFyshBoss", 1, "Underdocks", BOSS),
    Encounter("WaterfallGiantBoss", 1, "Underdocks", BOSS),
    # --- Act 2: Hive (decompiled/.../Acts/Hive.cs) ---
    Encounter("BowlbugsNormal", 2, "Hive", MONSTER),
    Encounter("BowlbugsWeak", 2, "Hive", MONSTER),
    Encounter("ChompersNormal", 2, "Hive", MONSTER),
    Encounter("ExoskeletonsNormal", 2, "Hive", MONSTER),
    Encounter("ExoskeletonsWeak", 2, "Hive", MONSTER),
    Encounter("HunterKillerNormal", 2, "Hive", MONSTER),
    Encounter("LouseProgenitorNormal", 2, "Hive", MONSTER),
    Encounter("MytesNormal", 2, "Hive", MONSTER),
    Encounter("OvicopterNormal", 2, "Hive", MONSTER),
    Encounter("SlumberingBeetleNormal", 2, "Hive", MONSTER),
    Encounter("SpinyToadNormal", 2, "Hive", MONSTER),
    Encounter("TheObscuraNormal", 2, "Hive", MONSTER),
    Encounter("ThievingHopperWeak", 2, "Hive", MONSTER),
    Encounter("TunnelerWeak", 2, "Hive", MONSTER),
    Encounter("DecimillipedeElite", 2, "Hive", ELITE),
    Encounter("EntomancerElite", 2, "Hive", ELITE),
    Encounter("InfestedPrismsElite", 2, "Hive", ELITE),
    Encounter("KaiserCrabBoss", 2, "Hive", BOSS),
    Encounter("KnowledgeDemonBoss", 2, "Hive", BOSS),
    Encounter("TheInsatiableBoss", 2, "Hive", BOSS),
    # --- Act 3: Glory (decompiled/.../Acts/Glory.cs) ---
    Encounter("AxebotsNormal", 3, "Glory", MONSTER),
    Encounter("ConstructMenagerieNormal", 3, "Glory", MONSTER),
    Encounter("DevotedSculptorWeak", 3, "Glory", MONSTER),
    Encounter("FabricatorNormal", 3, "Glory", MONSTER),
    Encounter("FrogKnightNormal", 3, "Glory", MONSTER),
    Encounter("GlobeHeadNormal", 3, "Glory", MONSTER),
    Encounter("OwlMagistrateNormal", 3, "Glory", MONSTER),
    Encounter("ScrollsOfBitingNormal", 3, "Glory", MONSTER),
    Encounter("ScrollsOfBitingWeak", 3, "Glory", MONSTER),
    Encounter("SlimedBerserkerNormal", 3, "Glory", MONSTER),
    Encounter("TheLostAndForgottenNormal", 3, "Glory", MONSTER),
    Encounter("TurretOperatorWeak", 3, "Glory", MONSTER),
    Encounter("KnightsElite", 3, "Glory", ELITE),
    Encounter("MechaKnightElite", 3, "Glory", ELITE),
    Encounter("SoulNexusElite", 3, "Glory", ELITE),
    Encounter("AeonglassBoss", 3, "Glory", BOSS),
    Encounter("QueenBoss", 3, "Glory", BOSS),
    Encounter("TestSubjectBoss", 3, "Glory", BOSS),
)

# A10 (`Ascension.DoubleBoss`) gives Act 3 a second boss, fought on its own floor right
# after the first (RunState.SecondBoss). debug_start_encounter jumps to ONE named
# encounter at a time, so the lab approximates the double-boss floor by cycling through
# every one of Act 3's three possible bosses at ascension 10, rather than staging two
# bosses in a single CombatRoom -- documented limit, not a faithful two-at-once replay.


def _act_priority(fidelity_doc: dict[str, Any] | None, acts: Sequence[int]) -> dict[int, int]:
    """Each wanted act's sort key: lower goes first. An act absent from the scoreboard
    (never captured at all) or with no comparable checks yet sorts before any act that
    merely has few, so a first run visits what's never been seen before what's thin."""
    totals = (fidelity_doc or {}).get("totals") or {}
    priority = {}
    for act in acts:
        bucket = totals.get(f"act {act}")
        comparable = bucket.get("totals", {}).get("comparable") if bucket else None
        priority[act] = -1 if comparable is None else comparable
    return priority


def rank_encounters(
    fidelity_doc: dict[str, Any] | None,
    *,
    acts: Sequence[int] = (2, 3, 1),
    encounters: Sequence[Encounter] = ENCOUNTERS,
) -> list[Encounter]:
    """Every encounter in `acts`, most useful to fight first: the act with the fewest
    comparable checks in `fidelity_doc` first, elites and bosses before plain monsters
    within an act, then alphabetically for a stable, reproducible order.
    """
    wanted = set(acts)
    priority = _act_priority(fidelity_doc, acts)
    pool = [encounter for encounter in encounters if encounter.act in wanted]
    return sorted(
        pool,
        key=lambda e: (priority[e.act], _TIER_RANK[e.tier], e.zone, e.name),
    )


class FightPicker:
    """Which encounter to fight next: `rank_encounters`' order, repeated. A fight-lab
    session plays many more fights than there are encounters, so this cycles the
    ranked list rather than hammering rank 1 forever -- a long session still visits
    every encounter roughly evenly, while a short one still spends its fights on the
    encounters that need them most.
    """

    def __init__(self, ranked: Sequence[Encounter]) -> None:
        if not ranked:
            raise ValueError("no encounters to choose from (check --acts)")
        self._ranked = list(ranked)
        self._next = 0

    def __len__(self) -> int:
        return len(self._ranked)

    def next(self) -> Encounter:
        encounter = self._ranked[self._next % len(self._ranked)]
        self._next += 1
        return encounter
