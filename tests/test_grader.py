import dataclasses

import pytest

from coach.engine import Analysis, Move, MoveStat
from coach.grader import (
    DEFAULT_THRESHOLDS,
    Grade,
    Thresholds,
    _departed_candidate_index,
    grade,
    grade_selection,
    identify_move,
)

# A fixed "someone else's move" used as analysis.best whenever a test wants to
# exercise the paired-statistics math rather than the same-move-as-best
# shortcut. Its identity fields never match any stat built by make_stat().
OTHER_BEST = Move(
    kind="end_turn",
    card_index=None,
    card_uid=None,
    card_id=None,
    potion_slot=None,
    potion_id=None,
    target=None,
    select_index=None,
    label="End turn",
)


def make_stat(**overrides) -> MoveStat:
    base = dict(
        kind="card",
        card_index=0,
        card_uid="c0",
        card_id="STRIKE_IRONCLAD",
        potion_slot=None,
        potion_id=None,
        target=None,
        select_index=None,
        label="Strike",
        visits=500,
        value_mean=50.0,
        p_death=0.0,
        paired_diff_vs_best=0.0,
        paired_diff_stderr=0.0,
        paired_n=50,
        paired_death_diff_vs_best=0.0,
        paired_death_diff_stderr=0.0,
        hand_indices=[0],
    )
    base.update(overrides)
    return MoveStat(**base)


def make_analysis(moves, *, best=None, reliable=True) -> Analysis:
    best_move = best if best is not None else moves[0]
    return Analysis(
        reliable=reliable,
        warnings=[],
        best=best_move,
        line=[best_move],
        line_truncated_at_draw=False,
        moves=list(moves),
        sims=1000,
        elapsed_s=0.5,
        budget_exhausted=False,
        select_mapping_failures=0,
    )


def make_card(uid, *, card_id="STRIKE_IRONCLAD"):
    return {
        "id": card_id,
        "upgraded": False,
        "uid": uid,
        "cost": 1,
        "enchantment": None,
        "enchant_amount": None,
        "fields": {},
    }


def make_hand(*uids, card_id="STRIKE_IRONCLAD"):
    return {"hand": [make_card(uid, card_id=card_id) for uid in uids]}


# --- grade: lower-bound bands, at their boundaries ---------------------------


@pytest.mark.parametrize(
    "diff, expected_label",
    [
        (0.5, "Best"),  # <= 0.5 -> Best
        (0.6, "Good"),
        (1.9, "Good"),
        (2.0, "Inaccuracy"),  # not < 2 -> falls through to Inaccuracy
        (4.9, "Inaccuracy"),
        (5.0, "Mistake"),  # not < 5 -> falls through to Mistake
        (9.9, "Mistake"),
        (10.0, "Blunder"),  # not < 10 -> falls through to Blunder
        (25.0, "Blunder"),
    ],
)
def test_grade_bands_at_their_lower_bound_boundaries(diff, expected_label):
    # stderr=0 so the lower bound equals diff exactly, with no death risk.
    stat = make_stat(paired_diff_vs_best=diff, paired_diff_stderr=0.0)
    analysis = make_analysis([stat], best=OTHER_BEST)

    result = grade(stat, analysis)

    assert result.label == expected_label
    assert result.cost_lower_hp == pytest.approx(diff)
    assert result.cost_mean_hp == pytest.approx(diff)


def test_grade_lower_bound_subtracts_1_645_stderr_from_the_mean_diff():
    stat = make_stat(paired_diff_vs_best=3.0, paired_diff_stderr=2.0)
    analysis = make_analysis([stat], best=OTHER_BEST)

    result = grade(stat, analysis)

    assert result.cost_mean_hp == pytest.approx(3.0)
    assert result.cost_lower_hp == pytest.approx(3.0 - 1.645 * 2.0)


# --- grade: the death-risk Blunder rule --------------------------------------


