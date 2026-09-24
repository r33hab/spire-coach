import copy
import json
from pathlib import Path

import pytest

from coach.bridge.decisions import (
    Decision,
    DecisionKind,
    Stabilizer,
    classify,
    fingerprint,
    is_settled,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mod"

# Verbatim from the task brief, kept independent of the module's own constants
# so a typo in the implementation's lists can't hide from these tests.
COMBAT_STATE_TYPES = ["monster", "elite", "boss"]
NON_COMBAT_STATE_TYPES = [
    "rewards",
    "card_reward",
    "map",
    "shop",
    "rest_site",
    "event",
    "treasure",
    "card_select",
    "relic_select",
]
UNRECOGNIZED_STATE_TYPES = ["menu", "unknown", "overlay", "game_over"]


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def monster_state():
    return load("state_monster_player_turn.json")


@pytest.fixture
def hand_select_state():
    return load("state_hand_select.json")


@pytest.fixture
def non_combat_state():
    return load("state_non_combat.json")


@pytest.fixture
def menu_state():
    return load("state_menu.json")


@pytest.fixture
def unrecognized_state():
    return load("state_unrecognized.json")


# --- is_settled --------------------------------------------------------


def test_settled_combat_state_is_settled(monster_state):
    assert is_settled(monster_state) is True


def test_combat_state_is_not_settled_during_enemy_turn(monster_state):
    state = copy.deepcopy(monster_state)
    state["battle"]["turn"] = "enemy"
    assert is_settled(state) is False


def test_combat_state_is_not_settled_mid_resolution(monster_state):
    state = copy.deepcopy(monster_state)
    state["is_resolving"] = True
    state["resolving_reasons"] = ["card_still_being_played"]
    assert is_settled(state) is False


def test_combat_state_is_not_settled_outside_the_play_phase(monster_state):
    state = copy.deepcopy(monster_state)
    state["battle"]["is_play_phase"] = False
    assert is_settled(state) is False


def test_hand_select_is_settled_when_not_resolving(hand_select_state):
    # battle.is_play_phase is false in the fixture: hand_select must not care.
    assert hand_select_state["battle"]["is_play_phase"] is False
    assert is_settled(hand_select_state) is True


def test_hand_select_is_not_settled_while_resolving(hand_select_state):
    state = copy.deepcopy(hand_select_state)
    state["is_resolving"] = True
    assert is_settled(state) is False


def test_non_combat_state_is_settled_when_not_resolving(non_combat_state):
    assert is_settled(non_combat_state) is True


def test_non_combat_state_is_not_settled_while_resolving(non_combat_state):
    state = copy.deepcopy(non_combat_state)
    state["is_resolving"] = True
    assert is_settled(state) is False


def test_missing_is_resolving_counts_as_settled_for_a_menu_payload(menu_state):
    assert "is_resolving" not in menu_state
    assert is_settled(menu_state) is True


# --- classify ------------------------------------------------------------


@pytest.mark.parametrize("state_type", COMBAT_STATE_TYPES)
def test_combat_states_classify_as_combat(monster_state, state_type):
    state = copy.deepcopy(monster_state)
    state["state_type"] = state_type
    assert classify(state) == Decision(
        kind=DecisionKind.COMBAT, state_type=state_type, room_type=state_type
    )


def test_hand_select_classifies_as_hand_select(hand_select_state):
    assert classify(hand_select_state) == Decision(
        kind=DecisionKind.HAND_SELECT, state_type="hand_select", room_type=None
    )


@pytest.mark.parametrize("state_type", NON_COMBAT_STATE_TYPES)
def test_non_combat_states_classify_as_non_combat(non_combat_state, state_type):
    state = copy.deepcopy(non_combat_state)
    state["state_type"] = state_type
    assert classify(state) == Decision(
        kind=DecisionKind.NON_COMBAT, state_type=state_type, room_type=None
    )


@pytest.mark.parametrize("state_type", UNRECOGNIZED_STATE_TYPES)
def test_unrecognized_state_types_classify_as_none(unrecognized_state, state_type):
    state = copy.deepcopy(unrecognized_state)
    state["state_type"] = state_type
    expected = Decision(kind=DecisionKind.NONE, state_type=state_type, room_type=None)
    assert classify(state) == expected


def test_menu_payload_missing_is_resolving_classifies_as_none(menu_state):
    expected = Decision(kind=DecisionKind.NONE, state_type="menu", room_type=None)
    assert classify(menu_state) == expected


def test_unsettled_combat_state_classifies_as_none_not_combat(monster_state):
    state = copy.deepcopy(monster_state)
    state["battle"]["turn"] = "enemy"
    decision = classify(state)
    assert decision.kind is DecisionKind.NONE
    # state_type is preserved even though the decision itself is NONE.
    assert decision.state_type == "monster"
    assert decision.room_type is None


def test_unsettled_hand_select_classifies_as_none(hand_select_state):
    state = copy.deepcopy(hand_select_state)
    state["is_resolving"] = True
    assert classify(state).kind is DecisionKind.NONE


def test_unsettled_non_combat_state_classifies_as_none(non_combat_state):
    state = copy.deepcopy(non_combat_state)
    state["is_resolving"] = True
    assert classify(state).kind is DecisionKind.NONE


# --- fingerprint -----------------------------------------------------------


def test_fingerprint_is_a_sha1_hex_digest(monster_state):
    value = fingerprint(monster_state)
    assert len(value) == 40
    assert all(c in "0123456789abcdef" for c in value)


def test_fingerprint_ignores_warnings_mod_commit_action_log_seq_and_time_scale(monster_state):
    other = copy.deepcopy(monster_state)
    other["warnings"] = ["something odd"]
    other["mod_commit"] = "deadbeef"
    other["action_log_seq"] = 999
    other["time_scale"] = 4.0
    assert fingerprint(monster_state) == fingerprint(other)


def test_fingerprint_ignores_fields_outside_the_tracked_set(monster_state):
    other = copy.deepcopy(monster_state)
    other["rooms_entered"] = 999
    other["schema_version"] = 3
    other["mod_version"] = "0.9.9"
    assert fingerprint(monster_state) == fingerprint(other)


def test_fingerprint_changes_when_state_type_changes(monster_state):
    other = copy.deepcopy(monster_state)
    other["state_type"] = "elite"
    assert fingerprint(monster_state) != fingerprint(other)


def test_fingerprint_changes_when_battle_changes(monster_state):
    other = copy.deepcopy(monster_state)
    other["battle"]["enemies"][0]["hp"] -= 5
    assert fingerprint(monster_state) != fingerprint(other)


def test_fingerprint_changes_when_player_changes(monster_state):
    other = copy.deepcopy(monster_state)
    other["player"]["hp"] -= 1
    assert fingerprint(monster_state) != fingerprint(other)


def test_fingerprint_changes_when_run_changes(monster_state):
    other = copy.deepcopy(monster_state)
    other["run"]["floor"] += 1
    assert fingerprint(monster_state) != fingerprint(other)


# --- Stabilizer --------------------------------------------------------


def test_stabilizer_withholds_the_first_settled_poll(monster_state):
    stabilizer = Stabilizer()
    assert stabilizer.feed(monster_state) is None


def test_stabilizer_emits_on_the_second_identical_settled_poll(monster_state):
    stabilizer = Stabilizer()
    stabilizer.feed(copy.deepcopy(monster_state))
    result = stabilizer.feed(copy.deepcopy(monster_state))
    assert result == monster_state


def test_stabilizer_returns_the_latest_fed_state_on_emission(monster_state):
    stabilizer = Stabilizer()
    first = copy.deepcopy(monster_state)
    first["action_log_seq"] = 1
    second = copy.deepcopy(monster_state)
    second["action_log_seq"] = 2

    assert stabilizer.feed(first) is None
    result = stabilizer.feed(second)

    assert result == second
    assert result["action_log_seq"] == 2


def test_stabilizer_does_not_re_emit_an_unchanged_state(monster_state):
    stabilizer = Stabilizer()
    stabilizer.feed(copy.deepcopy(monster_state))
    stabilizer.feed(copy.deepcopy(monster_state))
    assert stabilizer.feed(copy.deepcopy(monster_state)) is None
    assert stabilizer.feed(copy.deepcopy(monster_state)) is None


def test_stabilizer_resets_the_streak_when_the_state_changes(monster_state):
    stabilizer = Stabilizer()
    stabilizer.feed(copy.deepcopy(monster_state))
    stabilizer.feed(copy.deepcopy(monster_state))  # streak of 2, emitted

    changed = copy.deepcopy(monster_state)
    changed["player"]["hp"] -= 1

    assert stabilizer.feed(changed) is None  # new fingerprint, streak restarts at 1
    assert stabilizer.feed(copy.deepcopy(changed)) == changed  # streak of 2 again


def test_stabilizer_never_emits_while_unsettled(monster_state):
    stabilizer = Stabilizer()
    resolving = copy.deepcopy(monster_state)
    resolving["is_resolving"] = True
    assert stabilizer.feed(resolving) is None
    assert stabilizer.feed(copy.deepcopy(resolving)) is None


def test_stabilizer_resets_on_an_unsettled_poll_between_settled_ones(monster_state):
    stabilizer = Stabilizer()
    stabilizer.feed(copy.deepcopy(monster_state))
    stabilizer.feed(copy.deepcopy(monster_state))  # emitted once

    resolving = copy.deepcopy(monster_state)
    resolving["is_resolving"] = True
    assert stabilizer.feed(resolving) is None

    # Settling back to the very same position needs two fresh consecutive polls.
    assert stabilizer.feed(copy.deepcopy(monster_state)) is None
    assert stabilizer.feed(copy.deepcopy(monster_state)) == monster_state


def test_stabilizer_re_emits_a_position_once_it_re_settles_after_a_different_one(monster_state):
    # A, A, B, A, A: A stabilizes and emits on the 2nd feed; B is only seen once
    # (no emission); A re-stabilizes and must emit again on the 5th feed, not
    # get silently withheld because it was already emitted earlier.
    stabilizer = Stabilizer()
    a = monster_state
    b = copy.deepcopy(monster_state)
    b["player"]["hp"] -= 1

    assert stabilizer.feed(copy.deepcopy(a)) is None
    assert stabilizer.feed(copy.deepcopy(a)) == a
    assert stabilizer.feed(copy.deepcopy(b)) is None
    assert stabilizer.feed(copy.deepcopy(a)) is None
    assert stabilizer.feed(copy.deepcopy(a)) == a


def test_stabilizer_emits_only_once_across_a_long_unbroken_streak(monster_state):
    stabilizer = Stabilizer()
    results = [stabilizer.feed(copy.deepcopy(monster_state)) for _ in range(4)]
    assert results == [None, monster_state, None, None]
