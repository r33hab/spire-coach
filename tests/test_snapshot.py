"""Tests for the live snapshot builder (mod JSON -> sts2-live-snapshot/1).

Some expectations (state shapes, key names) are written out fresh from
docs/superpowers/specs/2026-09-24-snapshot-schemas.md section 1 rather than
imported from coach.snapshot's own constants, so a typo in the
implementation can't hide from these tests.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coach.snapshot import SnapshotError, build_live_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "mod"
CONVERTER_LIVE_FIXTURES = Path(
    "/Users/you/Documents/git/sts2-wt/convert/tests/python/fixtures/live"
)

GAME_VERSION = "v0.107.1"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def combat_state() -> dict:
    return load("combat_full.json")


@pytest.fixture
def hand_select_state() -> dict:
    return load("combat_hand_select.json")


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
            "powers": [{"id": "STRENGTH", "amount": 2}],
            "relics": [
                {"id": "BURNING_BLOOD", "counter": None, "fields": {}},
                {
                    "id": "KUNAI",
                    "counter": 2,
                    "fields": {"_isActivating": False, "_attacksPlayedThisTurn": 2},
                },
                {"id": "PEN_NIB", "counter": 9, "fields": {"AttacksPlayed": 19}},
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
                "powers": [{"id": "STRENGTH", "amount": 3}],
                "intents": [{"type": "ATTACK", "damage": 11, "base_damage": 8, "hits": 1}],
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
                "powers": [{"id": "REATTACH", "amount": 1}],
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
    with_order = build_live_snapshot(combat_state, game_version=GAME_VERSION)

    scrambled = copy.deepcopy(combat_state)
    # Reverse draw_pile_ordered so it disagrees with the sorted multiset in a
    # different way than the fixture's own (already-disagreeing) order.
    scrambled["player"]["draw_pile_ordered"] = list(
        reversed(scrambled["player"]["draw_pile_ordered"])
    )
    reordered = build_live_snapshot(scrambled, game_version=GAME_VERSION)

    removed = copy.deepcopy(combat_state)
    del removed["player"]["draw_pile_ordered"]
    without_order = build_live_snapshot(removed, game_version=GAME_VERSION)

    assert with_order["draw_pile"] == reordered["draw_pile"] == without_order["draw_pile"]
    # Sanity: the fixture's own draw_pile_ordered really does disagree with
    # the sorted multiset, so this test is exercising something real.
    ordered_ids = [c["id"] for c in combat_state["player"]["draw_pile_ordered"]]
    sorted_ids = [c["id"] for c in with_order["draw_pile"]]
    assert ordered_ids != sorted_ids

    # And the sorted multiset is genuinely sorted by (id, upgraded).
    keys = [(c["id"], c["upgraded"]) for c in with_order["draw_pile"]]
    assert keys == sorted(keys)


def test_snapshot_never_reads_draw_pile_ordered_key(combat_state):
    """Belt and braces: deleting the *_ordered draw pile key changes nothing,
    and a present-but-bogus one (non-card garbage) is still never touched."""
    poisoned = copy.deepcopy(combat_state)
    poisoned["player"]["draw_pile_ordered"] = "not-even-a-list"  # would blow up if read
    snapshot = build_live_snapshot(poisoned, game_version=GAME_VERSION)
    assert len(snapshot["draw_pile"]) == 5


# ---------------------------------------------------------------------------
# hand_select -> pending_selection
# ---------------------------------------------------------------------------


def test_hand_select_becomes_pending_selection(hand_select_state):
    snapshot = build_live_snapshot(
        hand_select_state, game_version=GAME_VERSION, room_type="monster"
    )

    assert snapshot["room_type"] == "monster"
    assert snapshot["pending_selection"] == {
        "kind": "SIMPLE_SELECT",
        "source_card": None,
        "candidates": [
            {
                "id": "DEFEND_IRONCLAD",
                "upgraded": False,
                "uid": None,
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
            {
                "id": "STRIKE_IRONCLAD",
                "upgraded": True,
                "uid": None,
                "cost": 1,
                "enchantment": None,
                "enchant_amount": None,
                "fields": {},
            },
        ],
        "min": 1,
        "max": 1,
        "selected_uids": [],
    }


def test_hand_select_without_room_type_override_raises(hand_select_state):
    # state_type is "hand_select", which is not a combat room_type on its
    # own; the capture layer must remember and pass the real room_type.
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(hand_select_state, game_version=GAME_VERSION)
    assert exc_info.value.path == "room_type"


def test_non_combat_state_type_without_room_type_raises(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["state_type"] = "rewards"
    with pytest.raises(SnapshotError) as exc_info:
        build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert exc_info.value.path == "room_type"


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
# X-cost card
# ---------------------------------------------------------------------------


def test_x_cost_card_has_null_cost(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    whirlwind = next(c for c in snapshot["hand"] if c["id"] == "WHIRLWIND")
    assert whirlwind["cost"] is None


def test_unplayable_negative_cost_card_has_null_cost(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    bane = next(c for c in snapshot["hand"] if c["id"] == "ASCENDERS_BANE")
    assert bane["cost"] is None


# ---------------------------------------------------------------------------
# Category prefix stripping
# ---------------------------------------------------------------------------


def test_character_category_prefix_is_stripped(combat_state):
    mutated = copy.deepcopy(combat_state)
    mutated["player"]["character"] = "CHARACTER.IRONCLAD"
    snapshot = build_live_snapshot(mutated, game_version=GAME_VERSION)
    assert snapshot["character"] == "IRONCLAD"


def test_character_without_prefix_is_unchanged(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    assert snapshot["character"] == "IRONCLAD"


# ---------------------------------------------------------------------------
# Missing required fields raise SnapshotError with a JSON path
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


# ---------------------------------------------------------------------------
# battle.history: only the 4 mod-derivable keys pass through
# ---------------------------------------------------------------------------


def test_history_drops_unlisted_keys_and_omits_unavailable_ones(combat_state):
    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    # powers_played_this_turn is in the mod payload but not one of the 4
    # keys this adapter is allowed to map (task-11-addendum.md #1).
    assert "powers_played_this_turn" not in snapshot["history"]
    # Keys the mod never reports (cards_drawn_this_combat, etc.) are
    # omitted entirely rather than defaulted to 0.
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
# Structural validation against schema section 1 (required keys and types)
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

    player = snapshot["player"]
    assert set(player.keys()) == _PLAYER_KEYS
    assert isinstance(player["hp"], int)
    assert isinstance(player["orbs"], dict)
    assert set(player["orbs"].keys()) == {"slots", "list"}
    for power in player["powers"]:
        assert set(power.keys()) == {"id", "amount"}
    for relic in player["relics"]:
        assert set(relic.keys()) == {"id", "counter", "fields"}
    for potion in player["potions"]:
        assert set(potion.keys()) == {"slot", "id"}

    pile_names = (
        "hand",
        "draw_pile",
        "discard_pile",
        "exhaust_pile",
        "known_draw_top",
        "known_draw_bottom",
    )
    for pile_name in pile_names:
        for card in snapshot[pile_name]:
            _assert_card_shape(card)

    for enemy in snapshot["enemies"]:
        assert set(enemy.keys()) == _ENEMY_KEYS
        assert isinstance(enemy["entity_id"], str)
        assert isinstance(enemy["id"], str)
        for power in enemy["powers"]:
            assert set(power.keys()) == {"id", "amount"}
        for intent in enemy["intents"]:
            assert set(intent.keys()) == {"type", "damage", "base_damage", "hits"}

    assert isinstance(snapshot["history"], dict)
    assert snapshot["pending_selection"] is None or isinstance(snapshot["pending_selection"], dict)


def test_pending_selection_matches_schema_shape(hand_select_state):
    snapshot = build_live_snapshot(
        hand_select_state, game_version=GAME_VERSION, room_type="monster"
    )
    pending = snapshot["pending_selection"]
    expected_keys = {"kind", "source_card", "candidates", "min", "max", "selected_uids"}
    assert set(pending.keys()) == expected_keys
    assert isinstance(pending["kind"], str)
    assert pending["source_card"] is None or isinstance(pending["source_card"], str)
    assert isinstance(pending["min"], int)
    assert isinstance(pending["max"], int)
    assert isinstance(pending["selected_uids"], list)
    for card in pending["candidates"]:
        _assert_card_shape(card)


# ---------------------------------------------------------------------------
# Strongly recommended extra test: key sets are a subset of the converter's
# own accepted live fixtures, at every level. This catches schema drift
# before the live smoke test. Skipped if the converter checkout is absent.
# ---------------------------------------------------------------------------

# The converter's sample fixtures never happen to exercise a state where a
# skill was played this turn, so this one schema-valid key (binding schema
# section 1 lists it as 1 of 7 accepted history keys; task-11-addendum.md
# rule 1 requires mapping it when the mod provides it) never appears in any
# of the 6 files. It is a gap in the sample data, not a disagreement about
# what the converter accepts -- reported in the task report.
_KNOWN_SAMPLE_COVERAGE_GAPS = {"history.skills_played_this_turn"}

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

    snapshot = build_live_snapshot(combat_state, game_version=GAME_VERSION)
    snapshot_keys = _dotted_keys(snapshot) - _KNOWN_SAMPLE_COVERAGE_GAPS

    unexpected = snapshot_keys - converter_keys
    assert not unexpected, f"keys not seen in any converter live fixture: {sorted(unexpected)}"