def test_death_risk_lower_bound_at_threshold_forces_blunder_despite_a_tiny_hp_cost():
    stat = make_stat(
        paired_diff_vs_best=0.1,
        paired_diff_stderr=0.0,  # HP-only would grade "Best"
        paired_death_diff_vs_best=0.10,
        paired_death_diff_stderr=0.0,  # lower bound exactly the 0.10 threshold
    )
    analysis = make_analysis([stat], best=OTHER_BEST)

    result = grade(stat, analysis)

    assert result.label == "Blunder"
    assert result.death_risk_delta_lower == pytest.approx(0.10)


def test_death_risk_lower_bound_just_under_threshold_does_not_force_blunder():
    stat = make_stat(
        paired_diff_vs_best=0.1,
        paired_diff_stderr=0.0,
        paired_death_diff_vs_best=0.099,
        paired_death_diff_stderr=0.0,
    )
    analysis = make_analysis([stat], best=OTHER_BEST)

    result = grade(stat, analysis)

    assert result.label == "Best"
    assert result.death_risk_delta_lower == pytest.approx(0.099)


def test_missing_death_risk_stat_leaves_death_risk_delta_lower_none_but_still_grades():
    stat = make_stat(
        paired_diff_vs_best=0.1,
        paired_diff_stderr=0.0,
        paired_death_diff_vs_best=None,
        paired_death_diff_stderr=None,
    )
    analysis = make_analysis([stat], best=OTHER_BEST)

    result = grade(stat, analysis)

    assert result.label == "Best"
    assert result.death_risk_delta_lower is None


# --- grade: paired_n and missing stats -> Ungraded --------------------------


def test_paired_n_below_20_is_ungraded():
    stat = make_stat(paired_diff_vs_best=0.0, paired_diff_stderr=0.0, paired_n=19)
    analysis = make_analysis([stat], best=OTHER_BEST)

    result = grade(stat, analysis)

    assert result.label == "Ungraded"
    assert result.cost_lower_hp is None
    assert result.cost_mean_hp is None
    assert result.death_risk_delta_lower is None


def test_paired_n_of_20_is_graded_not_ungraded():
    stat = make_stat(paired_diff_vs_best=0.0, paired_diff_stderr=0.0, paired_n=20)
    analysis = make_analysis([stat], best=OTHER_BEST)

    result = grade(stat, analysis)

    assert result.label == "Best"


def test_missing_paired_diff_stat_is_ungraded():
    stat = make_stat(paired_diff_vs_best=None, paired_diff_stderr=None, paired_n=50)
    analysis = make_analysis([stat], best=OTHER_BEST)

    result = grade(stat, analysis)

    assert result.label == "Ungraded"


# --- grade: the played move equal to the best move -> Best ------------------


def test_played_move_identical_to_best_is_best_even_without_paired_statistics():
    best = Move(
        kind="card",
        card_index=0,
        card_uid="c0",
        card_id="STRIKE_IRONCLAD",
        potion_slot=None,
        potion_id=None,
        target="JAW_WORM_0",
        select_index=None,
        label="Strike -> Jaw Worm",
    )
    # The search never pairs the best move against itself, so its own stat
    # entry can carry no paired statistics at all.
    stat = make_stat(
        card_uid="c0",
        target="JAW_WORM_0",
        paired_diff_vs_best=None,
        paired_diff_stderr=None,
        paired_n=0,
        paired_death_diff_vs_best=None,
        paired_death_diff_stderr=None,
    )
    analysis = make_analysis([stat], best=best)

    result = grade(stat, analysis)

    assert result.label == "Best"
    assert result.cost_lower_hp == 0.0
    assert result.cost_mean_hp == 0.0
    assert result.death_risk_delta_lower == 0.0
    assert result.best is best
    assert result.played is stat


# --- grade: unreliable analysis ----------------------------------------------


