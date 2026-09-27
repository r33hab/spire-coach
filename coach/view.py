"""What the live coach's panel shows, and how it is built.

`compose` turns the session's situation (the connection, the position on screen and
its analysis, the version check, the mode, the last grade) into the one `CoachState`
the panel renders. It is a pure function of what it is given: the session in
coach/live.py owns all the state and calls it on every change. The panel's move and
grade dicts and its one-sentence texts are built here too.
"""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from coach.bridge.versions import VersionStatus
from coach.engine import Analysis, CheckResult, Move, MoveStat, StrategyResult
from coach.grader import Grade

_ALTERNATIVES_MAX = 3
_DEATH_VALUE_HP = -100.0  # spec 7.2 (the search's DEATH_VALUE): what a death is worth

_MOVE_FIELDS = tuple(f.name for f in dataclasses.fields(Move))
_MOVE_IDENTITY_FIELDS = tuple(name for name in _MOVE_FIELDS if name != "label")


@dataclass(frozen=True)
class CoachState:
    """What the panel shows. Published whole on every change."""

    status: str  # waiting_for_game | idle | thinking | advice | advice_off | engine_restarting
    status_text: str  # one readable sentence
    recommendation: dict | None
    last_grade: dict | None
    mode: str
    versions: VersionStatus
    run: dict
    # Non-combat advice (the strategy champion), or None when there is none to show:
    # the panel's counterpart to `recommendation`, built by `_strategy_dict`.
    strategy: dict | None = None

    def to_dict(self) -> dict:
        return _json_safe(dataclasses.asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), allow_nan=False)


class ShownPosition(Protocol):
    """What the panel reads off the position on screen (coach/live.py's `_Position`)."""

    status: str
    error: str | None
    analysis: Analysis | None
    live: dict | None
    live_warnings: list[str]
    revealed: bool


class StrategyShown(Protocol):
    """What the panel reads off the non-combat screen on screen (coach/live.py's
    `_StrategyPosition`). `status`: "pending" (asked, no answer yet), "blocked"
    (advice is off), "unreadable" (no map cached yet this act: 3.5 needs one), "done"
    (`result` is the engine's answer, or None: no champion is exported), "failed" (the
    engine call raised). `act` is the run's act the screen settled on."""

    status: str
    state_type: str | None
    result: StrategyResult | None
    error: str | None
    act: int | None


def compose(
    position: ShownPosition | None,
    *,
    connected: bool,
    bridge_problem: str | None,
    internal_error: str | None,
    versions: VersionStatus,
    override_versions: bool,
    mode: str,
    last_grade: dict | None,
    run: dict,
    fidelity_warnings: list[str],
    engine_restarting: bool,
    z: float,
    potion_value_hp: float,
    strategy: StrategyShown | None = None,
) -> CoachState:
    """The panel's state for this situation. `position` is the combat decision on
    screen, or None when there is none; `engine_restarting` says the engine client is
    restarting the service (its on_restart fired and the request has not come back).
    `strategy` is the non-combat screen's strategy result, independent of `position`:
    it is shown alongside whatever `position` says, combat and non-combat decisions
    never being on screen at once."""

    def shown(status: str, text: str, recommendation: dict | None = None) -> CoachState:
        return CoachState(
            status=status,
            status_text=text,
            recommendation=recommendation,
            last_grade=last_grade,
            mode=mode,
            versions=versions,
            run=dict(run),
            strategy=_strategy_dict(strategy),
        )

    if internal_error is not None:
        return shown(
            "advice_off",
            sentence(f"Advice is off: the coach hit an internal error ({internal_error})"),
        )
    if not connected:
        if bridge_problem:
            return shown("waiting_for_game", sentence(f"Waiting for the game: {bridge_problem}"))
        return shown("waiting_for_game", "Waiting for the game.")
    if position is None:
        return shown("idle", "Connected: waiting for a combat decision.")
    if not (versions.ok or override_versions):
        problems = sentence("Advice is off: " + "; ".join(versions.problems))
        return shown(
            "advice_off",
            f"{problems} Turn on the version override in the settings to see advice anyway.",
        )
    if position.status == "unreadable":
        return shown(
            "advice_off",
            sentence(
                f"Advice is off for this position: the coach could not read it ({position.error})"
            ),
        )
    if position.status == "failed":
        return shown("advice_off", sentence(f"Advice is off for this position: {position.error}"))
    if position.status != "done":
        if engine_restarting:  # the position on screen waits on the restarted service
            return shown("engine_restarting", "The engine service stopped; restarting it.")
        return shown("thinking", "Analyzing this position…")
    recommendation = _recommendation(
        position,
        hidden=mode == "quiz" and not position.revealed,
        extra_warnings=[
            *_override_warnings(versions),
            *fidelity_warnings,
        ],
        z=z,
        potion_value_hp=potion_value_hp,
    )
    return shown("advice", _advice_text(position, recommendation), recommendation)


