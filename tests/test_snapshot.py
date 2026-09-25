"""Tests for the live snapshot builder (mod JSON -> sts2-live-snapshot/1).

Some expectations (state shapes, key names, the intent-type table) are
written out fresh from docs/superpowers/specs/2026-09-24-snapshot-schemas.md
section 1 and the decompiled IntentType enum, rather than imported from
coach.snapshot's own constants, so a typo in the implementation can't hide
from these tests (the same independence convention tests/test_decisions.py
already uses).
"""

from __future__ import annotations

import ast
import copy
import json
import os
from pathlib import Path

import pytest

from coach.snapshot import SnapshotError, build_live_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "mod"
CONVERTER_REPO = Path(
    os.environ.get("SPIRE_SIM_REPO", "/Users/you/Documents/git/sts2-wt/convert")
)
CONVERTER_LIVE_FIXTURES = CONVERTER_REPO / "tests" / "python" / "fixtures" / "live"
CONVERTER_IDS_PY = CONVERTER_REPO / "src" / "sts2_gym" / "live" / "ids.py"

GAME_VERSION = "v0.107.1"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def combat_state() -> dict:
    return load("combat_full.json")


@pytest.fixture
def hand_select_state() -> dict:
    return load("combat_hand_select.json")


@pytest.fixture
def hand_select_with_selection_state() -> dict:
    return load("combat_hand_select_with_selection.json")


@pytest.fixture
def card_select_grid_state() -> dict:
    return load("combat_card_select_grid.json")


@pytest.fixture
def card_select_choose_state() -> dict:
    return load("combat_card_select_choose.json")


# ---------------------------------------------------------------------------
# Full combat fixture -> exact expected snapshot
# ---------------------------------------------------------------------------