def test_unreliable_analysis_marks_the_grade_unreliable():
    stat = make_stat()
    analysis = make_analysis([stat], best=OTHER_BEST, reliable=False)

    assert grade(stat, analysis).unreliable is True


def test_reliable_analysis_marks_the_grade_not_unreliable():
    stat = make_stat()
    analysis = make_analysis([stat], best=OTHER_BEST, reliable=True)

    assert grade(stat, analysis).unreliable is False


# --- Grade / Thresholds shape -------------------------------------------------


def test_grade_is_frozen():
    stat = make_stat()
    analysis = make_analysis([stat], best=OTHER_BEST)
    result = grade(stat, analysis)
    assert isinstance(result, Grade)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.label = "changed"


def test_default_thresholds_match_the_global_constraints():
    assert DEFAULT_THRESHOLDS == Thresholds(
        z=1.645,
        best_max_hp=0.5,
        good_max_hp=2.0,
        inaccuracy_max_hp=5.0,
        mistake_max_hp=10.0,
        death_risk_blunder_min=0.10,
        min_paired_n=20,
    )


def test_grade_accepts_custom_thresholds():
    stat = make_stat(paired_diff_vs_best=1.0, paired_diff_stderr=0.0)
    analysis = make_analysis([stat], best=OTHER_BEST)
    loose = Thresholds(
        z=1.645,
        best_max_hp=5.0,
        good_max_hp=6.0,
        inaccuracy_max_hp=7.0,
        mistake_max_hp=8.0,
        death_risk_blunder_min=0.10,
        min_paired_n=20,
    )

    result = grade(stat, analysis, loose)

    assert result.label == "Best"


# --- identify_move: play_card -------------------------------------------------


def test_identify_move_maps_a_played_copy_of_a_merged_card_to_its_child():
    # Two identical Strikes in hand (indices 0 and 2) collapse into one child
    # that covers both hand indices; playing either copy must map to it.
    pre_live = make_hand("STRIKE_IRONCLAD#1", "BASH_IRONCLAD#1", "STRIKE_IRONCLAD#2")
    merged_strike = make_stat(
        card_uid=None,
        card_index=None,
        target="JAW_WORM_0",
        hand_indices=[0, 2],
        label="Strike -> Jaw Worm",
    )
    bash = make_stat(
        card_id="BASH_IRONCLAD",
        card_uid=None,
        card_index=None,
        target="JAW_WORM_0",
        hand_indices=[1],
        label="Bash -> Jaw Worm",
    )
    analysis = make_analysis([merged_strike, bash], best=OTHER_BEST)
    entry = {
        "seq": 1,
        "kind": "play_card",
        "source": "human",
        "round": 2,
        "card_id": "STRIKE_IRONCLAD",
        "card_uid": "STRIKE_IRONCLAD#2",
        "target": "JAW_WORM_0",
        "target_combat_id": 3,
    }

    result = identify_move(entry, pre_live, analysis)

    assert result is merged_strike


def test_identify_move_maps_a_targeted_card_on_the_wrong_enemy_to_that_targets_child():
    pre_live = make_hand("BASH_IRONCLAD#1", card_id="BASH_IRONCLAD")
    onto_jaw_worm = make_stat(
        card_id="BASH_IRONCLAD",
        card_uid=None,
        card_index=None,
        target="JAW_WORM_0",
        hand_indices=[0],
        label="Bash -> Jaw Worm",
    )
    onto_louse = make_stat(
        card_id="BASH_IRONCLAD",
        card_uid=None,
        card_index=None,
        target="LOUSE_0",
        hand_indices=[0],
        label="Bash -> Louse",
    )
    # The engine considers Jaw Worm the best target; the player targeted Louse.
    best = Move(
        kind="card",
        card_index=None,
        card_uid=None,
        card_id="BASH_IRONCLAD",
        potion_slot=None,
        potion_id=None,
        target="JAW_WORM_0",
        select_index=None,
        label="Bash -> Jaw Worm",
    )
    analysis = make_analysis([onto_jaw_worm, onto_louse], best=best)
    entry = {
        "seq": 1,
        "kind": "play_card",
        "source": "human",
        "round": 2,
        "card_id": "BASH_IRONCLAD",
        "card_uid": "BASH_IRONCLAD#1",
        "target": "LOUSE_0",
        "target_combat_id": 9,
    }

    result = identify_move(entry, pre_live, analysis)

    assert result is onto_louse