_NO_ACT1_STRATEGY_NOTE = "No between-fight advice outside Act 1 yet"


def _strategy_dict(strategy: StrategyShown | None) -> dict | None:
    """The panel's non-combat counterpart to `_recommendation`: the champion's ranked
    options, best first, each with its value's gap to the best, and the boss win
    chance the model gives its top pick (spec 5.2: None below the champion's 5%
    self-play floor, or with no `model.json` at all)."""
    if strategy is None:
        return None
    base = {"screen": strategy.state_type, "options": None, "boss_win_chance": None, "model": None}
    if strategy.status == "failed":
        # The strategy champion is trained on Act 1 only, so every Act 2-3 screen fails
        # (UnknownIdError: map.boss.id, among others) -- not a bug the panel should show
        # as one. An Act 1 failure is still a real one and keeps its own message.
        if strategy.act is not None and strategy.act != 1:
            return {**base, "status": "failed", "error": sentence(_NO_ACT1_STRATEGY_NOTE)}
        return {
            **base,
            "status": "failed",
            "error": sentence(strategy.error or "the engine could not score this screen"),
        }
    if strategy.result is None:
        return {**base, "status": strategy.status, "error": None}
    options = _strategy_options(strategy.result.options)
    return {
        **base,
        "status": "done",
        "error": None,
        "options": options,
        "boss_win_chance": options[0]["p_boss"] if options else None,
        "model": _json_safe(strategy.result.model),
    }


def _strategy_options(options: list[dict]) -> list[dict]:
    """The engine's best-first options (already sorted, unscored last), each with a
    human label and its value's gap to the best (0 for the best; None when either
    value is unscored)."""
    values = [_finite(option.get("value")) for option in options]
    best_value = next((value for value in values if value is not None), None)
    result = []
    for option, value in zip(options, values, strict=True):
        gap = best_value - value if best_value is not None and value is not None else None
        descriptor = option.get("option") or {}
        result.append(
            {
                # The service names cards, relics and potions; our own label is the fallback.
                "label": option.get("label") or _strategy_option_label(descriptor),
                "value": value,
                "p_boss": _finite(option.get("p_boss")),
                "gap": gap,
                "scorable": bool(descriptor.get("scorable", True)),
            }
        )
    return result


def _strategy_option_label(option: dict) -> str:
    """A short, human label for an `Option` (DESIGN.md 3.4): its kind, plus whatever
    identifies it (a card, a relic, a potion, a map node, an event choice)."""
    kind = option.get("kind") or "option"
    card = option.get("card")
    if isinstance(card, dict) and card.get("id"):
        return f"{kind}: {card['id']}"
    cards = option.get("cards")
    if isinstance(cards, list) and cards:
        ids = ", ".join(card["id"] for card in cards if isinstance(card, dict) and card.get("id"))
        if ids:
            return f"{kind}: {ids}"
    for name in ("relic", "potion"):
        value = option.get(name)
        if value is not None:
            return f"{kind}: {name} {value}"
    for name in ("node", "event"):
        value = option.get(name)
        if value is not None:
            return f"{kind}: {name} {list(value)}"
    return kind


def _override_warnings(versions: VersionStatus) -> list[str]:
    # Advice with a version problem is only ever shown with the override on.
    if versions.ok:
        return []
    return [sentence("The version check is overridden: " + "; ".join(versions.problems))]


def _advice_text(position: ShownPosition, recommendation: dict) -> str:
    if recommendation["best"] is None:
        return "Quiz: make your move, then see its grade and the best move."
    if not position.analysis.reliable:
        return "Advice ready, but unreliable: see the warnings."
    return "Advice ready."


def _recommendation(
    position: ShownPosition,
    *,
    hidden: bool,
    extra_warnings: list[str],
    z: float,
    potion_value_hp: float,
) -> dict:
    analysis = position.analysis
    best_stat = next((s for s in analysis.moves if _same_move(s, analysis.best)), None)
    return {
        "best": None if hidden else move_dict(analysis.best),
        "line": None if hidden else [move_dict(move) for move in analysis.line],
        "alternatives": None if hidden else _alternatives(analysis, z),
        "expected_hp_loss": _expected_hp_loss(position.live, best_stat, potion_value_hp),
        "p_death": _finite(best_stat.p_death) if best_stat is not None else None,
        "reliable": analysis.reliable,
        "warnings": _warnings(position, extra_warnings),
    }


