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


def test_fight_picker_refuses_an_empty_list():
    import pytest

    with pytest.raises(ValueError, match="no encounters"):
        FightPicker([])