def test_identify_move_returns_none_when_the_card_uid_is_not_in_the_hand():
    pre_live = make_hand("STRIKE_IRONCLAD#1")
    analysis = make_analysis([make_stat(hand_indices=[0])], best=OTHER_BEST)
    entry = {
        "kind": "play_card",
        "round": 2,
        "card_id": "STRIKE_IRONCLAD",
        "card_uid": "STRIKE_IRONCLAD#999",
        "target": None,
    }

    assert identify_move(entry, pre_live, analysis) is None


def test_identify_move_returns_none_when_the_card_uid_is_null():
    # "null if it could not be found" (raw-full.md) -- nothing to map from.
    pre_live = make_hand("STRIKE_IRONCLAD#1")
    analysis = make_analysis([make_stat(hand_indices=[0])], best=OTHER_BEST)
    entry = {
        "kind": "play_card",
        "round": 2,
        "card_id": "STRIKE_IRONCLAD",
        "card_uid": None,
        "target": None,
    }

    assert identify_move(entry, pre_live, analysis) is None


# --- identify_move: use_potion -------------------------------------------------


def test_identify_move_maps_a_potion_by_slot_and_target():
    other_slot = make_stat(
        kind="potion",
        card_index=None,
        card_uid=None,
        card_id=None,
        potion_slot=0,
        potion_id="BLOCK_POTION",
        target=None,
        hand_indices=[],
        label="Block Potion",
    )
    fire_on_jaw_worm = make_stat(
        kind="potion",
        card_index=None,
        card_uid=None,
        card_id=None,
        potion_slot=1,
        potion_id="FIRE_POTION",
        target="JAW_WORM_0",
        hand_indices=[],
        label="Fire Potion -> Jaw Worm",
    )
    analysis = make_analysis([other_slot, fire_on_jaw_worm], best=OTHER_BEST)
    entry = {
        "kind": "use_potion",
        "source": "human",
        "round": 2,
        "slot": 1,
        "target": "JAW_WORM_0",
        "target_combat_id": 3,
    }

    result = identify_move(entry, make_hand(), analysis)

    assert result is fire_on_jaw_worm


# --- identify_move: end_turn ---------------------------------------------------


def test_identify_move_maps_end_turn():
    end_turn_stat = make_stat(
        kind="end_turn",
        card_index=None,
        card_uid=None,
        card_id=None,
        potion_slot=None,
        potion_id=None,
        target=None,
        hand_indices=[],
        label="End turn",
    )
    analysis = make_analysis([end_turn_stat], best=OTHER_BEST)
    entry = {"kind": "end_turn", "source": "human", "round": 2}

    result = identify_move(entry, make_hand(), analysis)

    assert result is end_turn_stat


# --- identify_move: unknown / out-of-combat entries --------------------------


def test_identify_move_returns_none_for_an_unrecognized_kind():
    analysis = make_analysis([make_stat()], best=OTHER_BEST)
    entry = {"kind": "debug_add_card", "round": 2}

    assert identify_move(entry, make_hand(), analysis) is None


