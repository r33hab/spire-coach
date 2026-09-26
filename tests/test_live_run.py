"""Tests for coach/bridge/live_run.py: the live-run and screen objects `engine.strategy`
reads (rl/run_train/DESIGN.md, 3.5 and 3.7), built from one settled mod payload at a
time. `LiveRunTracker` is the only place that needs more than one payload: the map
screen's node graph (3.5) and an event's page count / a card_select's source (3.7)."""

from __future__ import annotations

from coach.bridge.live_run import LiveRunTracker


def state(**overrides):
    base = {
        "state_type": "map",
        "run": {"act": 1, "floor": 1, "ascension": 0, "map_coord": {"col": 0, "row": 0}},
        "player": {"hp": 60, "max_hp": 70, "gold": 0, "relics": [], "deck": [], "potions": []},
    }
    base.update(overrides)
    return base


def map_state(*, act=1, boss="SOUL_FYSH_BOSS", **overrides):
    return state(
        state_type="map",
        run={"act": act, "floor": 1, "ascension": 0, "map_coord": {"col": 0, "row": 0}},
        map={
            "current_position": {"col": 0, "row": 0, "type": "Ancient"},
            "nodes": [{"col": 0, "row": 1, "type": "Monster", "children": []}],
            "boss": {"id": boss},
            "next_options": [{"index": 0, "col": 0, "row": 1, "type": "Monster"}],
        },
        **overrides,
    )


# --- the map cache (3.5) ---------------------------------------------------------------


def test_no_map_seen_yet_gives_no_live_run():
    tracker = LiveRunTracker()

    live_run, screen = tracker.build(state(state_type="shop", shop={"items": []}))

    assert live_run is None
    assert screen["state_type"] == "shop"


def test_a_map_screen_caches_itself_and_answers_its_own_request():
    tracker = LiveRunTracker()

    live_run, _ = tracker.build(map_state())

    assert live_run == {
        "run": {"act": 1, "floor": 1, "ascension": 0, "map_coord": {"col": 0, "row": 0}},
        "player": state()["player"],
        "map": {
            "act": 1,
            "current_position": {"col": 0, "row": 0, "type": "Ancient"},
            "nodes": [{"col": 0, "row": 1, "type": "Monster", "children": []}],
            "boss": {"id": "SOUL_FYSH_BOSS"},
            "next_options": [{"index": 0, "col": 0, "row": 1, "type": "Monster"}],
        },
    }


def test_a_non_map_screen_that_already_carries_a_map_needs_no_earlier_map_screen():
    # The captured mod build sends `map` on Neow's event, before the player has ever
    # seen the map screen itself; nothing here should require having seen one first.
    tracker = LiveRunTracker()

    live_run, screen = tracker.build(
        state(
            state_type="event",
            event={"event_id": "NEOW", "body": None, "options": []},
            map={
                "current_position": {"col": 3, "row": 0, "type": "Ancient"},
                "nodes": [],
                "boss": {"id": "SOUL_FYSH_BOSS"},
                "next_options": [],
            },
        )
    )

    assert screen["state_type"] == "event"
    assert live_run["map"]["boss"] == {"id": "SOUL_FYSH_BOSS"}


def test_a_later_screen_in_the_same_act_reuses_the_cached_map():
    tracker = LiveRunTracker()
    tracker.build(map_state(boss="SOUL_FYSH_BOSS"))

    live_run, screen = tracker.build(
        state(state_type="card_reward", card_reward={"cards": [], "can_skip": True})
    )

    assert screen["state_type"] == "card_reward"
    assert live_run["map"]["boss"] == {"id": "SOUL_FYSH_BOSS"}


def test_a_new_act_drops_the_previous_acts_cached_map():
    tracker = LiveRunTracker()
    tracker.build(map_state(act=1))

    live_run, _ = tracker.build(
        state(
            state_type="rewards",
            run={"act": 2, "floor": 18, "ascension": 0},
            rewards={"items": [], "can_proceed": True},
        )
    )

    assert live_run is None  # act 2 has not shown its own map screen yet


# --- event_step (3.7) --------------------------------------------------------------------


def event_state(*, event_id="NEOW", body=None, options=()):
    return state(state_type="event", event={"event_id": event_id, "body": body, "options": list(options)})


def test_an_events_first_page_is_step_zero():
    tracker = LiveRunTracker()

    _, screen = tracker.build(event_state())

    assert screen["event_step"] == 0


def test_a_changed_page_increments_the_step_the_same_event_stays_at_zero_otherwise():
    tracker = LiveRunTracker()
    tracker.build(event_state(body=None))
    _, unchanged = tracker.build(event_state(body=None))
    _, changed = tracker.build(event_state(body="the room floods"))

    assert unchanged["event_step"] == 0
    assert changed["event_step"] == 1


def test_a_different_event_id_restarts_the_step_count():
    tracker = LiveRunTracker()
    tracker.build(event_state(event_id="NEOW", body="a"))
    tracker.build(event_state(event_id="NEOW", body="b"))

    _, screen = tracker.build(event_state(event_id="BONFIRE", body=None))

    assert screen["event_step"] == 0


def test_event_step_is_null_off_an_event_screen():
    tracker = LiveRunTracker()

    _, screen = tracker.build(state(state_type="shop", shop={"items": []}))

    assert screen["event_step"] is None


# --- source (3.7) ------------------------------------------------------------------------


def test_a_card_select_right_after_an_event_names_that_event_as_its_source():
    tracker = LiveRunTracker()
    tracker.build(event_state(event_id="WOOD_CARVINGS"))

    _, screen = tracker.build(
        state(state_type="card_select", card_select={"screen_type": "transform", "cards": []})
    )

    assert screen["source"] == {"event": "WOOD_CARVINGS"}


def test_a_card_select_after_a_newly_acquired_relic_names_that_relic_as_its_source():
    tracker = LiveRunTracker()
    tracker.build(state())  # relics: [] to start

    _, screen = tracker.build(
        state(
            state_type="card_select",
            card_select={"screen_type": "transform", "cards": []},
            player={**state()["player"], "relics": [{"id": "CLAWS"}]},
        )
    )

    assert screen["source"] == {"relic": "CLAWS"}


def test_a_card_select_with_no_new_relic_or_event_has_no_source():
    tracker = LiveRunTracker()
    tracker.build(state())

    _, screen = tracker.build(state(state_type="card_select", card_select={"screen_type": "upgrade", "cards": []}))

    assert screen["source"] is None


def test_source_is_null_off_a_card_select_screen():
    tracker = LiveRunTracker()

    _, screen = tracker.build(state(state_type="shop", shop={"items": []}))

    assert screen["source"] is None


# --- the screen carries the whole payload through (3.7) -----------------------------------


def test_the_screen_carries_unrelated_payload_fields_through_unchanged():
    tracker = LiveRunTracker()

    _, screen = tracker.build(state(state_type="shop", shop={"items": []}, mod_version="0.4.0"))

    assert screen["shop"] == {"items": []}
    assert screen["mod_version"] == "0.4.0"
