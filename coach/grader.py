"""Grade the player's moves from the engine's paired search statistics.

Turns an `Analysis`/`MoveStat` (`coach/engine.py`) into a `Grade`, per
docs/superpowers/specs/2026-09-23-spire-coach-design.md Section 7.5 and the
coach-live plan's Global Constraints: the grade is read off the lower bound
of a 90% one-sided confidence interval on the paired value gap to the best
move, `diff - 1.645 * stderr`, never the raw mean, so sampling noise never
produces a harsh grade. A move identical to the best move is always graded
Best, even before it has its own paired statistics (the search does not pair
the best move against itself).

`identify_move` maps one action-log entry (STS2MCP docs/raw-full.md,
"Action log") to the `MoveStat` it corresponds to, using the live hand's
card uids (`play_card`) or the potion slot (`use_potion`) plus the entry's
target -- never the search's own best-target guess, so a card played on the
wrong enemy is graded against the child for that target, not the best
one. `grade_selection` does the same job for an in-combat selection
(hand/grid/choose-a-card), which the mod does not log as an action: the
chosen candidate is found by diffing which one left the hand or a pile
between two live snapshots.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from coach.engine import Analysis, Move, MoveStat

# --- thresholds --------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    """The Global Constraints' grading thresholds, gathered so config can override them."""

    z: float = 1.645  # the one-sided 90% confidence factor
    best_max_hp: float = 0.5
    good_max_hp: float = 2.0
    inaccuracy_max_hp: float = 5.0
    mistake_max_hp: float = 10.0
    death_risk_blunder_min: float = 0.10
    min_paired_n: int = 20


DEFAULT_THRESHOLDS = Thresholds()


# --- Grade ---------------------------------------------------------------


@dataclass(frozen=True)
class Grade:
    label: str
    cost_lower_hp: float | None
    cost_mean_hp: float | None
    death_risk_delta_lower: float | None
    best: Move
    played: Move
    unreliable: bool


def _lower_bound(diff: float, stderr: float, z: float) -> float:
    return diff - z * stderr


# A MoveStat is a Move subclass (coach/engine.py), but the dataclass-generated
# __eq__ on both requires the two objects share the exact same class, so
# `analysis.best == stat` is always False even for identical moves. Identity
# is compared on these fields instead; `label` is descriptive text, not part
# of a move's identity.
_MOVE_IDENTITY_FIELDS = (
    "kind",
    "card_index",
    "card_uid",
    "card_id",
    "potion_slot",
    "potion_id",
    "target",
    "select_index",
)


def _same_move(a: Move, b: Move) -> bool:
    return all(getattr(a, field) == getattr(b, field) for field in _MOVE_IDENTITY_FIELDS)


