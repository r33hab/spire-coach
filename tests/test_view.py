"""Tests for what the panel shows (coach/view.py). The session-level behavior is covered
through CoachSession in tests/test_live.py; these pin the pure pieces directly."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from coach.bridge.versions import VersionStatus
from coach.engine import Analysis, Move, MoveStat, StrategyResult
from coach.view import CoachState, compose, one_line, sentence

OK = VersionStatus(ok=True, problems=[])


def stat(**overrides) -> MoveStat:
    fields = dict(
        kind="end_turn",
        card_index=None,
        card_uid=None,
        card_id=None,
        potion_slot=None,
        potion_id=None,
        target=None,
        select_index=None,
        label="End turn",
        visits=100,
        value_mean=50.0,
        p_death=0.0,
        paired_diff_vs_best=0.0,
        paired_diff_stderr=0.0,
        paired_n=50,
        paired_death_diff_vs_best=0.0,
        paired_death_diff_stderr=0.0,
        hand_indices=[],
        potion_slots=(),
        select_indices=(),
    )
    fields.update(overrides)
    return MoveStat(**fields)


def position_with(best: MoveStat, *, hp=60, potions=(None, None, None)) -> SimpleNamespace:
    move = Move(**{name: getattr(best, name) for name in Move.__dataclass_fields__})
    analysis = Analysis(
        reliable=True,
        warnings=[],
        best=move,
        line=[move],
        line_truncated_at_draw=False,
        moves=[best],
        sims=100,
        elapsed_s=0.1,
        budget_exhausted=False,
        select_mapping_failures=0,
    )
    live = {
        "player": {
            "hp": hp,
            "potions": [{"slot": i, "id": pid} for i, pid in enumerate(potions)],
        }
    }
    return SimpleNamespace(
        status="done", error=None, analysis=analysis, live=live, live_warnings=[], revealed=False
    )


def shown(position, **overrides) -> CoachState:
    situation = dict(
        connected=True,
        bridge_problem=None,
        internal_error=None,
        versions=OK,
        override_versions=False,
        mode="show",
        last_grade=None,
        run={"act": 1, "floor": 2},
        fidelity_warnings=[],
        engine_restarting=False,
        z=1.645,
        potion_value_hp=10.0,
    )
    situation.update(overrides)
    return compose(position, **situation)


def test_to_json_is_strict_json_with_every_non_finite_number_as_null():
    state = CoachState(
        status="advice",
        status_text="Advice ready.",
        recommendation={"p_death": math.nan, "costs": [math.inf, 1.5]},
        last_grade=None,
        mode="show",
        versions=OK,
        run={"act": 1, "floor": 2},
    )

    data = json.loads(state.to_json())

    assert data["recommendation"] == {"p_death": None, "costs": [None, 1.5]}
    assert data["versions"] == {"ok": True, "problems": []}


def test_an_internal_error_outranks_everything_and_waiting_outranks_idle():
    position = position_with(stat())
    assert shown(position, internal_error="KeyError: 'x'").status == "advice_off"
    assert shown(position, connected=False).status == "waiting_for_game"
    assert shown(None).status == "idle"
    mismatch = VersionStatus(ok=False, problems=["schema_version 5 does not match expected 4"])
    assert shown(position, versions=mismatch).status == "advice_off"
    assert shown(position, versions=mismatch, override_versions=True).status == "advice"


@pytest.mark.parametrize(
    "value, p_death, hp, potions, expected",
    [
        (50.0, 0.0, 60, (None, None, None), 10.0),  # a sure win at 50 HP from 60
        (60.0, 0.0, 50, ("FIRE_POTION", None, None), 0.0),  # 50 HP + a kept potion: no loss
        (40.0, 0.5, 60, (None, None, None), 0.0),  # the win's value is (40+50)/0.5 = 180 HP
        (50.0, 1.0, 60, (None, None, None), None),  # always dies: no win to measure
        (math.nan, 0.0, 60, (None, None, None), None),  # no data behind the value
    ],
)
def test_expected_hp_loss_takes_deaths_and_held_potions_out_of_the_engines_value(
    value, p_death, hp, potions, expected
):
    position = position_with(stat(value_mean=value, p_death=p_death), hp=hp, potions=potions)

    loss = shown(position).recommendation["expected_hp_loss"]

    assert loss == (pytest.approx(expected) if expected is not None else None)


def test_engine_restarting_shows_only_while_the_position_on_screen_waits():
    waiting = position_with(stat())
    waiting.status = "pending"
    assert shown(waiting, engine_restarting=True).status == "engine_restarting"
    assert shown(waiting).status == "thinking"
    assert shown(position_with(stat()), engine_restarting=True).status == "advice"


def test_texts_are_one_line_sentences():
    assert one_line(ValueError("bad\n  field")) == "bad field"
    assert one_line(ValueError()) == "ValueError"
    assert sentence("could not read it") == "could not read it."
    assert sentence("is the game running?") == "is the game running?"


# --- strategy: non-combat advice (coach-strategy) -------------------------------------


def strategy_with(options, *, status="done", state_type="map", error=None, model=None):
    result = None if options is None else StrategyResult(options=options, model=model or {})
    return SimpleNamespace(status=status, state_type=state_type, result=result, error=error)


def test_strategy_options_carry_their_gap_to_the_best_and_the_top_picks_boss_chance():
    strategy = strategy_with(
        [
            {"option": {"kind": "shop.card", "card": {"id": "STRIKE"}}, "value": 20.0, "p_boss": 0.4},
            {"option": {"kind": "shop.leave"}, "value": 18.5, "p_boss": 0.3},
            {"option": {"kind": "shop.relic", "relic": 7}, "value": None, "p_boss": None},
        ],
        model={"generation": 3},
    )

    data = shown(None, strategy=strategy).strategy

    assert data["screen"] == "map"
    assert data["status"] == "done"
    assert [o["label"] for o in data["options"]] == [
        "shop.card: STRIKE",
        "shop.leave",
        "shop.relic: relic 7",
    ]
    assert [o["gap"] for o in data["options"]] == [0, pytest.approx(1.5), None]
    assert data["boss_win_chance"] == pytest.approx(0.4)
    assert data["model"] == {"generation": 3}


def test_a_strategy_call_with_no_champion_exported_shows_no_options():
    strategy = strategy_with(None, status="done")

    assert shown(None, strategy=strategy).strategy == {
        "screen": "map",
        "status": "done",
        "error": None,
        "options": None,
        "boss_win_chance": None,
        "model": None,
    }


def test_a_failed_strategy_call_is_shown_as_a_sentence():
    strategy = strategy_with(None, status="failed", error=ValueError("boom"))

    data = shown(None, strategy=strategy).strategy

    assert data["status"] == "failed"
    assert data["error"] == "boom."


def test_no_strategy_leaves_the_panels_field_null():
    assert shown(None).strategy is None
    assert shown(position_with(stat())).strategy is None
