"""Build the training design's live-run and screen objects for the engine's
`strategy` method (`rl/run_train/DESIGN.md`, 3.5 and 3.7), from one settled,
non-combat mod payload.

`run`, `player` and `map` already match 3.5's schema: the mod's own objects are passed
through rather than converted. The captured mod build sends a `map` object on every
screen, not only the dedicated map screen, but 3.5 only promises it "while the player
is looking at the map itself"; `LiveRunTracker` uses whichever screen's own `map` is
freshest, and falls back to the last one cached in the current act when a screen
carries none, so a coach reading an older mod still has one from the last map screen
seen. `screen` (3.7) is the payload's
own `state_type` and its matching sub-object, plus `event_step` and `source`, which need
a little history of their own: an event's page count within its current visit, and
what opened a `card_select` screen (an event, or a newly acquired relic).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LiveRunTracker:
    """Per-session cache for one running coach: the last `map` object seen in the
    current act (whichever screen carried it), and enough of the last few screens to
    fill in `event_step` and `source`. Not thread-safe; the live loop only ever
    touches it from its own thread.
    """

    _map: dict[str, Any] | None = field(default=None, repr=False)
    _map_act: int | None = field(default=None, repr=False)
    _event_id: str | None = field(default=None, repr=False)
    _event_signature: tuple | None = field(default=None, repr=False)
    _event_step: int | None = field(default=None, repr=False)
    _prev_relic_ids: frozenset[str] | None = field(default=None, repr=False)
    _prev_state_type: str | None = field(default=None, repr=False)

    def build(self, state: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """The `(live_run, screen)` pair for one settled non-combat payload.

        `live_run` is None when there is no map cached for this act yet (the coach has
        not seen a `map` screen since the act began): 3.5 requires `map`, and nothing
        buildable from one payload can stand in for it, so the caller has nothing to
        ask the strategy champion about this screen.
        """
        run = dict(state.get("run") or {})
        act = run.get("act")
        state_type = state.get("state_type")

        if isinstance(state.get("map"), dict):
            self._map = dict(state["map"])
            self._map_act = act

        screen = self._screen(state, state_type)
        self._prev_relic_ids = _relic_ids(state)
        self._prev_state_type = state_type

        if self._map is None or self._map_act != act:
            return None, screen
        live_run = {
            "run": run,
            "player": state.get("player") or {},
            "map": {"act": self._map_act, **self._map},
        }
        return live_run, screen

    def _screen(self, state: dict[str, Any], state_type: str | None) -> dict[str, Any]:
        """The whole payload (mod schema 4), plus the two fields it cannot supply on
        its own. `decision_from_live` reads `state_type` and the one sub-object it
        names; the rest (`run`, `player`, `is_resolving`, ...) is along for the ride
        and ignored, the same way 3.5's extra `player`/`run` fields are."""
        screen = dict(state)
        # `source` first: it reads the event this call is about to leave behind
        # (`_prev_state_type`, `_event_id`), which `_event_step_for` resets below.
        screen["source"] = self._source_for(state, state_type)
        screen["event_step"] = self._event_step_for(state, state_type)
        return screen

    def _event_step_for(self, state: dict[str, Any], state_type: str | None) -> int | None:
        """3.7: the pages already answered at this event, from its first page (step 0).
        A new page is a change of `body`, the option titles or their descriptions
        while `event_id` stays the same; a different event restarts the count."""
        if state_type != "event":
            self._event_id = self._event_signature = self._event_step = None
            return None
        event = state.get("event") or {}
        event_id = event.get("event_id")
        signature = (
            event.get("body"),
            tuple(
                (option.get("title"), option.get("description"))
                for option in event.get("options") or []
            ),
        )
        if event_id != self._event_id:
            self._event_id, self._event_step, self._event_signature = event_id, 0, signature
        elif signature != self._event_signature:
            self._event_step = (self._event_step or 0) + 1
            self._event_signature = signature
        return self._event_step

    def _source_for(self, state: dict[str, Any], state_type: str | None) -> dict[str, str] | None:
        """3.7: what opened a `card_select` screen. Only the two sources the table
        scores (an event's own transform/relic pick, or a relic just acquired) are
        told apart; anything else is None, same as a screen the tracker cannot place."""
        if state_type != "card_select":
            return None
        if self._prev_state_type == "event" and self._event_id:
            return {"event": self._event_id}
        if self._prev_relic_ids is not None:
            gained = _relic_ids(state) - self._prev_relic_ids
            if len(gained) == 1:
                return {"relic": next(iter(gained))}
        return None


def _relic_ids(state: dict[str, Any]) -> frozenset[str]:
    relics = (state.get("player") or {}).get("relics") or []
    return frozenset(
        relic["id"] for relic in relics if isinstance(relic, dict) and relic.get("id")
    )