def _alternatives(analysis: Analysis, z: float) -> list[dict]:
    """The cheapest other moves, each with its paired cost against the best one."""
    others = [stat for stat in analysis.moves if not _same_move(stat, analysis.best)]
    others.sort(
        key=lambda stat: (
            _finite(stat.paired_diff_vs_best) is None,
            _finite(stat.paired_diff_vs_best) or 0.0,
        )
    )
    alternatives = []
    for stat in others[:_ALTERNATIVES_MAX]:
        diff = _finite(stat.paired_diff_vs_best)
        stderr = _finite(stat.paired_diff_stderr)
        alternatives.append(
            {
                "move": move_dict(stat),
                "cost_mean_hp": diff,
                "cost_lower_hp": diff - z * stderr
                if diff is not None and stderr is not None
                else None,
                "p_death_delta": _finite(stat.paired_death_diff_vs_best),
            }
        )
    return alternatives


def _expected_hp_loss(
    live: dict, best_stat: MoveStat | None, potion_value_hp: float
) -> float | None:
    """HP the best line is expected to lose if the fight is won.

    The engine's value is end HP plus `potion_value_hp` per potion still held, or -100
    for a death (spec 7.2). Taking the deaths out of the mean leaves the value of a
    win; the loss is what is held now (HP plus potions, valued the same way) minus
    that. A potion the line drinks counts as its value in HP.
    """
    if best_stat is None:
        return None
    value, p_death = _finite(best_stat.value_mean), _finite(best_stat.p_death)
    if value is None or p_death is None or p_death >= 1.0:
        return None
    player = live["player"]
    held = sum(1 for slot in player["potions"] if slot["id"] is not None)
    now = player["hp"] + potion_value_hp * held
    won = (value - p_death * _DEATH_VALUE_HP) / (1.0 - p_death)
    return max(0.0, now - won)


def _warnings(position: ShownPosition, extra_warnings: list[str]) -> list[str]:
    """The snapshot builder's warnings with the engine's, then the coach's own."""
    analysis = position.analysis
    texts = [*position.live_warnings, *analysis.warnings]
    if analysis.select_mapping_failures:
        texts.append(
            f"{analysis.select_mapping_failures} selection option(s) could not be matched "
            "to the cards on screen and are missing from the advice."
        )
    texts += extra_warnings
    return list(dict.fromkeys(one_line(text) for text in texts))


# --- the panel's move and grade dicts ----------------------------------------------------


def plain_move(move: Move) -> Move:
    """Only a Move's own fields (a MoveStat is a Move plus its statistics)."""
    return Move(**{name: getattr(move, name) for name in _MOVE_FIELDS})


def move_dict(move: Move) -> dict:
    return dataclasses.asdict(plain_move(move))


def _same_move(a: Move, b: Move) -> bool:
    return all(getattr(a, name) == getattr(b, name) for name in _MOVE_IDENTITY_FIELDS)


def grade_dict(grade: Grade) -> dict:
    graded = dataclasses.asdict(grade)
    graded["best"] = move_dict(grade.best)
    graded["played"] = move_dict(grade.played)
    return _json_safe(graded)


def ungraded_dict(played: Move, analysis: Analysis | None) -> dict:
    """The Grade shape for a move with no analysis to grade it on, or one the analysis
    does not have."""
    return {
        "label": "Ungraded",
        "cost_lower_hp": None,
        "cost_mean_hp": None,
        "death_risk_delta_lower": None,
        "best": move_dict(analysis.best) if analysis is not None else None,
        "played": move_dict(played),
        "unreliable": analysis is not None and not analysis.reliable,
    }


# --- one-sentence texts --------------------------------------------------------------------


def mismatch_sentence(move: Move, result: CheckResult, gap: Path) -> str:
    findings = [
        f"{m.get('field')} predicted {m.get('predicted')}, the game showed {m.get('actual')}"
        for m in result.deterministic_mismatches
    ] + [
        f"{m.get('field')} came out {m.get('actual')}, which the simulator cannot produce"
        for m in result.implausible
    ]
    more = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
    return one_line(
        f"Fidelity mismatch after {move.label}: {findings[0]}{more}; gap report {gap.name}."
    )


def one_line(value: object) -> str:
    """An exception or message as one line of text: no newline, never a stack trace."""
    text = " ".join(str(value).split())
    if not text and isinstance(value, BaseException):
        return type(value).__name__
    return text


def sentence(text: str) -> str:
    text = one_line(text)
    return text if text.endswith((".", "!", "?", "…")) else f"{text}."


# --- numbers the panel can always serialize ----------------------------------------------


def _finite(value: object) -> float | None:
    """A finite number, else None: a statistic with no data behind it (NaN, inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _json_safe(value):
    """`value` with every non-finite float replaced by None, so its JSON is strict."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value