def test_identify_move_returns_none_for_an_out_of_combat_entry_ruling_r4():
    # round is null outside combat (raw-full.md); R4: never graded, even
    # though use_potion is otherwise a gradable kind.
    stat = make_stat(
        kind="potion",
        card_index=None,
        card_uid=None,
        card_id=None,
        potion_slot=0,
        potion_id="FIRE_POTION",
        target=None,
        hand_indices=[],
    )
    analysis = make_analysis([stat], best=OTHER_BEST)
    entry = {"kind": "use_potion", "round": None, "slot": 0, "target": None}

    assert identify_move(entry, make_hand(), analysis) is None


# --- grade_selection -----------------------------------------------------------


def test_grade_selection_identifies_the_candidate_that_left_the_hand():
    candidate_a = make_card("c-a", card_id="STRIKE_IRONCLAD")
    candidate_b = make_card("c-b", card_id="DEFEND_IRONCLAD")
    pre_live = {
        "hand": [candidate_a, candidate_b],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
        "pending_selection": {
            "kind": "HAND",
            "candidates": [candidate_a, candidate_b],
            "min": 1,
            "max": 1,
            "can_skip": False,
        },
    }
    post_live = {
        # candidate_a (index 0) left the hand; candidate_b remains.
        "hand": [candidate_b],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
    }
    chosen = make_stat(
        kind="select",
        card_index=None,
        card_uid=None,
        card_id=None,
        target=None,
        select_index=0,
        hand_indices=[],
        label="Select Strike",
    )
    other = make_stat(
        kind="select",
        card_index=None,
        card_uid=None,
        card_id=None,
        target=None,
        select_index=1,
        hand_indices=[],
        label="Select Defend",
    )
    analysis = make_analysis([chosen, other], best=OTHER_BEST)

    result = grade_selection(pre_live, post_live, analysis)

    assert result is not None
    assert result.played is chosen


def test_grade_selection_returns_none_without_a_pending_selection():
    pre_live = {
        "hand": [],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
        "pending_selection": None,
    }
    post_live = {"hand": [], "draw_pile": [], "discard_pile": [], "exhaust_pile": []}
    analysis = make_analysis([make_stat(kind="select", select_index=0)], best=OTHER_BEST)

    assert grade_selection(pre_live, post_live, analysis) is None


def test_grade_selection_returns_none_when_no_candidate_can_be_identified():
    candidate_a = make_card("c-a")
    pre_live = {
        "hand": [candidate_a],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
        "pending_selection": {
            "kind": "HAND",
            "candidates": [candidate_a],
            "min": 1,
            "max": 1,
            "can_skip": True,
        },
    }
    # Nothing changed: the player skipped the selection. can_skip is true, but
    # this analysis has no "skip" MoveStat either, so there is nothing to grade.
    post_live = {
        "hand": [candidate_a],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
    }
    analysis = make_analysis([make_stat(kind="select", select_index=0)], best=OTHER_BEST)

    assert grade_selection(pre_live, post_live, analysis) is None


def test_departed_candidate_index_returns_none_when_more_than_one_candidate_departed():
    # Review Minor: _departed_candidate_index itself must not guess between two
    # simultaneous departures -- ambiguous, so no index is returned at all.
    candidate_a = make_card("c-a")
    candidate_b = make_card("c-b")
    pre_live = {
        "hand": [candidate_a, candidate_b],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
    }
    post_live = {"hand": [], "draw_pile": [], "discard_pile": [], "exhaust_pile": []}

    result = _departed_candidate_index(pre_live, post_live, [candidate_a, candidate_b])

    assert result is None


# --- grade_selection: choose-a-card picks and skips (controller ruling) ------