def test_full_combat_fixture_produces_exact_expected_snapshot(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)

    assert snapshot == {
        "schema": "sts2-live-snapshot/1",
        "game_version": "v0.107.1",
        "character": "IRONCLAD",
        "ascension": 3,
        "act": 1,
        "floor": 6,
        "room_type": "monster",
        "round": 2,
        "player": {
            "hp": 61,
            "max_hp": 80,
            "block": 5,
            "energy": 2,
            "max_energy": 3,
            "gold": 132,
            "stars": 0,
            "osty": None,
            "orbs": {"slots": 0, "list": []},
            "powers": [{"id": "STRENGTH_POWER", "amount": 2}],
            "relics": [
                {"id": "BURNING_BLOOD", "counter": None, "fields": {}},
                {
                    "id": "KUNAI",
                    "counter": 2,
                    "fields": {"_attacksPlayedThisTurn": 2},
                },
                {
                    "id": "PEN_NIB",
                    "counter": 9,
                    "fields": {"_attacksPlayed": 19, "AttacksPlayed": 19},
                },
            ],
            "potions": [
                {"slot": 0, "id": None},
                {"slot": 1, "id": "FIRE_POTION"},
                {"slot": 2, "id": None},
            ],
            "max_potion_slots": 3,
        },
        "hand": [
            {
                "id": "STRIKE_IRONCLAD",
                "upgraded": False,
                "uid": "c1",
                "cost": 1,
                "enchantment": "SHARP",
                "enchant_amount": 3,
                "fields": {},
            },
            {
                "id": "WHIRLWIND",
                "upgraded": False,
                "uid": "c2",
                "cost": None,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "ASCENDERS_BANE",
                "upgraded": False,
                "uid": "c3",
                "cost": None,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "TRUE_GRIT",
                "upgraded": False,
                "uid": "c4",
                "cost": 0,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {"cost_this_turn": 0},
            },
        ],
        "draw_pile": [
            {
                "id": "ANGER",
                "upgraded": False,
                "uid": "c9",
                "cost": 0,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "ASCENDERS_BANE",
                "upgraded": False,
                "uid": "c13",
                "cost": None,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "BASH",
                "upgraded": False,
                "uid": "c8",
                "cost": 2,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "DEFEND_IRONCLAD",
                "upgraded": False,
                "uid": "c6",
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "DEFEND_IRONCLAD",
                "upgraded": False,
                "uid": "c7",
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "STRIKE_IRONCLAD",
                "upgraded": True,
                "uid": "c5",
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
        ],
        "discard_pile": [
            {
                "id": "STRIKE_IRONCLAD",
                "upgraded": False,
                "uid": "c11",
                "cost": 1,
                "enchantment": "NIMBLE",
                "enchant_amount": 2,
                "fields": {},
            },
            {
                "id": "IRON_WAVE",
                "upgraded": False,
                "uid": "c10",
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
        ],
        "exhaust_pile": [
            {
                "id": "TRUE_GRIT",
                "upgraded": False,
                "uid": "c12",
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
        ],
        "known_draw_top": [],
        "known_draw_bottom": [],
        "enemies": [
            {
                "entity_id": "JAW_WORM_0",
                "id": "JAW_WORM",
                "slot": 0,
                "hp": 30,
                "max_hp": 44,
                "block": 4,
                "powers": [{"id": "STRENGTH_POWER", "amount": 3}],
                "intents": [
                    {"type": "ATTACK", "damage": 11, "base_damage": 8, "hits": 1},
                    {"type": "DEATH_BLOW", "damage": 20, "base_damage": 15, "hits": 1},
                    {"type": "STATUS_CARD", "damage": None, "base_damage": None, "hits": None},
                ],
                "move_id": "CHOMP_MOVE",
                "state_log": ["BELLOW_MOVE", "CHOMP_MOVE"],
                "ai_fields": {"Anger": True, "BellowStrength": 3},
            },
            {
                "entity_id": "DECIMILLIPEDE_SEGMENT_1",
                "id": "DECIMILLIPEDE_SEGMENT",
                "slot": 1,
                "hp": 0,
                "max_hp": 46,
                "block": 0,
                "powers": [{"id": "REATTACH_POWER", "amount": 1}],
                "intents": [],
                "move_id": "DEAD_MOVE",
                "state_log": [],
                "ai_fields": {},
            },
        ],
        "history": {
            "cards_played_this_turn": 2,
            "cards_played_this_combat": 9,
            "attacks_played_this_turn": 1,
            "skills_played_this_turn": 1,
        },
        "pending_selection": None,
    }


# ---------------------------------------------------------------------------
# Hidden information: draw_pile_ordered must never affect the output
# ---------------------------------------------------------------------------


def test_hidden_draw_order_never_leaks(combat_state):
    # (I2) Compare WHOLE snapshots, not just draw_pile.
    with_order = build_live_snapshot(combat_state, game_version=GAME_VERSION)

    scrambled = copy.deepcopy(combat_state)
    scrambled["player"]["draw_pile_ordered"] = list(
        reversed(scrambled["player"]["draw_pile_ordered"])
    )
    reordered = build_live_snapshot(scrambled, game_version=GAME_VERSION)

    removed = copy.deepcopy(combat_state)
    del removed["player"]["draw_pile_ordered"]
    without_order = build_live_snapshot(removed, game_version=GAME_VERSION)

    assert with_order == reordered == without_order

    # Sanity: the fixture's own draw_pile_ordered really does disagree with
    # the sorted multiset, so this test is exercising something real.
    ordered_ids = [c["id"] for c in combat_state["player"]["draw_pile_ordered"]]
    sorted_ids = [c["id"] for c in with_order["draw_pile"]]
    assert ordered_ids != sorted_ids

    # And the sorted multiset really is sorted (id is the primary key).
    ids = [c["id"] for c in with_order["draw_pile"]]
    assert ids == sorted(ids)


def test_snapshot_never_reads_draw_pile_ordered_key(combat_state):
    """Belt and braces: a present-but-bogus draw_pile_ordered (non-list
    garbage that would crash anything that touched it) changes nothing."""
    poisoned = copy.deepcopy(combat_state)
    poisoned["player"]["draw_pile_ordered"] = "not-even-a-list"
    snapshot = build_live_snapshot(poisoned, game_version=GAME_VERSION)
    assert len(snapshot["draw_pile"]) == 6


# ---------------------------------------------------------------------------
# hand_select: room_type must still be supplied externally (its own
# state_type is not a room type). `combat_hand_select.json` predates mod
# Task 6c's `selection` object, so it is this suite's "older mod" fixture:
# nothing is invented, and the gap is reported as a warning instead of an
# error (task-11b requirement 4; supersedes Task 11's I3 hard-raise ruling,
# now that the mod can report a selection at all for the states that have
# one).
# ---------------------------------------------------------------------------


def test_hand_select_without_room_type_raises_room_type_error(hand_select_state):
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(hand_select_state, game_version=GAME_VERSION)
    assert exc_info.value.path == "room_type"


def test_hand_select_without_selection_object_returns_null_pending_selection_with_warning(
    hand_select_state,
):
    warnings: list[str] = []
    snapshot = build_live_snapshot(
        hand_select_state, game_version=GAME_VERSION, room_type="monster", warnings=warnings
    )
    assert snapshot["pending_selection"] is None
    assert len(warnings) == 1
    assert "selection" in warnings[0]


# ---------------------------------------------------------------------------
# pending_selection: schema section 1.1, built from the mod's `selection`
# object (task-11b requirements 3/4) plus the controller-ruling `played_card`
# (built from player.play_pile, matched by selection.source.uid).
# ---------------------------------------------------------------------------


def test_pending_selection_for_hand_with_a_toggled_card_and_its_played_card(
    hand_select_with_selection_state,
):
    snapshot = build_live_snapshot(
        hand_select_with_selection_state, game_version=GAME_VERSION, room_type="monster"
    )
    assert snapshot["pending_selection"] == {
        "kind": "HAND",
        "pile": None,
        "prompt_key": "card_selection.TO_EXHAUST",
        "source_card": "BURNING_PACT",
        "source_potion": None,
        "source_relic": None,
        "played_card": {
            "id": "BURNING_PACT",
            "upgraded": False,
            "uid": "BURNING_PACT#0",
            "cost": 1,
            "enchantment": None,
            "enchant_amount": None,
            "fields": {},
        },
        "candidates": [
            {
                "id": "DEFEND_IRONCLAD",
                "upgraded": False,
                "uid": "DEFEND_IRONCLAD#3",
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "STRIKE_IRONCLAD",
                "upgraded": True,
                "uid": "STRIKE_IRONCLAD#2",
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
        ],
        "min": 1,
        "max": 1,
        "can_skip": False,
        "selected_uids": ["DEFEND_IRONCLAD#3"],
    }


def test_pending_selection_for_grid_over_the_discard_pile_with_no_play_pile(card_select_grid_state):
    """This fixture reports no player.play_pile at all: a card-sourced
    selection with nothing to match against still resolves played_card to
    null, with a warning (controller ruling, third test case)."""
    warnings: list[str] = []
    snapshot = build_live_snapshot(
        card_select_grid_state, game_version=GAME_VERSION, room_type="monster", warnings=warnings
    )
    assert snapshot["pending_selection"] == {
        "kind": "GRID",
        "pile": "DISCARD",
        "prompt_key": "cards.HEADBUTT.selectionScreenPrompt",
        "source_card": "HEADBUTT",
        "source_potion": None,
        "source_relic": None,
        "played_card": None,
        "candidates": [
            {
                "id": "IRON_WAVE",
                "upgraded": False,
                "uid": "IRON_WAVE#0",
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "SHRUG_IT_OFF",
                "upgraded": False,
                "uid": "SHRUG_IT_OFF#0",
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
        ],
        "min": 1,
        "max": 1,
        "can_skip": False,
        "selected_uids": [],
    }
    assert len(warnings) == 1
    assert "play_pile" in warnings[0]


def test_pending_selection_for_choose_with_generated_cards_can_skip_and_a_potion_source(
    card_select_choose_state,
):
    snapshot = build_live_snapshot(
        card_select_choose_state, game_version=GAME_VERSION, room_type="monster"
    )
    assert snapshot["pending_selection"] == {
        "kind": "CHOOSE",
        "pile": None,
        "prompt_key": "gameplay_ui.CHOOSE_CARD_HEADER",
        "source_card": None,
        "source_potion": "ATTACK_POTION",
        "source_relic": None,
        "played_card": None,
        "candidates": [
            {
                "id": "BLUDGEON",
                "upgraded": False,
                "uid": "BLUDGEON#0",
                "cost": 3,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "UPPERCUT",
                "upgraded": False,
                "uid": "UPPERCUT#0",
                "cost": 2,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
        ],
        "min": 0,
        "max": 1,
        "can_skip": True,
        "selected_uids": [],
    }


def test_pending_selection_power_source_warns_and_nulls_all_three_source_fields(
    hand_select_with_selection_state,
):
    mutated = copy.deepcopy(hand_select_with_selection_state)
    mutated["hand_select"]["selection"]["source"] = {
        "type": "power",
        "id": "ENTROPY_POWER",
        "uid": None,
    }
    warnings: list[str] = []
    snapshot = build_live_snapshot(
        mutated, game_version=GAME_VERSION, room_type="monster", warnings=warnings
    )
    pending = snapshot["pending_selection"]
    assert pending["source_card"] is None
    assert pending["source_potion"] is None
    assert pending["source_relic"] is None
    assert pending["played_card"] is None
    assert len(warnings) == 1
    assert "power" in warnings[0]


def test_pending_selection_unknown_source_warns_and_nulls_all_three_source_fields(
    hand_select_with_selection_state,
):
    mutated = copy.deepcopy(hand_select_with_selection_state)
    mutated["hand_select"]["selection"]["source"] = {"type": "unknown", "id": None, "uid": None}
    warnings: list[str] = []
    snapshot = build_live_snapshot(
        mutated, game_version=GAME_VERSION, room_type="monster", warnings=warnings
    )
    pending = snapshot["pending_selection"]
    assert pending["source_card"] is None
    assert pending["source_potion"] is None
    assert pending["source_relic"] is None
    assert len(warnings) == 1
    assert "unknown" in warnings[0]


def test_pending_selection_card_source_with_no_matching_play_pile_card_warns(
    hand_select_with_selection_state,
):
    mutated = copy.deepcopy(hand_select_with_selection_state)
    mutated["player"]["play_pile"] = [
        {
            "uid": "SOME_OTHER_CARD#0",
            "name": "Some Other Card",
            "cost": "1",
            "star_cost": None,
            "description": "...",
            "id": "SOME_OTHER_CARD",
            "is_upgraded": False,
            "upgrade_level": 0,
            "enchantment": None,
            "affliction": None,
            "fields": {},
            "unplayable": False,
        }
    ]
    warnings: list[str] = []
    snapshot = build_live_snapshot(
        mutated, game_version=GAME_VERSION, room_type="monster", warnings=warnings
    )
    assert snapshot["pending_selection"]["played_card"] is None
    assert len(warnings) == 1
    assert "play_pile" in warnings[0]


def test_non_list_play_pile_raises_cleanly(hand_select_with_selection_state):
    mutated = copy.deepcopy(hand_select_with_selection_state)
    mutated["player"]["play_pile"] = "not-even-a-list"
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION, room_type="monster")
    assert exc_info.value.path == "player.play_pile"


# ---------------------------------------------------------------------------
# Room type / state type (M4)
# ---------------------------------------------------------------------------


def test_unrecognized_state_type_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["state_type"] = "rewards"
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "state_type"


def test_room_type_override_disagreeing_with_state_type_raises(combat_state):
    # combat_state's own state_type is "monster"; an explicit room_type may
    # only fill the gap for hand_select, never override a combat state.
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(combat_state, game_version=GAME_VERSION, room_type="boss")
    assert exc_info.value.path == "room_type"


def test_room_type_matching_state_type_is_accepted(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION, room_type="monster")
    assert snapshot["room_type"] == "monster"


def test_no_alive_or_reviving_enemy_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    for enemy in mutated["battle"]["enemies"]:
        enemy["alive"] = False
        enemy.pop("revive", None)
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "enemies"


# ---------------------------------------------------------------------------
# Reviving enemy kept, dead-without-revive enemy dropped
# ---------------------------------------------------------------------------


def test_reviving_enemy_kept_dead_enemy_dropped(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    ids = [e["entity_id"] for e in snapshot["enemies"]]
    assert "DECIMILLIPEDE_SEGMENT_1" in ids  # dead, but has a revive block
    assert "SPHERIC_GUARDIAN_2" not in ids  # dead, no revive block
    assert "JAW_WORM_0" in ids  # alive


# ---------------------------------------------------------------------------
# Card cost: X-cost, and unplayable based on the card's own `unplayable` flag
# (task-11b requirement 5), now present on every card object including pile
# cards. `combat_full.json` predates the flag everywhere (an older-mod
# fixture for this purpose), so it exercises the negative-cost fallback;
# dedicated tests below exercise the modern flag explicitly, in both hand and
# pile, and prove the flag -- not the sign -- has the final say.
# ---------------------------------------------------------------------------


def test_x_cost_card_has_null_cost(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    whirlwind = next(c for c in snapshot["hand"] if c["id"] == "WHIRLWIND")
    assert whirlwind["cost"] is None


def test_negative_cost_without_unplayable_flag_has_null_cost_in_hand(combat_state):
    """Older mod (no `unplayable` key anywhere): the fallback nulls a
    negative displayed cost in the hand, same as the removed unplayable_reason
    check used to."""
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    bane = next(c for c in snapshot["hand"] if c["id"] == "ASCENDERS_BANE")
    assert bane["cost"] is None


def test_negative_cost_without_unplayable_flag_has_null_cost_in_pile_too(combat_state):
    """Older mod, pile card: the same fallback now also nulls a curse sitting
    in a pile, since the fallback no longer depends on a hand-only signal
    (supersedes Task 11's "no signal to null it on" finding)."""
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    bane = next(c for c in snapshot["draw_pile"] if c["id"] == "ASCENDERS_BANE")
    assert bane["cost"] is None


def test_unplayable_true_flag_nulls_cost_even_if_positive(combat_state):
    """The modern flag is authoritative, independent of the sign: a
    positive-cost card explicitly flagged unplayable is still nulled."""
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["hand"][0]["unplayable"] = True  # STRIKE_IRONCLAD, cost "1"
    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION)
    strike = next(c for c in snapshot["hand"] if c["id"] == "STRIKE_IRONCLAD")
    assert strike["cost"] is None


def test_unplayable_false_flag_keeps_negative_cost(combat_state):
    """The modern flag overrides the negative-cost fallback too: `false`
    means playable even if the displayed cost is (temporarily) negative."""
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["hand"][2]["unplayable"] = False  # ASCENDERS_BANE, cost "-2"
    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION)
    bane = next(c for c in snapshot["hand"] if c["id"] == "ASCENDERS_BANE")
    assert bane["cost"] == -2


def test_cost_until_played_is_dropped(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["hand"][3]["fields"] = {
        "cost_this_turn": 0,
        "cost_until_played": 1,
        "cost_for_combat": 2,
    }
    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION)
    true_grit = next(c for c in snapshot["hand"] if c["id"] == "TRUE_GRIT")
    assert true_grit["fields"] == {"cost_this_turn": 0, "cost_for_combat": 2}


# ---------------------------------------------------------------------------
# Character: player.character_id is locale-independent (task-11b requirement
# 1); an older mod without it falls back to mapping the English title, with
# a warning, and anything unmappable raises.
# ---------------------------------------------------------------------------


def test_character_id_is_used_directly(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    assert snapshot["character"] == "IRONCLAD"


def test_missing_character_id_falls_back_to_english_title_with_warning(combat_state):
    mutated = copy.deepcopy(combat_state)
    del mutated["player"]["character_id"]
    mutated["player"]["character"] = "The Ironclad"
    warnings: list[str] = []
    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION, warnings=warnings)
    assert snapshot["character"] == "IRONCLAD"
    assert len(warnings) == 1
    assert "character_id" in warnings[0]
    assert "The Ironclad" in warnings[0]


@pytest.mark.parametrize(
    "title,character_id",
    [
        ("The Ironclad", "IRONCLAD"),
        ("The Silent", "SILENT"),
        ("The Defect", "DEFECT"),
        ("The Necrobinder", "NECROBINDER"),
        ("The Regent", "REGENT"),
    ],
)
def test_every_english_title_maps_without_character_id(combat_state, title, character_id):
    mutated = copy.deepcopy(combat_state)
    del mutated["player"]["character_id"]
    mutated["player"]["character"] = title
    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert snapshot["character"] == character_id


def test_unrecognized_character_value_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["character_id"] = "NOT_A_REAL_CHARACTER"
    with pytest.raises(SnapshotError, match="NOT_A_REAL_CHARACTER") as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.character_id"


def test_unrecognized_character_title_without_character_id_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    del mutated["player"]["character_id"]
    mutated["player"]["character"] = "A Mystery Adventurer"
    with pytest.raises(SnapshotError, match="A Mystery Adventurer") as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.character"


# ---------------------------------------------------------------------------
# Missing / null / wrong-type required fields raise SnapshotError with path
# ---------------------------------------------------------------------------


def test_missing_player_hp_raises_with_path(combat_state):
    mutated = copy.deepcopy(combat_state)
    del mutated["player"]["hp"]
    with pytest.raises(SnapshotError, match="player.hp") as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.hp"


def test_missing_enemy_hp_raises_with_nested_path(combat_state):
    mutated = copy.deepcopy(combat_state)
    del mutated["battle"]["enemies"][0]["hp"]
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "enemies[0].hp"


def test_missing_run_raises_with_path(combat_state):
    mutated = copy.deepcopy(combat_state)
    del mutated["run"]
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "run"


def test_null_required_field_also_raises(combat_state):
    """A field present but null is treated the same as missing."""
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["hp"] = None
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.hp"


def test_missing_alive_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    del mutated["battle"]["enemies"][0]["alive"]
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "enemies[0].alive"


def test_wrong_type_field_raises_with_path(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["hp"] = "70"  # should be an int, not a string
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.hp"


def test_malformed_hand_index_raises_with_real_path_not_none(combat_state):
    """(M3) A type-malformed field raises with a real path, never `hand[None]`."""
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["hand"][0]["index"] = "zero"
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.hand[0].index"


# ---------------------------------------------------------------------------
# *_ordered pile permutation checks (I1)
# ---------------------------------------------------------------------------


def test_short_ordered_pile_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["discard_pile_ordered"].pop()
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.discard_pile_ordered"


def test_ordered_pile_uid_mismatch_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["discard_pile_ordered"][0]["uid"] = "not-a-real-uid"
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.discard_pile_ordered"


def test_non_dict_entry_in_ordered_pile_raises_cleanly(combat_state):
    """(M3) A malformed *_ordered entry raises SnapshotError, never a bare
    AttributeError from calling .get() on something that isn't a dict."""
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["discard_pile_ordered"][0] = "not-an-object"
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.discard_pile_ordered[0]"


def test_ordered_pile_matches_duplicate_null_uids_without_collapsing(combat_state):
    """Cards without a uid must not collapse into one entry.

    Which specific null-uid ordered slot a null-uid card lands in is not
    itself determinable from the data (both entries are indistinguishable
    by uid), so this only asserts the property the fix actually guarantees:
    both distinct cards come through exactly once, never merged or dropped.
    """
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["discard_pile"] = [
        {
            "uid": None,
            "name": "Strike",
            "cost": "1",
            "star_cost": None,
            "description": "Deal 6 damage.",
            "id": "STRIKE_IRONCLAD",
            "is_upgraded": False,
            "upgrade_level": 0,
            "enchantment": None,
            "affliction": None,
            "fields": {},
        },
        {
            "uid": None,
            "name": "Defend",
            "cost": "1",
            "star_cost": None,
            "description": "Gain 5 Block.",
            "id": "DEFEND_IRONCLAD",
            "is_upgraded": False,
            "upgrade_level": 0,
            "enchantment": None,
            "affliction": None,
            "fields": {},
        },
    ]
    mutated["player"]["discard_pile_ordered"] = [
        {"id": "DEFEND_IRONCLAD", "is_upgraded": False, "uid": None},
        {"id": "STRIKE_IRONCLAD", "is_upgraded": False, "uid": None},
    ]
    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert sorted(c["id"] for c in snapshot["discard_pile"]) == [
        "DEFEND_IRONCLAD",
        "STRIKE_IRONCLAD",
    ]
    assert len(snapshot["discard_pile"]) == 2


def test_explicitly_empty_ordered_pile_raises_when_unordered_pile_is_non_empty(combat_state):
    """Carried from Task 11's re-review: an explicit [] must not be treated
    the same as an absent (None) *_ordered -- it still has to be a
    permutation of the unordered pile, so it raises on the length mismatch
    exactly like a too-short list would."""
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["discard_pile_ordered"] = []
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.discard_pile_ordered"


def test_explicitly_empty_ordered_pile_is_fine_when_unordered_pile_is_also_empty(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["exhaust_pile"] = []
    mutated["player"]["exhaust_pile_ordered"] = []
    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert snapshot["exhaust_pile"] == []


def test_absent_ordered_pile_still_falls_back_to_unordered_order(combat_state):
    """None (the key genuinely absent) is unaffected by the [] fix above."""
    mutated = copy.deepcopy(combat_state)
    del mutated["player"]["discard_pile_ordered"]
    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert [c["id"] for c in snapshot["discard_pile"]] == ["IRON_WAVE", "STRIKE_IRONCLAD"]


# ---------------------------------------------------------------------------
# Regression: the WHOLE snapshot is independent of player.draw_pile's own
# input order too, not just draw_pile_ordered (carried from Task 11's
# re-review).
# ---------------------------------------------------------------------------


def test_reversed_draw_pile_input_produces_an_identical_snapshot(combat_state):
    original = build_live_snapshot(combat_state, game_version=GAME_VERSION)

    reversed_state = copy.deepcopy(combat_state)
    reversed_state["player"]["draw_pile"] = list(reversed(reversed_state["player"]["draw_pile"]))

    assert build_live_snapshot(reversed_state, game_version=GAME_VERSION) == original


# ---------------------------------------------------------------------------
# Potion slot validation (I1)
# ---------------------------------------------------------------------------


def test_potion_slot_out_of_range_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["potions"][0]["slot"] = 5  # max_potion_slots is 3
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.potions[0].slot"


def test_duplicate_potion_slot_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["potions"].append(
        {
            "id": "BLOCK_POTION",
            "name": "Block Potion",
            "description": "Gain 12 Block.",
            "slot": 1,
            "can_use_in_combat": True,
            "target_type": "Self",
            "keywords": [],
        }
    )
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.potions[1].slot"


# ---------------------------------------------------------------------------
# "Unreadable" (mod-null) fields raise, optionally carrying the mod's own
# warning text (I1)
# ---------------------------------------------------------------------------


def test_null_card_fields_raises_with_mod_warning_text(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["hand"][0]["fields"] = None
    mutated["warnings"] = ["card.fields: InvalidOperationException: reflection failed"]
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "hand[0].fields"
    assert "InvalidOperationException" in str(exc_info.value)


def test_null_relic_fields_without_warning_still_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["relics"][0]["fields"] = None
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.relics[0].fields"


def test_null_ai_fields_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["battle"]["enemies"][0]["ai_fields"] = None
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "enemies[0].ai_fields"


def test_null_attack_damage_raises_with_warning(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["battle"]["enemies"][0]["intents"][0]["damage"] = None
    mutated["warnings"] = ["enemy.intent.damage: NullReferenceException: no local player"]
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "enemies[0].intents[0].damage"
    assert "NullReferenceException" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Pets (M5)
# ---------------------------------------------------------------------------


def test_pets_present_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["pets"] = [
        {
            "id": "OSTY",
            "name": "Otsy",
            "alive": True,
            "hp": 12,
            "max_hp": 12,
            "block": 0,
            "status": [],
        }
    ]
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "player.pets"


def test_empty_pets_list_does_not_raise(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["pets"] = []
    build_live_snapshot(mutated, game_version=GAME_VERSION)  # must not raise


# ---------------------------------------------------------------------------
# battle.history: only the 4 mod-derivable keys pass through
# ---------------------------------------------------------------------------


def test_history_drops_unlisted_keys_and_omits_unavailable_ones(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    assert "powers_played_this_turn" not in snapshot["history"]
    assert "cards_drawn_this_combat" not in snapshot["history"]
    assert "cards_generated_this_combat" not in snapshot["history"]
    assert "lightning_orbs_channeled_this_combat" not in snapshot["history"]


def test_history_missing_from_mod_becomes_empty_dict(combat_state):
    mutated = copy.deepcopy(combat_state)
    del mutated["battle"]["history"]
    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert snapshot["history"] == {}


# ---------------------------------------------------------------------------
# known_draw_top / known_draw_bottom are caller-supplied, not read from state
# ---------------------------------------------------------------------------


def test_known_draw_top_and_bottom_come_from_params_not_state(combat_state):
    known_top = [
        {
            "id": "STRIKE_IRONCLAD",
            "is_upgraded": True,
            "uid": "c5",
            "cost": "1",
            "enchantment": None,
            "affliction": None,
            "fields": {},
        }
    ]
    known_bottom = [
        {
            "id": "DEFEND_IRONCLAD",
            "is_upgraded": False,
            "uid": None,
            "cost": "1",
            "enchantment": None,
            "affliction": None,
            "fields": {},
        }
    ]
    snapshot = build_live_snapshot(
        combat_state,
        game_version=GAME_VERSION,
        known_top=known_top,
        known_bottom=known_bottom,
    )
    assert snapshot["known_draw_top"] == [
        {
            "id": "STRIKE_IRONCLAD",
            "upgraded": True,
            "uid": "c5",
            "cost": 1,
            "enchantment": None,
            "enchant_amount": None,
            "fields": {},
        }
    ]
    assert snapshot["known_draw_bottom"] == [
        {
            "id": "DEFEND_IRONCLAD",
            "upgraded": False,
            "uid": None,
            "cost": 1,
            "enchantment": None,
            "enchant_amount": None,
            "fields": {},
        }
    ]


# ---------------------------------------------------------------------------
# Intent types (C1): the full 15-member table, plus an unmapped type
# ---------------------------------------------------------------------------

# The game's MonsterMoves.Intents.IntentType (decompiled IntentType.cs),
# spelled the live snapshot's way. Written fresh, independent of
# coach.snapshot._INTENT_TYPE_NAMES.
_EXPECTED_INTENT_TYPE_NAMES = {
    "Attack": "ATTACK",
    "DeathBlow": "DEATH_BLOW",
    "Defend": "DEFEND",
    "Buff": "BUFF",
    "Summon": "SUMMON",
    "Heal": "HEAL",
    "Debuff": "DEBUFF",
    "DebuffStrong": "DEBUFF_STRONG",
    "CardDebuff": "CARD_DEBUFF",
    "StatusCard": "STATUS_CARD",
    "Sleep": "SLEEP",
    "Stun": "STUN",
    "Hidden": "HIDDEN",
    "Escape": "ESCAPE",
    "Unknown": "UNKNOWN",
}
_ATTACK_RAW_TYPES = {"Attack", "DeathBlow"}


@pytest.mark.parametrize("raw_type,expected", sorted(_EXPECTED_INTENT_TYPE_NAMES.items()))
def test_every_intent_type_maps_correctly(combat_state, raw_type, expected):
    mutated = copy.deepcopy(combat_state)
    intent = {"type": raw_type, "label": "x", "title": "x", "description": "x"}
    if raw_type in _ATTACK_RAW_TYPES:
        intent.update({"damage": 5, "base_damage": 4, "hits": 1})
    mutated["battle"]["enemies"][0]["intents"] = [intent]

    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION)
    built = snapshot["enemies"][0]["intents"][0]

    assert built["type"] == expected
    if raw_type in _ATTACK_RAW_TYPES:
        assert built == {"type": expected, "damage": 5, "base_damage": 4, "hits": 1}
    else:
        assert built == {"type": expected, "damage": None, "base_damage": None, "hits": None}


def test_unmapped_intent_type_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["battle"]["enemies"][0]["intents"] = [
        {"type": "SomeFutureIntentType", "label": "?", "title": "?", "description": "?"}
    ]
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "enemies[0].intents[0].type"


def _load_converter_intent_types() -> dict[str, str] | None:
    if not CONVERTER_IDS_PY.is_file():
        return None
    tree = ast.parse(CONVERTER_IDS_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "INTENT_TYPES"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    return None


@pytest.mark.skipif(not CONVERTER_IDS_PY.is_file(), reason="converter checkout not available")
def test_intent_type_vocabulary_matches_converter():
    """(M2) Cross-check the independently-written table above against the
    converter's own accepted vocabulary (sts2_gym/live/ids.py)."""
    converter_types = _load_converter_intent_types()
    assert converter_types is not None
    assert set(_EXPECTED_INTENT_TYPE_NAMES.values()) == set(converter_types.keys())


# ---------------------------------------------------------------------------
# Hidden and unknown fields never leak (M1), except inside the two
# documented pass-through dicts (addendum rules 4/6).
# ---------------------------------------------------------------------------

_FORBIDDEN_SENTINEL = "FORBIDDEN-SENTINEL-3f8a9c"
_PASSTHROUGH_SENTINEL = "passthrough-sentinel-9c8a3f"


def _inject_hidden_and_unknown_fields(state: dict) -> dict:
    mutated = copy.deepcopy(state)
    s = _FORBIDDEN_SENTINEL

    # Hidden fields, named ones and pool/rng/seed/future-encounter shaped ones.
    mutated["player"]["draw_pile_ordered"] = [{"id": s, "is_upgraded": False, "uid": s}]
    mutated["current_run"] = {"seed": s}
    mutated["rng"] = s
    mutated["pools"] = {"card_pool": [s], "relic_pool": [s]}
    mutated["next_encounters"] = [s]

    # Unknown keys at every nesting level.
    mutated["unknown_top_level_key"] = s
    mutated["run"]["unknown_run_key"] = s
    mutated["player"]["unknown_player_key"] = s
    mutated["battle"]["unknown_battle_key"] = s
    for card in mutated["player"]["hand"]:
        card["unknown_card_key"] = s
    for card in mutated["player"]["draw_pile"]:
        card["unknown_pile_card_key"] = s
    for relic in mutated["player"]["relics"]:
        relic["unknown_relic_key"] = s

    # move_follow_up_id on enemies AND allies.
    for enemy in mutated["battle"]["enemies"]:
        enemy["move_follow_up_id"] = s
        enemy["unknown_enemy_key"] = s
    mutated["battle"]["allies"] = [
        {
            "entity_id": "OSTY_0",
            "id": "OSTY",
            "alive": True,
            "hp": 1,
            "max_hp": 1,
            "block": 0,
            "status": [],
            "intents": [],
            "move_id": s,
            "move_follow_up_id": s,
            "is_stunned": False,
            "state_log": [],
            "slot": None,
            "ai_fields": {},
            "unknown_ally_key": s,
        }
    ]

    # The two documented pass-through dicts: a value placed INSIDE them is
    # expected to come through, because the mod filters them to primitive
    # instance fields and the converter reads only named, reviewed keys.
    mutated["battle"]["enemies"][0]["ai_fields"] = {"SomeMonsterField": _PASSTHROUGH_SENTINEL}
    mutated["player"]["relics"][0]["fields"] = {"_someRelicField": _PASSTHROUGH_SENTINEL}

    return mutated


def test_hidden_and_unknown_fields_never_leak(combat_state):
    poisoned = _inject_hidden_and_unknown_fields(combat_state)
    snapshot = build_live_snapshot(poisoned, game_version=GAME_VERSION)
    dumped = json.dumps(snapshot)

    assert _FORBIDDEN_SENTINEL not in dumped
    # Confirms the exception is real: ai_fields/relic fields really do pass
    # through, so the assertion above is not accidentally too strong to
    # ever fail.
    assert _PASSTHROUGH_SENTINEL in dumped


# ---------------------------------------------------------------------------
# Structural validation against schema section 1 (required keys and types,
# broadened to every output field per M7)
# ---------------------------------------------------------------------------

_CARD_KEYS = {"id", "upgraded", "uid", "cost", "enchantment", "enchant_amount", "fields"}
_ENEMY_KEYS = {
    "entity_id",
    "id",
    "slot",
    "hp",
    "max_hp",
    "block",
    "powers",
    "intents",
    "move_id",
    "state_log",
    "ai_fields",
}
_PLAYER_KEYS = {
    "hp",
    "max_hp",
    "block",
    "energy",
    "max_energy",
    "gold",
    "stars",
    "osty",
    "orbs",
    "powers",
    "relics",
    "potions",
    "max_potion_slots",
}
_TOP_LEVEL_KEYS = {
    "schema",
    "game_version",
    "character",
    "ascension",
    "act",
    "floor",
    "room_type",
    "round",
    "player",
    "hand",
    "draw_pile",
    "discard_pile",
    "exhaust_pile",
    "known_draw_top",
    "known_draw_bottom",
    "enemies",
    "history",
    "pending_selection",
}
_HISTORY_ACCEPTED_KEYS = {
    "cards_played_this_turn",
    "cards_played_this_combat",
    "cards_drawn_this_combat",
    "cards_generated_this_combat",
    "lightning_orbs_channeled_this_combat",
    "attacks_played_this_turn",
    "skills_played_this_turn",
}
# Schema section 1.1, as amended 2026-09-24 plus the played_card controller
# ruling. Written out fresh rather than imported from coach.snapshot, same
# independence convention as the rest of this file.
_PENDING_SELECTION_KEYS = {
    "kind",
    "pile",
    "prompt_key",
    "source_card",
    "source_potion",
    "source_relic",
    "played_card",
    "candidates",
    "min",
    "max",
    "can_skip",
    "selected_uids",
}


def _assert_card_shape(card: dict) -> None:
    assert set(card.keys()) == _CARD_KEYS
    assert isinstance(card["id"], str)
    assert isinstance(card["upgraded"], bool)
    assert card["uid"] is None or isinstance(card["uid"], str)
    assert card["cost"] is None or isinstance(card["cost"], int)
    assert card["enchantment"] is None or isinstance(card["enchantment"], str)
    assert card["enchant_amount"] is None or isinstance(card["enchant_amount"], int)
    assert isinstance(card["fields"], dict)


def test_output_matches_schema_shape(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)

    assert set(snapshot.keys()) == _TOP_LEVEL_KEYS
    assert snapshot["schema"] == "sts2-live-snapshot/1"
    assert isinstance(snapshot["game_version"], str)
    assert isinstance(snapshot["character"], str)
    assert isinstance(snapshot["ascension"], int)
    assert isinstance(snapshot["act"], int)
    assert isinstance(snapshot["floor"], int)
    assert snapshot["room_type"] in {"monster", "elite", "boss"}
    assert isinstance(snapshot["round"], int)
    assert snapshot["pending_selection"] is None

    player = snapshot["player"]
    assert set(player.keys()) == _PLAYER_KEYS
    assert isinstance(player["hp"], int)
    assert isinstance(player["max_hp"], int)
    assert isinstance(player["block"], int)
    assert isinstance(player["energy"], int)
    assert isinstance(player["max_energy"], int)
    assert isinstance(player["gold"], int)
    assert isinstance(player["stars"], int)
    assert player["osty"] is None or set(player["osty"].keys()) == {"hp", "max_hp"}
    assert isinstance(player["orbs"], dict)
    assert set(player["orbs"].keys()) == {"slots", "list"}
    assert isinstance(player["orbs"]["slots"], int)
    for orb in player["orbs"]["list"]:
        assert set(orb.keys()) == {"id", "passive", "evoke"}
        assert isinstance(orb["id"], str)
    for power in player["powers"]:
        assert set(power.keys()) == {"id", "amount"}
        assert isinstance(power["id"], str)
        assert isinstance(power["amount"], int)
    for relic in player["relics"]:
        assert set(relic.keys()) == {"id", "counter", "fields"}
        assert isinstance(relic["id"], str)
        assert relic["counter"] is None or isinstance(relic["counter"], int)
        assert isinstance(relic["fields"], dict)
    for potion in player["potions"]:
        assert set(potion.keys()) == {"slot", "id"}
        assert isinstance(potion["slot"], int)
        assert potion["id"] is None or isinstance(potion["id"], str)
    assert isinstance(player["max_potion_slots"], int)

    pile_names = (
        "hand",
        "draw_pile",
        "discard_pile",
        "exhaust_pile",
        "known_draw_top",
        "known_draw_bottom",
    )
    for pile_name in pile_names:
        assert isinstance(snapshot[pile_name], list)
        for card in snapshot[pile_name]:
            _assert_card_shape(card)

    assert isinstance(snapshot["enemies"], list)
    assert snapshot["enemies"], "a combat snapshot always has at least one enemy"
    for enemy in snapshot["enemies"]:
        assert set(enemy.keys()) == _ENEMY_KEYS
        assert isinstance(enemy["entity_id"], str)
        assert isinstance(enemy["id"], str)
        assert enemy["slot"] is None or isinstance(enemy["slot"], int)
        assert isinstance(enemy["hp"], int)
        assert isinstance(enemy["max_hp"], int)
        assert isinstance(enemy["block"], int)
        for power in enemy["powers"]:
            assert set(power.keys()) == {"id", "amount"}
            assert isinstance(power["id"], str)
            assert isinstance(power["amount"], int)
        for intent in enemy["intents"]:
            assert set(intent.keys()) == {"type", "damage", "base_damage", "hits"}
            assert intent["type"] in _EXPECTED_INTENT_TYPE_NAMES.values()
            assert intent["damage"] is None or isinstance(intent["damage"], int)
            assert intent["base_damage"] is None or isinstance(intent["base_damage"], int)
            assert intent["hits"] is None or isinstance(intent["hits"], int)
        assert enemy["move_id"] is None or isinstance(enemy["move_id"], str)
        assert enemy["state_log"] is None or isinstance(enemy["state_log"], list)
        assert isinstance(enemy["ai_fields"], dict)

    assert isinstance(snapshot["history"], dict)
    for key, value in snapshot["history"].items():
        assert key in _HISTORY_ACCEPTED_KEYS
        assert isinstance(value, int)


# ---------------------------------------------------------------------------
# Strongly recommended extra test: key sets are a subset of the converter's
# own accepted live fixtures, at every level. This catches schema drift
# before the live smoke test. Skipped if the converter checkout is absent.
# The checkout path comes from SPIRE_SIM_REPO, defaulting to the path used
# throughout this task (M2).
# ---------------------------------------------------------------------------

# The converter's sample fixtures never happen to exercise a state where a
# skill was played this turn, so this one schema-valid key (binding schema
# section 1 lists it as 1 of 7 accepted history keys; task-11-addendum.md
# rule 1 requires mapping it when the mod provides it) never appears in any
# of the 6 files. Likewise, none of the 6 files happens to carry a non-empty
# orbs list (Defect-only; every sample's orbs are {"slots": 0, "list": []}),
# so a populated orb's fixed 3-key shape (id/passive/evoke) never appears
# either, even though it is not an open-ended dict like ai_fields. Both are
# gaps in the sample data, not a disagreement about what the converter
# accepts -- reported in the task report.
_KNOWN_SAMPLE_COVERAGE_GAPS = {
    "history.skills_played_this_turn",
    "player.orbs.list.id",
    "player.orbs.list.passive",
    "player.orbs.list.evoke",
}

# ai_fields and relic fields are explicitly open-ended: the schema documents
# them as "primitive instance fields ... by field name", i.e. per-instance,
# per-monster/per-relic-class dynamic keys, never a fixed contract (unlike
# Card.fields, which is always exactly cost_this_turn/cost_for_combat). Their
# container path is still checked; their dynamically-named contents are not.
_OPAQUE_DICT_PATHS = {"enemies.ai_fields", "player.relics.fields"}


def _dotted_keys(obj, prefix: str = "") -> set[str]:
    """Every dotted key path reachable through dicts/lists in obj."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            found.add(path)
            if path in _OPAQUE_DICT_PATHS:
                continue
            found |= _dotted_keys(value, path)
    elif isinstance(obj, list):
        for item in obj:
            found |= _dotted_keys(item, prefix)
    return found


def _orbs_state(state: dict) -> dict:
    mutated = copy.deepcopy(state)
    mutated["player"]["orbs"] = [
        {
            "id": "LIGHTNING",
            "name": "Lightning",
            "description": "Passive: Deal 3 damage to a random enemy. Evoke: Deal 8 damage.",
            "passive_val": 3,
            "evoke_val": 8,
            "keywords": [],
        }
    ]
    mutated["player"]["orb_slots"] = 3
    mutated["player"]["orb_empty_slots"] = 2
    return mutated


@pytest.mark.skipif(
    not CONVERTER_LIVE_FIXTURES.is_dir(),
    reason="sts2-wt converter fixtures checkout not available",
)
def test_snapshot_keys_are_subset_of_converter_live_fixtures(combat_state):
    converter_keys: set[str] = set()
    fixture_paths = sorted(CONVERTER_LIVE_FIXTURES.glob("*.json"))
    assert fixture_paths, "expected at least one converter live fixture"
    for fixture_path in fixture_paths:
        converter_keys |= _dotted_keys(json.loads(fixture_path.read_text(encoding="utf-8")))

    known_top = [
        {
            "id": "STRIKE_IRONCLAD",
            "is_upgraded": True,
            "uid": "c5",
            "cost": "1",
            "enchantment": None,
            "affliction": None,
            "fields": {},
        }
    ]
    known_bottom = [
        {
            "id": "DEFEND_IRONCLAD",
            "is_upgraded": False,
            "uid": None,
            "cost": "1",
            "enchantment": None,
            "affliction": None,
            "fields": {},
        }
    ]

    snapshots = [
        build_live_snapshot(combat_state, game_version=GAME_VERSION),
        build_live_snapshot(_orbs_state(combat_state), game_version=GAME_VERSION),
        build_live_snapshot(
            combat_state,
            game_version=GAME_VERSION,
            known_top=known_top,
            known_bottom=known_bottom,
        ),
    ]

    snapshot_keys: set[str] = set()
    for snapshot in snapshots:
        snapshot_keys |= _dotted_keys(snapshot)
    snapshot_keys -= _KNOWN_SAMPLE_COVERAGE_GAPS

    unexpected = snapshot_keys - converter_keys
    assert not unexpected, f"keys not seen in any converter live fixture: {sorted(unexpected)}"


def test_pending_selection_keys_are_subset_of_schema_section_1_1(
    hand_select_with_selection_state, card_select_grid_state, card_select_choose_state
):
    """The converter's top-level sample fixtures (checked above) predate
    schema section 1.1's amendment: their own pending_selection is always
    null, so they cannot stand in for it here. Checked against the schema's
    own key list instead (task-11b requirement 6)."""
    selection_snapshots = [
        build_live_snapshot(
            hand_select_with_selection_state, game_version=GAME_VERSION, room_type="monster"
        ),
        build_live_snapshot(card_select_grid_state, game_version=GAME_VERSION, room_type="monster"),
        build_live_snapshot(
            card_select_choose_state, game_version=GAME_VERSION, room_type="monster"
        ),
    ]
    for snapshot in selection_snapshots:
        pending = snapshot["pending_selection"]
        assert pending is not None
        assert set(pending.keys()) <= _PENDING_SELECTION_KEYS
        for candidate in pending["candidates"]:
            assert set(candidate.keys()) <= _CARD_KEYS
        if pending["played_card"] is not None:
            assert set(pending["played_card"].keys()) <= _CARD_KEYS