def grade(stat: MoveStat, analysis: Analysis, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> Grade:
    """Grade one played move against the analysis it was made from."""
    unreliable = not analysis.reliable

    if _same_move(stat, analysis.best):
        # The best move by definition costs nothing relative to itself, even
        # when it carries no paired statistics of its own.
        return Grade(
            label="Best",
            cost_lower_hp=0.0,
            cost_mean_hp=0.0,
            death_risk_delta_lower=0.0,
            best=analysis.best,
            played=stat,
            unreliable=unreliable,
        )

    if (
        stat.paired_diff_vs_best is None
        or stat.paired_diff_stderr is None
        or stat.paired_n < thresholds.min_paired_n
    ):
        return Grade(
            label="Ungraded",
            cost_lower_hp=None,
            cost_mean_hp=None,
            death_risk_delta_lower=None,
            best=analysis.best,
            played=stat,
            unreliable=unreliable,
        )

    cost_mean_hp = stat.paired_diff_vs_best
    cost_lower_hp = _lower_bound(stat.paired_diff_vs_best, stat.paired_diff_stderr, thresholds.z)

    death_risk_delta_lower = None
    if stat.paired_death_diff_vs_best is not None and stat.paired_death_diff_stderr is not None:
        death_risk_delta_lower = _lower_bound(
            stat.paired_death_diff_vs_best, stat.paired_death_diff_stderr, thresholds.z
        )

    is_death_risk_blunder = (
        death_risk_delta_lower is not None
        and death_risk_delta_lower >= thresholds.death_risk_blunder_min
    )
    if is_death_risk_blunder:
        label = "Blunder"
    elif cost_lower_hp <= thresholds.best_max_hp:
        label = "Best"
    elif cost_lower_hp < thresholds.good_max_hp:
        label = "Good"
    elif cost_lower_hp < thresholds.inaccuracy_max_hp:
        label = "Inaccuracy"
    elif cost_lower_hp < thresholds.mistake_max_hp:
        label = "Mistake"
    else:
        label = "Blunder"

    return Grade(
        label=label,
        cost_lower_hp=cost_lower_hp,
        cost_mean_hp=cost_mean_hp,
        death_risk_delta_lower=death_risk_delta_lower,
        best=analysis.best,
        played=stat,
        unreliable=unreliable,
    )


# --- identify_move ---------------------------------------------------------

# The mod's action-log kinds that can ever be graded (ruling R4): an
# out-of-combat entry (round is null) or any other kind is never graded.
_GRADABLE_ACTION_KINDS = frozenset({"play_card", "use_potion", "end_turn"})


def _hand_index_for_uid(pre_live: dict, card_uid: str) -> int | None:
    for index, card in enumerate(pre_live.get("hand") or []):
        if card.get("uid") == card_uid:
            return index
    return None


def identify_move(action_entry: dict, pre_live: dict, analysis: Analysis) -> MoveStat | None:
    """Map one mod action-log entry to the MoveStat it corresponds to, or None."""
    kind = action_entry.get("kind")
    if kind not in _GRADABLE_ACTION_KINDS or action_entry.get("round") is None:
        return None

    if kind == "play_card":
        card_uid = action_entry.get("card_uid")
        if card_uid is None:
            return None
        hand_index = _hand_index_for_uid(pre_live, card_uid)
        if hand_index is None:
            return None
        target = action_entry.get("target")
        return next(
            (
                m
                for m in analysis.moves
                if m.kind == "card" and hand_index in m.hand_indices and m.target == target
            ),
            None,
        )

    if kind == "use_potion":
        slot = action_entry.get("slot")
        target = action_entry.get("target")
        return next(
            (
                m
                for m in analysis.moves
                if m.kind == "potion" and m.potion_slot == slot and m.target == target
            ),
            None,
        )

    return next((m for m in analysis.moves if m.kind == "end_turn"), None)  # kind == "end_turn"


# --- grade_selection --------------------------------------------------------

_PILE_KEYS = ("hand", "draw_pile", "discard_pile", "exhaust_pile")


def _pile_uid_counts(live: dict) -> Counter:
    counts: Counter = Counter()
    for key in _PILE_KEYS:
        for card in live.get(key) or []:
            uid = card.get("uid")
            if uid is not None:
                counts[uid] += 1
    return counts


def _departed_candidate_index(
    pre_live: dict, post_live: dict, candidates: list[dict]
) -> int | None:
    """The one candidate whose uid count in the hand/piles dropped between the two snapshots."""
    pre_counts = _pile_uid_counts(pre_live)
    post_counts = _pile_uid_counts(post_live)
    departed = [
        index
        for index, card in enumerate(candidates)
        if card.get("uid") is not None
        and pre_counts[card["uid"]] > post_counts.get(card["uid"], 0)
    ]
    return departed[0] if len(departed) == 1 else None


def grade_selection(pre_live: dict, post_live: dict, analysis: Analysis) -> Grade | None:
    """Grade an in-combat selection, identified by diffing pre/post hand and piles."""
    pending = pre_live.get("pending_selection")
    if not pending:
        return None

    candidates = pending.get("candidates") or []
    index = _departed_candidate_index(pre_live, post_live, candidates)
    if index is None:
        return None

    stat = next(
        (m for m in analysis.moves if m.kind == "select" and m.select_index == index), None
    )
    if stat is None:
        return None
    return grade(stat, analysis)
