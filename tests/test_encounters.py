"""Tests for coach/encounters.py: picking which real fight to play next."""

from __future__ import annotations

from coach.encounters import (
    BOSS,
    ELITE,
    ENCOUNTERS,
    MONSTER,
    Encounter,
    FightPicker,
    rank_encounters,
)


def test_encounters_are_only_real_debug_start_encounter_names():
    # A regression guard against a typo creeping into the roster: every name is
    # distinct and every one has a valid tier and act.
    names = [e.name for e in ENCOUNTERS]
    assert len(names) == len(set(names))
    for encounter in ENCOUNTERS:
        assert encounter.act in (1, 2, 3)
        assert encounter.tier in (MONSTER, ELITE, BOSS)


def test_rank_encounters_prefers_the_act_with_fewer_comparable_checks():
    doc = {
        "totals": {
            "act 1": {"totals": {"comparable": 200}},
            "act 2": {"totals": {"comparable": 10}},
            "act 3": {"totals": {"comparable": 100}},
        }
    }
    ranked = rank_encounters(doc, acts=(1, 2, 3))
    acts_in_order = [e.act for e in ranked]
    # Act 2 (fewest comparable) should be entirely ahead of act 3, which is ahead of act 1.
    assert acts_in_order.index(2) < acts_in_order.index(3) < acts_in_order.index(1)


def test_rank_encounters_puts_a_never_captured_act_first():
    doc = {"totals": {"act 2": {"totals": {"comparable": 5}}}}
    ranked = rank_encounters(doc, acts=(2, 3))
    assert ranked[0].act == 3  # act 3 never appears in the doc: treated as least-covered


def test_rank_encounters_prefers_bosses_and_elites_within_an_act():
    ranked = rank_encounters(None, acts=(2,))
    tiers_seen_in_order = [e.tier for e in ranked]
    first_monster_index = tiers_seen_in_order.index(MONSTER)
    assert BOSS in tiers_seen_in_order[:first_monster_index]
    assert ELITE in tiers_seen_in_order[:first_monster_index]


def test_rank_encounters_excludes_acts_not_asked_for():
    ranked = rank_encounters(None, acts=(1,))
    assert all(e.act == 1 for e in ranked)


def test_fight_picker_cycles_through_the_ranked_list():
    a = Encounter("A", 2, "Hive", MONSTER)
    b = Encounter("B", 2, "Hive", MONSTER)
    picker = FightPicker([a, b])
    picked = [picker.next() for _ in range(5)]
    assert picked == [a, b, a, b, a]


def test_fight_picker_reprioritizes_from_moves_recorded_this_session():
    a = Encounter("A", 2, "Hive", BOSS)
    b = Encounter("B", 2, "Hive", BOSS)
    c = Encounter("C", 2, "Hive", BOSS)
    picker = FightPicker([a, b, c])
    assert picker.next() is a  # the static rank's own first pick
    picker.record(a, moves=5)  # a died fast: still the fewest UNLESS something untried exists
    assert picker.next() is b  # b (0 moves) beats a (5), and is not the one just played
    picker.record(b, moves=50)  # b lasted a long fight
    assert picker.next() is c  # c (0 moves) is still untried, ahead of both a and b
    picker.record(c, moves=1)
    # every encounter has now been tried at least once; the lowest total (a, 5) wins,
    # and it is not "c" (the one just played), so no back-to-back repeat either.
    assert picker.next() is a


def test_fight_picker_never_repeats_the_previous_encounter_when_another_exists():
    a = Encounter("A", 2, "Hive", BOSS)
    b = Encounter("B", 2, "Hive", BOSS)
    picker = FightPicker([a, b])
    picker.record(a, moves=0)
    picker.record(b, moves=0)  # tied at 0 moves each: rank order alone would repeat "a"
    first = picker.next()
    second = picker.next()
    assert first is not second


def test_fight_picker_moves_on_from_a_wall_once_it_hits_the_attempt_cap():
    # Item 7 ("no hopeless repeats" / "move on from walls"): The Insatiable kills fast
    # and repeatably, which keeps making it look like the biggest gap to the plain
    # least-moves rule (test_fight_picker_reprioritizes_from_moves_recorded_this_session)
    # -- it must stop being offered once it has used up its attempts, even though it
    # would otherwise keep winning on move count alone.
    wall = Encounter("TheInsatiableBoss", 2, "Hive", BOSS)
    other = Encounter("KaiserCrabBoss", 2, "Hive", BOSS)
    picker = FightPicker([wall, other], max_attempts=2)
    picker.record(wall, moves=5)  # attempt 1: a fast, cheap-looking loss
    assert picker.next() is other
    picker.record(other, moves=50)  # a real, longer fight
    picker.record(wall, moves=6)  # attempt 2: the wall hits its cap
    # the wall has far fewer total moves (11 vs 50) and would normally win every tie,
    # but every remaining pick must go to "other" instead for the rest of the session.
    assert [picker.next() for _ in range(4)] == [other] * 4


def test_fight_picker_falls_back_to_a_capped_encounter_if_nothing_else_is_left():
    only = Encounter("TheInsatiableBoss", 2, "Hive", BOSS)
    picker = FightPicker([only], max_attempts=1)
    picker.record(only, moves=5)  # capped, but it is the only encounter there is
    assert picker.next() is only  # retrying a wall beats raising out of fights to play


def test_fight_picker_default_cap_is_generous_but_finite():
    wall = Encounter("TheInsatiableBoss", 2, "Hive", BOSS)
    other = Encounter("KaiserCrabBoss", 2, "Hive", BOSS)
    picker = FightPicker([wall, other])
    picker.record(other, moves=100)  # so "other" never just looks more urgent on moves
    for _ in range(FightPicker.DEFAULT_MAX_ATTEMPTS - 1):
        picker.record(wall, moves=1)
    assert picker.next() is wall  # still under the default cap, and cheaper than "other"
    picker.record(wall, moves=1)  # this attempt reaches the default cap
    assert picker.next() is other  # now excluded regardless of move totals


def test_fight_picker_refuses_an_empty_list():
    import pytest

    with pytest.raises(ValueError, match="no encounters"):
        FightPicker([])
