"""Decide when the coach should look at a position.

The coach must only think during settled, player-facing moments: full combat
turns, an in-combat card selection, or a non-combat screen waiting on the
player. Anything mid-animation or mid-resolution is never a decision point.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

COMBAT_STATE_TYPES = frozenset({"monster", "elite", "boss"})
NON_COMBAT_STATE_TYPES = frozenset(
    {
        "rewards",
        "card_reward",
        "map",
        "shop",
        "rest_site",
        "event",
        "treasure",
        "card_select",
        "relic_select",
    }
)

# The subset of the mod payload that defines a position, for fingerprinting.
# Everything else (warnings, mod_commit, action_log_seq, time_scale, ...) can
# change without the position itself changing, so it is left out on purpose.
_FINGERPRINT_KEYS = ("state_type", "battle", "player", "run")


class DecisionKind(Enum):
    COMBAT = "combat"
    HAND_SELECT = "hand_select"
    NON_COMBAT = "non_combat"
    NONE = "none"


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    state_type: str | None
    room_type: str | None


def is_settled(state: dict) -> bool:
    """True once the state is done animating/resolving and it's the player's turn to act.

    `is_resolving` is the base signal. Combat additionally needs the play
    phase to be active and it to be the player's turn (not, say, mid-enemy
    turn). Menu, no-run and FTUE payloads never carry `is_resolving` at all;
    they are settled by definition since only combat and hand_select ever
    need the flag.
    """
    state_type = state.get("state_type")
    is_resolving = state.get("is_resolving")

    if is_resolving is None:
        return state_type not in COMBAT_STATE_TYPES and state_type != "hand_select"

    if is_resolving:
        return False

    if state_type in COMBAT_STATE_TYPES:
        battle = state.get("battle") or {}
        return battle.get("is_play_phase") is True and battle.get("turn") == "player"

    return True


def classify(state: dict) -> Decision:
    """What kind of decision, if any, this state represents."""
    state_type = state.get("state_type")

    if not is_settled(state):
        return Decision(kind=DecisionKind.NONE, state_type=state_type, room_type=None)

    if state_type in COMBAT_STATE_TYPES:
        return Decision(kind=DecisionKind.COMBAT, state_type=state_type, room_type=state_type)

    if state_type == "hand_select":
        return Decision(kind=DecisionKind.HAND_SELECT, state_type=state_type, room_type=None)

    if state_type in NON_COMBAT_STATE_TYPES:
        return Decision(kind=DecisionKind.NON_COMBAT, state_type=state_type, room_type=None)

    return Decision(kind=DecisionKind.NONE, state_type=state_type, room_type=None)


def fingerprint(state: dict) -> str:
    """A stable id for a position: sha1 of canonical JSON over the fields that define it."""
    projected = {key: state.get(key) for key in _FINGERPRINT_KEYS}
    canonical = json.dumps(projected, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()  # noqa: S324 (fingerprint, not security)


class Stabilizer:
    """Buffers polled states until a settled position repeats.

    `feed` returns the state once its settled fingerprint has been seen on 2
    consecutive polls, and only once per unbroken streak: further polls with
    that same fingerprint return None, so a consumer stores each distinct
    settled position exactly once. Any unsettled poll, or a change in the
    fingerprint, restarts the streak from zero.
    """

    def __init__(self) -> None:
        self._streak_fingerprint: str | None = None
        self._streak_count = 0
        self._emitted_current_streak = False

    def feed(self, state: dict) -> dict | None:
        current = fingerprint(state) if is_settled(state) else None

        if current != self._streak_fingerprint:
            self._streak_fingerprint = current
            self._streak_count = 0
            self._emitted_current_streak = False

        if current is None:
            return None

        self._streak_count += 1

        if self._streak_count >= 2 and not self._emitted_current_streak:
            self._emitted_current_streak = True
            return state

        return None