def test_grade_selection_choose_pick_matches_the_new_hand_card_by_id_and_upgraded():
    existing = make_card("hand-1", card_id="DEFEND_IRONCLAD")
    # Two candidates share an id/upgraded pair (a generated duplicate); either
    # index names the same merged child, so the search may have kept either
    # one as that child's own select_index (here, index 1, not 0).
    candidate_strike_1 = make_card("gen-a", card_id="STRIKE_IRONCLAD")
    candidate_strike_2 = make_card("gen-b", card_id="STRIKE_IRONCLAD")
    candidate_bash = make_card("gen-c", card_id="BASH_IRONCLAD")
    pre_live = {
        "hand": [existing],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
        "pending_selection": {
            "kind": "CHOOSE",
            "candidates": [candidate_strike_1, candidate_strike_2, candidate_bash],
            "min": 1,
            "max": 1,
            "can_skip": True,
        },
    }
    # The chosen card is dealt into the hand as a brand new instance (a new
    # uid) -- CHOOSE candidates are matched by (id, upgraded), never by uid.
    new_strike = make_card("new-instance-uid", card_id="STRIKE_IRONCLAD")
    post_live = {
        "hand": [existing, new_strike],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
    }
    pick_strike = make_stat(
        kind="select",
        card_index=None,
        card_uid=None,
        card_id=None,
        target=None,
        select_index=1,  # the merged child kept index 1, not 0
        hand_indices=[],
        label="Choose Strike",
    )
    pick_bash = make_stat(
        kind="select",
        card_index=None,
        card_uid=None,
        card_id=None,
        target=None,
        select_index=2,
        hand_indices=[],
        label="Choose Bash",
    )
    analysis = make_analysis([pick_strike, pick_bash], best=OTHER_BEST)

    result = grade_selection(pre_live, post_live, analysis)

    assert result is not None
    assert result.played is pick_strike


def test_grade_selection_choose_skip_maps_to_the_skip_move_when_nothing_is_taken():
    candidate_a = make_card("gen-a", card_id="STRIKE_IRONCLAD")
    candidate_b = make_card("gen-b", card_id="BASH_IRONCLAD")
    pre_live = {
        "hand": [],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
        "pending_selection": {
            "kind": "CHOOSE",
            "candidates": [candidate_a, candidate_b],
            "min": 0,
            "max": 1,
            "can_skip": True,
        },
    }
    post_live = {"hand": [], "draw_pile": [], "discard_pile": [], "exhaust_pile": []}  # unchanged
    skip_stat = make_stat(
        kind="skip",
        card_index=None,
        card_uid=None,
        card_id=None,
        target=None,
        select_index=None,
        hand_indices=[],
        label="Skip",
    )
    analysis = make_analysis([skip_stat], best=OTHER_BEST)

    result = grade_selection(pre_live, post_live, analysis)

    assert result is not None
    assert result.played is skip_stat


def test_grade_selection_choose_returns_none_when_more_than_one_new_matching_card_appears():
    candidate_a = make_card("gen-a", card_id="STRIKE_IRONCLAD")
    candidate_b = make_card("gen-b", card_id="BASH_IRONCLAD")
    pre_live = {
        "hand": [],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
        "pending_selection": {
            "kind": "CHOOSE",
            "candidates": [candidate_a, candidate_b],
            "min": 1,
            "max": 1,
            "can_skip": False,
        },
    }
    # Two new cards appear, each matching a different candidate: which one was
    # actually picked (e.g. the other arriving via an unrelated draw) cannot
    # be told apart, so this must not be graded as either.
    new_strike = make_card("new-1", card_id="STRIKE_IRONCLAD")
    new_bash = make_card("new-2", card_id="BASH_IRONCLAD")
    post_live = {
        "hand": [new_strike, new_bash],
        "draw_pile": [],
        "discard_pile": [],
        "exhaust_pile": [],
    }
    pick_strike = make_stat(
        kind="select",
        card_index=None,
        card_uid=None,
        card_id=None,
        target=None,
        select_index=0,
        hand_indices=[],
    )
    pick_bash = make_stat(
        kind="select",
        card_index=None,
        card_uid=None,
        card_id=None,
        target=None,
        select_index=1,
        hand_indices=[],
    )
    analysis = make_analysis([pick_strike, pick_bash], best=OTHER_BEST)

    assert grade_selection(pre_live, post_live, analysis) is None
