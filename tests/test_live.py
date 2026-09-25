"""Tests for the live coach loop (coach/live.py).

Everything runs against fakes. `FakeBridge` stands in for the mod: the state it
reports and its action log, which a test edits between polls. `FakeEngine` stands
in for EngineClient: canned analyses and fidelity checks, recorded calls, and an
optional gate that holds one position's analysis open while the game moves on.
The session runs engine calls on its real background thread, so a test steps it
until the result it waits for has been applied (`step_until`).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import threading
import time
from pathlib import Path

import pytest

from coach.bridge.client import BridgeAuthError, BridgeUnavailable
from coach.engine import Analysis, CheckResult, EngineError, EngineUnavailable, Move, MoveStat
from coach.grader import Grade
from coach.live import CoachSession, CoachSettings

FIXTURES = Path(__file__).parent / "fixtures" / "mod"
NOT_REACHABLE = "could not reach the mod: is the game running with the mod loaded?"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def matching_game_version(monkeypatch):
    # The session reads the installed game's version on every settled state; a
    # test never touches the real install for it.
    monkeypatch.setattr("coach.live.read_game_version", lambda: "v0.107.1")


# --- game states -----------------------------------------------------------------
# combat_full.json is a monster fight in act 1, floor 6, round 2, with the action log
# at seq 12. Hand: c1 Strike (aimed), c2 Whirlwind, c3 Ascender's Bane (unplayable),
# c4 True Grit. The Jaw Worm (JAW_WORM_0) is the one living enemy. A Fire Potion
# sits in belt slot 1.

P1_HAND = ("c2", "c3", "c4")  # after Strike (c1)
P2_HAND = ("c3", "c4")  # after Whirlwind (c2) too


def combat(seq=12, *, hand=("c1", "c2", "c3", "c4"), energy=2, jaw_worm_hp=30, round_=2):
    state = load("combat_full.json")
    state["action_log_seq"] = seq
    cards = [card for card in state["player"]["hand"] if card["uid"] in hand]
    for index, card in enumerate(cards):
        card["index"] = index
    state["player"]["hand"] = cards
    state["player"]["energy"] = energy
    state["battle"]["round"] = round_
    state["battle"]["enemies"][0]["hp"] = jaw_worm_hp
    return state


def resolving(state, seq):
    """`state` mid-animation, with the action log already at `seq`."""
    moving = copy.deepcopy(state)
    moving["is_resolving"] = True
    moving["action_log_seq"] = seq
    return moving


def rewards(seq):
    state = load("state_non_combat.json")
    state["state_type"] = "rewards"
    state["rewards"] = {"items": [], "can_proceed": True}
    state["action_log_seq"] = seq
    return state


def choose_a_card(base, seq):
    """An in-combat choose-a-card screen (an Attack Potion's) over `base`'s fight."""
    state = copy.deepcopy(base)
    state["state_type"] = "card_select"
    state["in_combat"] = True
    state["card_select"] = load("combat_card_select_choose.json")["card_select"]
    state["action_log_seq"] = seq
    state["player"]["potions"] = []
    return state


def hand_select(base, seq):
    """An in-combat hand selection (Burning Pact's exhaust) over `base`'s fight."""
    state = copy.deepcopy(base)
    state["state_type"] = "hand_select"
    state["in_combat"] = True
    state["hand_select"] = load("combat_hand_select_with_selection.json")["hand_select"]
    state["action_log_seq"] = seq
    return state


def hand_select_over(base, seq, uids, *, selected=()):
    """A hand selection (an exhaust) over `base`'s own hand cards `uids`, with the
    cards in `selected` already toggled on screen."""
    state = copy.deepcopy(base)
    cards = [dict(card) for card in state["player"]["hand"] if card["uid"] in uids]
    state["state_type"] = "hand_select"
    state["in_combat"] = True
    state["action_log_seq"] = seq
    state["hand_select"] = {
        "mode": "simple_select",
        "prompt": "Select a card to Exhaust.",
        "cards": [card for card in cards if card["uid"] not in selected],
        "can_confirm": bool(selected),
        "selection": {
            "kind": "HAND",
            "pile": None,
            "prompt_key": "card_selection.TO_EXHAUST",
            "source": {"type": "relic", "id": "NEW_RELIC", "uid": None},
            "min": 1,
            "max": 1,
            "can_skip": False,
            "selected": [{"uid": uid, "id": "X"} for uid in selected],
        },
    }
    return state


def after_exhausting(base, seq, uid):
    """`base` once the hand card `uid` has been exhausted."""
    state = copy.deepcopy(base)
    state["action_log_seq"] = seq
    player = state["player"]
    card = next(c for c in player["hand"] if c["uid"] == uid)
    player["hand"] = [c for c in player["hand"] if c["uid"] != uid]
    for index, kept in enumerate(player["hand"]):
        kept["index"] = index
    player["exhaust_pile"].append(card)
    player["exhaust_pile_ordered"].append(
        {"id": card["id"], "is_upgraded": card["is_upgraded"], "uid": uid}
    )
    return state


def with_new_hand_card(base, seq, *, uid, card_id, name):
    """`base` after a choose-a-card pick: the potion is gone and the new card is in hand."""
    state = copy.deepcopy(base)
    state["action_log_seq"] = seq
    state["player"]["potions"] = []
    hand = state["player"]["hand"]
    hand.append(
        {
            "index": len(hand),
            "uid": uid,
            "id": card_id,
            "name": name,
            "type": "Attack",
            "cost": "2",
            "is_upgraded": False,
            "enchantment": None,
            "affliction": None,
            "fields": {},
        }
    )
    return state


# --- action-log entries (STS2MCP docs/raw-full.md, "Action log") -----------------

_CARD_IDS = {"c1": "STRIKE_IRONCLAD", "c2": "WHIRLWIND", "c4": "TRUE_GRIT"}


def _entry(seq, kind, source, round_, **fields):
    return {
        "seq": seq,
        "kind": kind,
        "source": source,
        "utc": "2026-09-25T10:00:00.000Z",
        "act": 1,
        "floor": 6,
        "round": round_,
        **fields,
    }


def play(seq, uid, target=None, *, source="human", round_=2):
    return _entry(
        seq,
        "play_card",
        source,
        round_,
        card_id=_CARD_IDS[uid],
        card_uid=uid,
        target=target,
        target_combat_id=42 if target else None,
    )


def use_potion(seq, slot, target=None, *, source="human", round_=2):
    return _entry(
        seq, "use_potion", source, round_, slot=slot, target=target, target_combat_id=None
    )


def end_turn_entry(seq, *, source="human", round_=2):
    return _entry(seq, "end_turn", source, round_)


# --- fakes -----------------------------------------------------------------------


class FakeBridge:
    """The mod as the session sees it: the current state and the action log.

    `state` is what get_state() returns (a copy), or an exception it raises.
    `entries` is the whole action log; actions_since(since) returns the entries
    after `since`, but none past `visible_up_to` while a test holds some back.
    `base_seq` is the log's newest seq before any of `entries`.
    """

    def __init__(self, state=None, *, base_seq=12):
        self.state = state
        self.entries = []
        self.base_seq = base_seq
        self.visible_up_to = None
        self.actions_since_calls = []

    def get_state(self):
        if isinstance(self.state, BaseException):
            raise self.state
        return copy.deepcopy(self.state)

    def actions_since(self, since):
        self.actions_since_calls.append(since)
        shown = [
            entry
            for entry in self.entries
            if self.visible_up_to is None or entry["seq"] <= self.visible_up_to
        ]
        return {
            "status": "ok",
            "latest_seq": max([self.base_seq] + [entry["seq"] for entry in shown]),
            "entries": [copy.deepcopy(entry) for entry in shown if entry["seq"] > since],
        }


CLEAN_CHECK = CheckResult(
    comparable=True, deterministic_mismatches=[], random_fields=[], implausible=[], warnings=[]
)


class FakeEngine:
    """Stands in for EngineClient. `respond(live)` returns the Analysis (or the
    exception to raise) for a position; `check` does the same for a fidelity check."""

    def __init__(self, respond, *, check=None):
        self.respond = respond
        self.check_respond = check or (lambda pre, move, post: CLEAN_CHECK)
        self.analyze_calls = []
        self.check_calls = []
        self._gate = None

    def hold(self, predicate):
        """Hold every analysis whose live snapshot matches `predicate` until the
        returned event is set."""
        release = threading.Event()
        self._gate = (predicate, release)
        return release

    def analyze(self, live, **kwargs):
        self.analyze_calls.append((live, kwargs))
        if self._gate is not None and self._gate[0](live):
            assert self._gate[1].wait(5), "the test never released the held analysis"
        return self._answer(self.respond(live))

    def check(self, pre, move, post):
        self.check_calls.append((pre, move, post))
        return self._answer(self.check_respond(pre, move, post))

    @staticmethod
    def _answer(outcome):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


# --- analyses --------------------------------------------------------------------


def hand_uids(live):
    return [card["uid"] for card in live["hand"]]


def _stat(kind, *, label, diff=0.0, stderr=0.0, n=50, death=0.0, value=50.0, p_death=0.0, **move):
    fields = dict(
        card_index=None,
        card_uid=None,
        card_id=None,
        potion_slot=None,
        potion_id=None,
        target=None,
        select_index=None,
        hand_indices=[],
        potion_slots=(),
        select_indices=(),
    )
    fields.update(move)
    return MoveStat(
        kind=kind,
        label=label,
        visits=500,
        value_mean=value,
        p_death=p_death,
        paired_diff_vs_best=diff,
        paired_diff_stderr=stderr,
        paired_n=n,
        paired_death_diff_vs_best=death,
        paired_death_diff_stderr=0.0,
        **fields,
    )


def card(live, uid, target=None, **stats):
    index = hand_uids(live).index(uid)
    card_id = live["hand"][index]["id"]
    label = f"{card_id} → {target}" if target else card_id
    return _stat(
        "card",
        label=label,
        card_index=index,
        card_uid=uid,
        card_id=card_id,
        target=target,
        hand_indices=[index],
        **stats,
    )


def end_turn(**stats):
    return _stat("end_turn", label="End turn", **stats)


def potion(slot, potion_id, target=None, **stats):
    label = f"{potion_id} → {target}" if target else potion_id
    return _stat(
        "potion",
        label=label,
        potion_slot=slot,
        potion_slots=(slot,),
        potion_id=potion_id,
        target=target,
        **stats,
    )


def select(index, label, **stats):
    return _stat("select", label=label, select_index=index, select_indices=(index,), **stats)


def skip(**stats):
    return _stat("skip", label="Skip", **stats)


def as_move(stat):
    return Move(**{field.name: getattr(stat, field.name) for field in dataclasses.fields(Move)})


def analysis_of(moves, *, best, line=None, reliable=True, warnings=(), unmapped=0):
    return Analysis(
        reliable=reliable,
        warnings=list(warnings),
        best=as_move(best),
        line=[as_move(m) for m in (line or [best])],
        line_truncated_at_draw=False,
        moves=list(moves),
        sims=1000,
        elapsed_s=0.5,
        budget_exhausted=False,
        select_mapping_failures=unmapped,
    )


def p0_analysis(live, **overrides):
    """P0: True Grit is best; Whirlwind costs 1 HP (Good), Strike 3 HP
    (Inaccuracy), the potion in slot 1 5 HP (Mistake), ending the turn 8 HP."""
    grit = card(live, "c4", value=55.0, p_death=0.02)
    whirlwind = card(live, "c2", diff=1.0, stderr=0.5)
    moves = [grit, whirlwind, card(live, "c1", "JAW_WORM_0", diff=3.0, death=0.01)]
    potion_id = live["player"]["potions"][1]["id"]
    if potion_id is not None:
        target = "JAW_WORM_0" if potion_id == "FIRE_POTION" else None
        moves.append(potion(1, potion_id, target, diff=5.0))
    moves.append(end_turn(diff=8.0))
    return analysis_of(moves, best=grit, line=[grit, whirlwind], **overrides)


def p1_analysis(live):
    """P1 (after Strike): Whirlwind is best; True Grit costs 2.5 HP."""
    whirlwind = card(live, "c2", value=52.0)
    return analysis_of([whirlwind, card(live, "c4", diff=2.5), end_turn(diff=6.0)], best=whirlwind)


def p2_analysis(live):
    if "c4" not in hand_uids(live):  # True Grit is gone too: only the turn's end is left
        end = end_turn()
        return analysis_of([end], best=end)
    grit = card(live, "c4")
    return analysis_of([grit, end_turn(diff=4.0)], best=grit)


def choose_or_standard_analysis(live):
    """The Attack Potion's choose-a-card screen: Bludgeon is best, Uppercut costs 6 HP."""
    if live["pending_selection"] is not None:
        bludgeon = select(0, "Bludgeon")
        uppercut = select(1, "Uppercut", diff=6.0)
        return analysis_of([bludgeon, uppercut, skip(diff=12.0)], best=bludgeon)
    return standard_analysis(live)


def standard_analysis(live):
    uids = hand_uids(live)
    if "c1" in uids:
        return p0_analysis(live)
    if "c2" in uids:
        return p1_analysis(live)
    return p2_analysis(live)


# --- session helpers ---------------------------------------------------------------


@pytest.fixture
def make_session(tmp_path):
    sessions = []

    def make(bridge, engine, settings=None):
        published = []
        session = CoachSession(
            bridge,
            engine,
            publish=published.append,
            data_dir=tmp_path,
            settings=settings or CoachSettings(),
        )
        sessions.append(session)
        return session, published

    yield make
    for session in sessions:
        session.close()


def settle(session, bridge, state):
    """Show `state` for two polls: what it takes for a position to count as settled."""
    bridge.state = state
    session.step()
    session.step()


def act(session, bridge, entry, then):
    """The player makes the move `entry` logs: the screen shows it resolving for a poll
    (the log already has it), and then the position `then` settles."""
    bridge.entries.append(entry)
    bridge.state = resolving(bridge.state, entry["seq"])
    session.step()
    settle(session, bridge, then)


def step_until(session, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for the session"
        session.step()
        time.sleep(0.002)


def latest(published):
    return published[-1]


def advising(published, best_uid=None):
    """True once the latest published state is advice (for `best_uid`, if given)."""

    def condition():
        state = published[-1] if published else None
        if state is None or state.status != "advice":
            return False
        return best_uid is None or state.recommendation["best"]["card_uid"] == best_uid

    return condition


def grade_history(published):
    """Every grade the panel was shown, in the order it was shown."""
    history = []
    for state in published:
        if state.last_grade is not None and (not history or history[-1] is not state.last_grade):
            history.append(state.last_grade)
    return history


def run_log(tmp_path, kind):
    [path] = (tmp_path / "runs").glob("*.jsonl")  # the live coach's run log (spec 5.5)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [line["payload"] for line in lines if line["kind"] == kind]


# --- the whole scenario --------------------------------------------------------------


def test_waiting_then_a_turn_is_analyzed_a_move_graded_and_a_mismatch_writes_a_gap_file(
    make_session, tmp_path
):
    mismatch = CheckResult(
        comparable=True,
        deterministic_mismatches=[{"field": "enemies[0].hp", "predicted": 23, "actual": 24}],
        random_fields=[],
        implausible=[],
        warnings=[],
    )
    bridge = FakeBridge(BridgeUnavailable(NOT_REACHABLE))
    engine = FakeEngine(standard_analysis, check=lambda pre, move, post: mismatch)
    session, published = make_session(bridge, engine)

    session.step()
    assert latest(published).status == "waiting_for_game"
    assert NOT_REACHABLE in latest(published).status_text

    p0 = combat(seq=12)
    bridge.state = p0
    session.step()
    assert latest(published).status == "idle"  # connected; the turn has not settled yet

    session.step()  # the same position on a second poll: the turn has settled
    step_until(session, advising(published))
    assert "thinking" in [state.status for state in published]
    assert latest(published).recommendation["best"]["card_uid"] == "c4"
    assert latest(published).run == {"act": 1, "floor": 6}
    [(analyzed, kwargs)] = engine.analyze_calls
    assert analyzed["schema"] == "sts2-live-snapshot/1"
    assert hand_uids(analyzed) == ["c1", "c2", "c3", "c4"]
    assert kwargs == {"time_budget_s": 1.5}

    # The human plays Strike on the Jaw Worm.
    bridge.entries.append(play(13, "c1", "JAW_WORM_0"))
    bridge.state = resolving(p0, seq=13)
    session.step()
    graded = latest(published).last_grade
    assert graded["label"] == "Inaccuracy"
    assert graded["played"]["card_uid"] == "c1"
    assert graded["played"]["target"] == "JAW_WORM_0"
    assert graded["best"]["card_uid"] == "c4"
    assert engine.check_calls == []  # the check waits for the next settled state

    # The next position settles: it is analyzed, and the move is fidelity-checked.
    p1 = combat(seq=13, hand=P1_HAND, energy=1, jaw_worm_hp=24)
    settle(session, bridge, p1)
    step_until(
        session,
        lambda: (
            advising(published, "c2")()
            and any("mismatch" in w for w in latest(published).recommendation["warnings"])
        ),
    )
    [(pre, move, post)] = engine.check_calls
    assert hand_uids(pre) == ["c1", "c2", "c3", "c4"]
    assert hand_uids(post) == ["c2", "c3", "c4"]
    assert type(move) is Move
    assert move == as_move(card(pre, "c1", "JAW_WORM_0"))

    [gap_file] = (tmp_path / "gaps").glob("*.json")
    assert json.loads(gap_file.read_text(encoding="utf-8")) == {
        "pre": pre,
        "post": post,
        "move": dataclasses.asdict(move),
        "check": dataclasses.asdict(mismatch),
    }
    assert gap_file.name in " ".join(latest(published).recommendation["warnings"])

    # Everything went to the run log: states, the move, analyses, the grade, the check.
    assert [r["state"]["action_log_seq"] for r in run_log(tmp_path, "state")] == [12, 13]
    assert [r["seq"] for r in run_log(tmp_path, "action")] == [13]
    assert [hand_uids(r["live"]) for r in run_log(tmp_path, "analysis")] == [
        ["c1", "c2", "c3", "c4"],
        ["c2", "c3", "c4"],
    ]
    [grade_record] = run_log(tmp_path, "grade")
    assert grade_record["seq"] == 13
    assert grade_record["grade"] == graded
    [check_record] = run_log(tmp_path, "check")
    assert check_record["result"] == dataclasses.asdict(mismatch)
    assert check_record["gap"] == str(gap_file)


def test_advice_carries_the_best_move_its_line_alternatives_by_cost_hp_loss_and_death_risk(
    make_session,
):
    bridge = FakeBridge()
    session, published = make_session(bridge, FakeEngine(standard_analysis))

    settle(session, bridge, combat(seq=12))
    step_until(session, advising(published))

    advice = latest(published)
    assert advice.status_text == "Advice ready."
    assert advice.mode == "show"
    assert advice.versions.ok is True
    recommendation = advice.recommendation
    assert recommendation["best"]["label"] == "TRUE_GRIT"
    assert [move["card_uid"] for move in recommendation["line"]] == ["c4", "c2"]
    alternatives = recommendation["alternatives"]
    assert [a["move"]["label"] for a in alternatives] == [
        "WHIRLWIND",
        "STRIKE_IRONCLAD → JAW_WORM_0",
        "FIRE_POTION → JAW_WORM_0",
    ]
    assert [a["cost_mean_hp"] for a in alternatives] == [1.0, 3.0, 5.0]
    assert alternatives[0]["cost_lower_hp"] == pytest.approx(1.0 - 1.645 * 0.5)
    assert alternatives[1]["p_death_delta"] == pytest.approx(0.01)
    # 61 HP plus one potion held (10 HP each), against the best move's value if the
    # fight is won: (55 + 0.02 * 100) / 0.98.
    assert recommendation["expected_hp_loss"] == pytest.approx(71 - 57 / 0.98)
    assert recommendation["p_death"] == pytest.approx(0.02)
    assert recommendation["reliable"] is True
    assert recommendation["warnings"] == []


def test_a_failed_fidelity_check_is_a_warning_and_a_not_comparable_one_writes_no_gap(
    make_session, tmp_path
):
    outcomes = [
        EngineError("engine returned unknown_method: no check here", error_type="unknown_method"),
        CheckResult(
            comparable=False,
            deterministic_mismatches=[],
            random_fields=[],
            implausible=[],
            warnings=["pre has unmodelled content"],
        ),
    ]
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis, check=lambda pre, move, post: outcomes.pop(0))
    session, published = make_session(bridge, engine)
    settle(session, bridge, combat(seq=12))
    step_until(session, advising(published))

    act(session, bridge, play(13, "c1", "JAW_WORM_0"), combat(seq=13, hand=P1_HAND))
    step_until(
        session,
        lambda: advising(published, "c2")() and latest(published).recommendation["warnings"],
    )
    [warning] = latest(published).recommendation["warnings"]
    assert warning == (
        "The fidelity check after STRIKE_IRONCLAD → JAW_WORM_0 failed: "
        "engine returned unknown_method: no check here."
    )

    # The simulator can't compare this one (unmodelled content): no gap report, but
    # the check's own warning reaches the panel too.
    act(session, bridge, play(14, "c2"), combat(seq=14, hand=P2_HAND))
    step_until(session, lambda: len(run_log(tmp_path, "check")) == 2)
    step_until(session, advising(published, "c4"))
    assert not (tmp_path / "gaps").exists()
    assert latest(published).recommendation["warnings"] == [
        warning,
        "pre has unmodelled content",
    ]


def test_a_mismatch_warning_stays_up_for_ten_moves_then_clears(make_session):
    # Spec 6.3: the warning line shows a mismatch from the last 10 moves.
    mismatch = CheckResult(
        comparable=True,
        deterministic_mismatches=[{"field": "player.block", "predicted": 5, "actual": 6}],
        random_fields=[],
        implausible=[],
        warnings=[],
    )
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis, check=lambda pre, move, post: mismatch)
    session, published = make_session(bridge, engine)
    settle(session, bridge, combat(seq=12))
    step_until(session, advising(published))
    act(session, bridge, play(13, "c1", "JAW_WORM_0"), combat(seq=13, hand=P1_HAND))
    step_until(
        session,
        lambda: advising(published, "c2")() and latest(published).recommendation["warnings"],
    )
    [warning] = latest(published).recommendation["warnings"]
    assert warning.startswith(
        "Fidelity mismatch after STRIKE_IRONCLAD → JAW_WORM_0: "
        "player.block predicted 5, the game showed 6; gap report "
    )

    bridge.entries += [end_turn_entry(seq) for seq in range(14, 23)]  # nine more moves
    session.step()
    assert latest(published).recommendation["warnings"] == [warning]

    bridge.entries.append(end_turn_entry(23))  # the tenth after it
    session.step()
    assert latest(published).recommendation["warnings"] == []


# --- Review Focus 1: the player acts before the analysis finishes ----------------------


def test_a_move_made_before_its_analysis_finished_is_graded_on_that_analysis(
    make_session, tmp_path
):
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    release_p0 = engine.hold(lambda live: "c1" in hand_uids(live))
    session, published = make_session(bridge, engine)

    p0 = combat(seq=12)
    settle(session, bridge, p0)
    assert latest(published).status == "thinking"

    bridge.entries.append(play(13, "c1", "JAW_WORM_0"))
    bridge.state = resolving(p0, seq=13)
    session.step()
    assert latest(published).last_grade is None  # waiting on P0's analysis

    settle(session, bridge, combat(seq=13, hand=P1_HAND, energy=1, jaw_worm_hp=24))
    assert latest(published).status == "thinking"  # P1 is the position on screen now
    p1_since = len(published) - 1

    release_p0.set()
    step_until(session, advising(published, "c2"))

    # P0's result came in after P1 was on screen: it never reached the panel...
    for state in published[p1_since:]:
        assert state.recommendation is None or state.recommendation["best"]["card_uid"] == "c2"
    # ...but it still graded the move made from P0.
    [graded] = grade_history(published)
    assert graded["label"] == "Inaccuracy"
    assert graded["best"]["card_uid"] == "c4"
    stale, fresh = run_log(tmp_path, "analysis")
    assert (hand_uids(stale["live"]), stale["current"]) == (["c1", "c2", "c3", "c4"], False)
    assert (hand_uids(fresh["live"]), fresh["current"]) == (["c2", "c3", "c4"], True)


def test_the_newest_position_is_analyzed_next_ahead_of_older_queued_ones(make_session):
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    release_p0 = engine.hold(lambda live: "c1" in hand_uids(live))
    session, published = make_session(bridge, engine)

    settle(session, bridge, combat(seq=12))
    act(session, bridge, play(13, "c1", "JAW_WORM_0"), combat(seq=13, hand=P1_HAND))
    act(session, bridge, play(14, "c2"), combat(seq=14, hand=P2_HAND))
    bridge.entries.append(end_turn_entry(15))  # made from P2
    session.step()
    release_p0.set()
    step_until(session, lambda: len(grade_history(published)) == 3)

    # P0 was already running; P2 (on screen) went next, ahead of P1, which only a
    # grade still needed.
    assert [hand_uids(live) for live, _ in engine.analyze_calls] == [
        ["c1", "c2", "c3", "c4"],
        ["c3", "c4"],
        ["c2", "c3", "c4"],
    ]
    # P2's analysis landed before P1's, yet the grades still come in move order.
    assert [(g["played"]["label"], g["label"]) for g in grade_history(published)] == [
        ("STRIKE_IRONCLAD → JAW_WORM_0", "Inaccuracy"),
        ("WHIRLWIND", "Best"),
        ("End turn", "Inaccuracy"),
    ]
    assert latest(published).recommendation["best"]["card_uid"] == "c4"


def test_a_move_clicked_between_the_two_polls_that_settle_a_position_is_matched_to_it(
    make_session,
):
    bridge = FakeBridge()
    session, published = make_session(bridge, FakeEngine(standard_analysis))
    bridge.state = combat(seq=12)
    session.step()  # P0's first poll, with the log at 12

    # Strike is clicked before the next poll: the log has it, but the screen still
    # shows P0 unchanged (the play has not started resolving).
    bridge.entries.append(play(13, "c1", "JAW_WORM_0"))
    bridge.state = combat(seq=13)
    session.step()  # P0 settles on this poll, which already reads the log at 13
    step_until(session, lambda: grade_history(published))

    [graded] = grade_history(published)
    assert graded["label"] == "Inaccuracy"  # graded on P0, not left Ungraded


def test_an_analysis_nobody_needs_any_more_is_skipped(make_session, tmp_path):
    # Review minor 2. P1 is left by an api move, so no grade needs its analysis;
    # by its turn it is not on screen either. It is skipped, and the grade for the
    # move from P2 does not wait behind it.
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    release_p0 = engine.hold(lambda live: "c1" in hand_uids(live))
    session, published = make_session(bridge, engine)
    settle(session, bridge, combat(seq=12))
    act(session, bridge, play(13, "c1", "JAW_WORM_0"), combat(seq=13, hand=P1_HAND))
    act(session, bridge, play(14, "c2", source="api"), combat(seq=14, hand=P2_HAND))
    act(session, bridge, play(15, "c4"), combat(seq=15, hand=("c3",)))

    release_p0.set()
    step_until(session, lambda: len(grade_history(published)) == 2)
    step_until(session, advising(published))

    assert [hand_uids(live) for live, _ in engine.analyze_calls] == [
        ["c1", "c2", "c3", "c4"],
        ["c3"],  # P3, on screen, first
        ["c3", "c4"],  # then P2, for the grade; P1 never
    ]
    assert [(g["played"]["card_uid"], g["label"]) for g in grade_history(published)] == [
        ("c1", "Inaccuracy"),
        ("c4", "Best"),
    ]
    assert [r["skipped"] for r in run_log(tmp_path, "analysis") if "skipped" in r] == [True]


def test_a_skipped_analysis_a_late_move_turns_out_to_need_is_run_after_all(make_session, tmp_path):
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    release_p0 = engine.hold(lambda live: "c1" in hand_uids(live))
    session, published = make_session(bridge, engine)
    settle(session, bridge, combat(seq=12))
    act(session, bridge, play(13, "c1", "JAW_WORM_0"), combat(seq=13, hand=P1_HAND))
    # Whirlwind is played from P1, but the log only hands it over later.
    bridge.visible_up_to = 13
    act(session, bridge, play(14, "c2"), combat(seq=14, hand=P2_HAND))

    release_p0.set()
    step_until(session, lambda: any(r.get("skipped") for r in run_log(tmp_path, "analysis")))
    bridge.visible_up_to = None
    step_until(session, lambda: len(grade_history(published)) == 2)

    assert [(g["played"]["card_uid"], g["label"]) for g in grade_history(published)] == [
        ("c1", "Inaccuracy"),
        ("c2", "Best"),  # graded on P1's analysis after all
    ]
    assert [hand_uids(live) for live, _ in engine.analyze_calls] == [
        ["c1", "c2", "c3", "c4"],
        ["c3", "c4"],
        ["c2", "c3", "c4"],
    ]


def test_a_skip_decided_before_the_position_came_back_on_screen_is_undone(make_session):
    # The worker decides a skip on its own thread. Here it skips P1 while a popup is
    # on screen, and P1 comes back on screen before that skip is applied: P1 must be
    # analyzed after all, not left thinking forever.
    class HookedBridge(FakeBridge):
        hook = None

        def get_state(self):
            if self.hook is not None:
                hook, self.hook = self.hook, None
                hook()
            return super().get_state()

    bridge = HookedBridge()
    engine = FakeEngine(standard_analysis)
    release_p0 = engine.hold(lambda live: "c1" in hand_uids(live))
    session, published = make_session(bridge, engine)
    settle(session, bridge, combat(seq=12))
    p1 = combat(seq=13, hand=P1_HAND)
    act(session, bridge, play(13, "c1", "JAW_WORM_0"), p1)
    popup = load("state_non_combat.json")
    popup.update(state_type="overlay", action_log_seq=13)
    settle(session, bridge, popup)  # P1 is off screen
    bridge.state = p1
    session.step()  # P1's first poll again

    def skip_lands_during_this_poll():
        release_p0.set()
        deadline = time.monotonic() + 5
        while session._results.qsize() < 2:  # P0's analysis, then P1 skipped
            assert time.monotonic() < deadline, "the worker never skipped P1"
            time.sleep(0.001)

    bridge.hook = skip_lands_during_this_poll
    session.step()  # P1 settles on screen again, and then the skip is applied
    step_until(session, advising(published, "c2"))


# --- Review Focus 3: several actions between two polls --------------------------------


def test_a_position_first_seen_after_the_log_moved_on_is_never_bound_to_a_move(make_session):
    # Review Important 1 (the reviewer's reproduction). P1 appears after Strike, but
    # Whirlwind is clicked before the coach first polls it and P1 stays on screen for
    # both settling polls, so its first poll already reads the log at 14. From the
    # coach's side P1 could be the position before Whirlwind or the one after it:
    # recorded at 14, True Grit (played from P2, after Whirlwind) would be graded on
    # P1's analysis and fidelity-checked from P1.
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    session, published = make_session(bridge, engine)
    p0 = combat(seq=12)
    settle(session, bridge, p0)
    step_until(session, advising(published, "c4"))

    bridge.entries.append(play(13, "c1", "JAW_WORM_0"))  # Strike, seen resolving
    bridge.state = resolving(p0, seq=13)
    session.step()

    bridge.entries.append(play(14, "c2"))  # Whirlwind, clicked before P1's first poll
    settle(session, bridge, combat(seq=14, hand=P1_HAND, energy=1, jaw_worm_hp=24))

    # Whirlwind resolves into P2, which never settles; True Grit is played from P2.
    bridge.entries.append(play(15, "c4"))
    bridge.state = resolving(combat(seq=15, hand=P2_HAND, energy=0, jaw_worm_hp=20), seq=15)
    session.step()
    step_until(session, lambda: len(grade_history(published)) == 3)

    # P3, after True Grit, settles.
    settle(session, bridge, combat(seq=15, hand=("c3",), energy=0, jaw_worm_hp=20))
    step_until(session, advising(published))

    graded = [(g["played"]["card_uid"], g["label"]) for g in grade_history(published)]
    assert graded == [("c1", "Inaccuracy"), ("c2", "Ungraded"), ("c4", "Ungraded")]
    assert engine.check_calls == []  # nothing is checked from P1, nor into it


def test_a_selection_between_two_ambiguous_first_sights_is_not_graded(make_session):
    # Neither the screen nor the position after it has a known seq (each was first
    # seen after the log had moved on), so "no move in between" cannot be shown and
    # the diff of the two could mix the pick with a move.
    def respond(live):
        if live["pending_selection"] is not None:
            strike = select(0, "Exhaust Strike")
            return analysis_of([strike, select(1, "Exhaust True Grit")], best=strike)
        return standard_analysis(live)

    bridge = FakeBridge()
    engine = FakeEngine(respond)
    session, published = make_session(bridge, engine)
    settle(session, bridge, combat(seq=12))
    step_until(session, advising(published))

    after_whirlwind = combat(seq=13, hand=("c1", "c3", "c4"))
    bridge.entries.append(play(13, "c2"))  # its effect opens an exhaust screen
    settle(session, bridge, hand_select_over(after_whirlwind, 13, ("c1", "c4")))
    step_until(session, lambda: len(engine.analyze_calls) == 2 and advising(published)())
    bridge.entries.append(play(14, "c4"))  # clicked before the next position's first poll
    settle(session, bridge, after_exhausting(after_whirlwind, 14, "c1"))
    step_until(session, lambda: len(engine.analyze_calls) == 3 and advising(published)())

    assert [g["played"]["kind"] for g in grade_history(published)] == ["card", "card"]


def test_two_moves_read_in_one_poll_are_each_graded_on_their_own_position_in_order(
    make_session, tmp_path
):
    bridge = FakeBridge()
    session, published = make_session(bridge, FakeEngine(standard_analysis))
    settle(session, bridge, combat(seq=12))
    step_until(session, advising(published, "c4"))

    # Strike, then Whirlwind. Both positions settle on screen (Strike seen resolving
    # in between), but the action log hands over the two moves in a single read.
    bridge.visible_up_to = 12
    bridge.entries += [play(13, "c1", "JAW_WORM_0"), play(14, "c2")]
    bridge.state = resolving(bridge.state, seq=13)
    session.step()
    settle(session, bridge, combat(seq=13, hand=P1_HAND))
    step_until(session, advising(published, "c2"))
    assert latest(published).last_grade is None

    bridge.visible_up_to = None
    bridge.state = resolving(combat(seq=14, hand=P2_HAND), seq=14)
    session.step()

    assert [
        (g["played"]["card_uid"], g["label"], g["best"]["card_uid"])
        for g in grade_history(published)
    ] == [("c1", "Inaccuracy", "c4"), ("c2", "Best", "c2")]
    assert [r["seq"] for r in run_log(tmp_path, "grade")] == [13, 14]


def test_a_move_from_a_position_that_never_settled_is_ungraded_and_never_misattributed(
    make_session,
):
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    session, published = make_session(bridge, engine)
    settle(session, bridge, combat(seq=12))
    step_until(session, advising(published))

    # Two quick plays: the position between them never settles on screen.
    bridge.entries += [play(13, "c1", "JAW_WORM_0"), play(14, "c2")]
    bridge.state = resolving(combat(seq=14, hand=P2_HAND), seq=14)
    session.step()

    first, second = grade_history(published)
    assert (first["played"]["card_uid"], first["label"]) == ("c1", "Inaccuracy")
    assert set(second) == {field.name for field in dataclasses.fields(Grade)}
    assert second["label"] == "Ungraded"
    assert second["best"] is None
    assert second["played"]["card_uid"] == "c2"
    assert second["played"]["label"] == "WHIRLWIND"

    # The next settled position comes after both moves, so it is no fair "after"
    # for Strike alone: no fidelity check is run against it.
    settle(session, bridge, combat(seq=14, hand=P2_HAND))
    step_until(session, advising(published, "c4"))
    assert engine.check_calls == []


def test_api_moves_are_logged_but_never_graded_or_checked(make_session, tmp_path):
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    session, published = make_session(bridge, engine)
    p0 = combat(seq=12)
    settle(session, bridge, p0)
    step_until(session, advising(published))

    bridge.entries.append(play(13, "c1", "JAW_WORM_0", source="api"))
    bridge.state = resolving(p0, seq=13)
    session.step()
    settle(session, bridge, combat(seq=13, hand=P1_HAND))
    step_until(session, advising(published, "c2"))

    assert grade_history(published) == []
    assert engine.check_calls == []
    assert run_log(tmp_path, "grade") == []
    assert [(r["seq"], r["source"]) for r in run_log(tmp_path, "action")] == [(13, "api")]


def test_a_pick_on_a_screen_an_api_move_opened_stays_ungraded(make_session, tmp_path):
    # Review minor 5: the api drinks an Attack Potion; the choose-a-card screen it
    # opens is the api's to pick from too, so neither the potion nor the pick is graded.
    bridge = FakeBridge()
    engine = FakeEngine(choose_or_standard_analysis)
    session, published = make_session(bridge, engine)
    p0 = combat(seq=12)
    p0["player"]["potions"][0]["id"] = "ATTACK_POTION"
    settle(session, bridge, p0)
    step_until(session, advising(published))

    act(session, bridge, use_potion(13, 1, source="api"), choose_a_card(p0, seq=13))
    step_until(session, lambda: len(engine.analyze_calls) == 2 and advising(published)())
    picked = with_new_hand_card(p0, 13, uid="UPPERCUT#1", card_id="UPPERCUT", name="Uppercut")
    settle(session, bridge, picked)
    step_until(session, lambda: len(engine.analyze_calls) == 3 and advising(published)())

    assert grade_history(published) == []
    assert run_log(tmp_path, "grade") == []
    assert engine.check_calls == []


def test_an_out_of_combat_potion_is_logged_but_never_graded(make_session, tmp_path):
    # Ruling R4: selling a potion to the merchant is a use_potion with round null.
    bridge = FakeBridge()
    session, published = make_session(bridge, FakeEngine(standard_analysis))
    shop = load("state_non_combat.json")
    shop["action_log_seq"] = 12
    settle(session, bridge, shop)

    bridge.entries.append(use_potion(13, 0, round_=None))
    session.step()

    assert latest(published).status == "idle"
    assert grade_history(published) == []
    assert [r["kind"] for r in run_log(tmp_path, "action")] == ["use_potion"]


def test_a_move_logged_for_another_round_is_not_graded_on_this_position(make_session):
    bridge = FakeBridge()
    session, published = make_session(bridge, FakeEngine(standard_analysis))
    settle(session, bridge, combat(seq=12, round_=2))
    step_until(session, advising(published))

    bridge.entries.append(play(13, "c1", "JAW_WORM_0", round_=3))
    session.step()

    [graded] = grade_history(published)
    assert graded["label"] == "Ungraded"


def test_the_move_that_ends_a_fight_is_never_checked_against_the_next_fight(make_session):
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    session, published = make_session(bridge, engine)
    p0 = combat(seq=12)
    settle(session, bridge, p0)
    step_until(session, advising(published))

    bridge.entries.append(play(13, "c1", "JAW_WORM_0"))  # the killing blow
    bridge.state = resolving(p0, seq=13)
    session.step()
    settle(session, bridge, rewards(seq=13))
    assert latest(published).status == "idle"
    # Proceeding through the map logs no move, so the next fight opens at seq 13 too.
    settle(session, bridge, combat(seq=13, hand=P1_HAND, round_=1))
    step_until(session, advising(published, "c2"))

    assert [g["label"] for g in grade_history(published)] == ["Inaccuracy"]
    assert engine.check_calls == []


def test_a_failed_publish_is_retried_and_no_log_entry_is_handled_twice(tmp_path):
    # Review minor 1. Publishing the second of two grades fails: the entry it came
    # from must not be read again on the next poll, and the panel must still get it.
    published, failed = [], []

    def flaky_publish(state):
        grade = state.last_grade
        if grade is not None and grade["played"]["label"] == "End turn" and not failed:
            failed.append(state)
            raise RuntimeError("the panel went away")
        published.append(state)

    bridge = FakeBridge()
    session = CoachSession(
        bridge,
        FakeEngine(standard_analysis),
        publish=flaky_publish,
        data_dir=tmp_path,
        settings=CoachSettings(),
    )
    try:
        settle(session, bridge, combat(seq=12))
        step_until(session, advising(published))
        bridge.entries += [play(13, "c1", "JAW_WORM_0"), end_turn_entry(14)]
        with pytest.raises(RuntimeError, match="the panel went away"):
            session.step()
        session.step()
    finally:
        session.close()

    assert [r["seq"] for r in run_log(tmp_path, "grade")] == [13, 14]
    assert bridge.actions_since_calls[-1] == 14
    assert latest(published).last_grade["played"]["label"] == "End turn"


# --- ruling R5: a game restart or dropped entries in the action log ------------------


def test_a_game_restart_in_the_action_log_drops_old_positions_and_grades_nothing_across_it(
    make_session, tmp_path
):
    bridge = FakeBridge()
    session, published = make_session(bridge, FakeEngine(standard_analysis))
    p0 = combat(seq=12)
    settle(session, bridge, p0)
    step_until(session, advising(published))
    bridge.entries.append(play(13, "c1", "JAW_WORM_0"))
    session.step()
    assert [g["label"] for g in grade_history(published)] == ["Inaccuracy"]

    # The game restarted: its fresh action log is behind the coach's position in it.
    bridge.entries = []
    bridge.base_seq = 12
    session.step()
    assert bridge.actions_since_calls[-1] == 13
    session.step()
    assert bridge.actions_since_calls[-1] == 12  # reset to the new game's latest seq

    # A move in the new game whose seq happens to follow the old P0's is not graded
    # on the old P0: that position was dropped with the restart.
    bridge.entries = [play(13, "c1", "JAW_WORM_0")]
    session.step()
    assert [g["label"] for g in grade_history(published)] == ["Inaccuracy", "Ungraded"]
    [gap] = run_log(tmp_path, "log_gap")
    assert (gap["since"], gap["latest_seq"]) == (13, 12)


def test_dropped_log_entries_reset_the_log_position_and_grade_nothing_across_the_gap(
    make_session, tmp_path
):
    bridge = FakeBridge()
    session, published = make_session(bridge, FakeEngine(standard_analysis))
    settle(session, bridge, combat(seq=12))
    step_until(session, advising(published))

    # The first entry handed over is past since + 1: 13 and 14 were dropped.
    bridge.entries = [play(15, "c1", "JAW_WORM_0"), play(16, "c2")]
    session.step()
    session.step()

    assert grade_history(published) == []
    assert [r["seq"] for r in run_log(tmp_path, "action")] == [15, 16]
    assert bridge.actions_since_calls[-1] == 16


# --- advice off: versions, the engine, an unreadable position -------------------------


def test_a_version_mismatch_turns_advice_off_with_the_reason_and_never_asks_the_engine(
    make_session,
):
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    session, published = make_session(bridge, engine)
    state = combat(seq=12)
    state["schema_version"] = 5

    settle(session, bridge, state)
    session.step()

    advice_off = latest(published)
    assert advice_off.status == "advice_off"
    assert "schema_version 5 does not match expected 4" in advice_off.status_text
    assert advice_off.recommendation is None
    assert advice_off.versions.ok is False
    assert engine.analyze_calls == []


def test_the_version_override_allows_advice_anyway_with_a_warning(make_session):
    settings = CoachSettings()
    bridge = FakeBridge()
    session, published = make_session(bridge, FakeEngine(standard_analysis), settings)
    state = combat(seq=12)
    state["schema_version"] = 5
    settle(session, bridge, state)
    assert latest(published).status == "advice_off"

    settings.override_versions = True  # flipped from the panel's settings
    step_until(session, advising(published))

    warnings = latest(published).recommendation["warnings"]
    assert any("overridden" in w and "schema_version 5" in w for w in warnings)


def test_the_clients_own_restart_shows_engine_restarting_then_advice(make_session):
    # Ruling (Review Focus 2): the session hears the client restart the service
    # through its on_restart, while the position on screen waits for its analysis.
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    session, published = make_session(bridge, engine)
    assert callable(engine.on_restart)  # the session installed itself on the engine

    def respond(live):
        if len(engine.analyze_calls) == 1:
            engine.on_restart()  # the service crashed: the client restarts it and retries
        return standard_analysis(live)

    engine.respond = respond
    settle(session, bridge, combat(seq=12))
    step_until(session, advising(published))

    statuses = [state.status for state in published]
    assert statuses.index("thinking") < statuses.index("engine_restarting") < len(statuses) - 1
    restarting = published[statuses.index("engine_restarting")]
    assert restarting.status_text == "The engine service stopped; restarting it."
    assert len(engine.analyze_calls) == 1  # the retry is the client's, not a second request

    # The restart is over once its request came back: the next position just thinks.
    since = len(published)
    act(session, bridge, play(13, "c1", "JAW_WORM_0"), combat(seq=13, hand=P1_HAND))
    step_until(session, advising(published, "c2"))
    later = [state.status for state in published[since:]]
    assert "thinking" in later and "engine_restarting" not in later


def test_a_restart_during_a_background_check_leaves_the_advice_on_screen(make_session):
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)

    def check(pre, move, post):
        engine.on_restart()  # the client restarts the service mid-check
        return CLEAN_CHECK

    engine.check_respond = check
    session, published = make_session(bridge, engine)
    settle(session, bridge, combat(seq=12))
    step_until(session, advising(published))
    act(session, bridge, play(13, "c1", "JAW_WORM_0"), combat(seq=13, hand=P1_HAND))
    step_until(session, lambda: engine.check_calls and advising(published, "c2")())
    since_advice = len(published) - 1
    for _ in range(5):
        session.step()

    assert all(state.status == "advice" for state in published[since_advice:])


def test_an_engine_that_stays_down_turns_advice_off_and_the_next_position_tries_again(
    make_session,
):
    # EngineUnavailable means the client's own restart and retry failed too.
    failures = [EngineUnavailable("engine service unavailable after a restart: it exited")]
    bridge = FakeBridge()
    engine = FakeEngine(lambda live: failures.pop() if failures else standard_analysis(live))
    session, published = make_session(bridge, engine)

    settle(session, bridge, combat(seq=12))
    step_until(session, lambda: latest(published).status == "advice_off")
    assert "engine service is unavailable" in latest(published).status_text

    act(session, bridge, play(13, "c1", "JAW_WORM_0"), combat(seq=13, hand=P1_HAND))
    step_until(session, advising(published, "c2"))
    assert len(engine.analyze_calls) == 2  # no extra retry: the next position tries again


def test_an_engine_error_turns_advice_off_for_that_position_with_the_engines_message(
    make_session,
):
    error = EngineError("engine returned conversion: unknown card id XYZ", error_type="conversion")
    bridge = FakeBridge()
    session, published = make_session(bridge, FakeEngine(lambda live: error))

    settle(session, bridge, combat(seq=12))
    step_until(session, lambda: latest(published).status == "advice_off")

    assert "unknown card id XYZ" in latest(published).status_text
    assert "\n" not in latest(published).status_text


def test_a_position_the_coach_cannot_read_turns_advice_off_naming_the_field(make_session):
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    session, published = make_session(bridge, engine)
    state = combat(seq=12)
    del state["player"]["hp"]

    settle(session, bridge, state)

    assert latest(published).status == "advice_off"
    assert "player.hp" in latest(published).status_text
    assert engine.analyze_calls == []


def test_a_lost_game_shows_waiting_and_the_advice_comes_back_without_a_new_analysis(
    make_session,
):
    bridge = FakeBridge()
    engine = FakeEngine(standard_analysis)
    session, published = make_session(bridge, engine)
    p0 = combat(seq=12)
    settle(session, bridge, p0)
    step_until(session, advising(published))

    bridge.state = BridgeAuthError("token rejected: reinstall the mod")
    session.step()
    assert latest(published).status == "waiting_for_game"
    assert "token rejected" in latest(published).status_text
    assert latest(published).recommendation is None

    bridge.state = p0
    session.step()
    assert latest(published).status == "advice"
    assert len(engine.analyze_calls) == 1


# --- quiz mode -----------------------------------------------------------------------


def test_quiz_mode_hides_the_best_move_until_the_move_from_that_position_is_graded(
    make_session,
):
    bridge = FakeBridge()
    session, published = make_session(
        bridge, FakeEngine(standard_analysis), CoachSettings(mode="quiz")
    )
    p0 = combat(seq=12)
    settle(session, bridge, p0)
    step_until(session, lambda: latest(published).status == "advice")

    quiz = latest(published)
    assert quiz.mode == "quiz"
    assert quiz.recommendation["best"] is None
    assert quiz.recommendation["line"] is None
    assert quiz.recommendation["alternatives"] is None
    assert quiz.recommendation["p_death"] == pytest.approx(0.02)
    assert "quiz" in quiz.status_text.lower()

    bridge.entries.append(play(13, "c1", "JAW_WORM_0"))
    bridge.state = resolving(p0, seq=13)
    session.step()
    revealed = latest(published)
    assert revealed.last_grade["label"] == "Inaccuracy"
    assert revealed.recommendation["best"]["card_uid"] == "c4"
    assert revealed.recommendation["alternatives"]

    settle(session, bridge, combat(seq=13, hand=P1_HAND))
    step_until(session, lambda: latest(published).status == "advice")
    assert latest(published).recommendation["best"] is None  # P1 has not been graded yet

    session.set_mode("show")
    session.step()
    assert latest(published).mode == "show"
    assert latest(published).recommendation["best"]["card_uid"] == "c2"
    with pytest.raises(ValueError):
        session.set_mode("blind")


# --- in-combat selections, the builder's warnings, unreliable positions ---------------


def test_an_in_combat_choose_a_card_is_analyzed_with_the_fights_room_type_and_graded(
    make_session,
):
    bridge = FakeBridge()
    engine = FakeEngine(choose_or_standard_analysis)
    session, published = make_session(bridge, engine)
    p0 = combat(seq=12)
    p0["player"]["potions"][0]["id"] = "ATTACK_POTION"
    settle(session, bridge, p0)
    step_until(session, advising(published))

    screen = choose_a_card(p0, seq=13)
    act(session, bridge, use_potion(13, 1), screen)
    step_until(
        session, lambda: latest(published).status == "advice" and len(engine.analyze_calls) == 2
    )
    analyzed = engine.analyze_calls[1][0]
    assert analyzed["room_type"] == "monster"  # remembered from the fight's own state
    assert analyzed["pending_selection"]["kind"] == "CHOOSE"

    picked = with_new_hand_card(p0, 13, uid="UPPERCUT#1", card_id="UPPERCUT", name="Uppercut")
    settle(session, bridge, picked)
    step_until(session, lambda: len(engine.check_calls) == 2)

    assert [(g["played"]["label"], g["label"]) for g in grade_history(published)] == [
        ("ATTACK_POTION", "Mistake"),
        ("Uppercut", "Mistake"),
    ]
    (potion_pre, potion_move, potion_post), (pick_pre, pick_move, pick_post) = engine.check_calls
    assert (potion_move.kind, potion_move.potion_slot) == ("potion", 1)
    assert potion_post["pending_selection"]["kind"] == "CHOOSE"
    assert (pick_move.kind, pick_move.select_index) == ("select", 1)
    assert pick_pre == potion_post
    assert "UPPERCUT#1" in hand_uids(pick_post)


def test_a_partial_pick_is_not_graded_and_the_confirmed_exhaust_is(make_session):
    # Ruling (grade_selection): the mod builds player.hand from the fight's hand pile,
    # which a toggled card only leaves on confirmation, so a partial pick is no new
    # position. The confirmed pick moves the card to the exhaust pile: still in a
    # pile, but it left the hand, and that is the pick.
    def respond(live):
        if live["pending_selection"] is not None:
            strike = select(0, "Exhaust Strike", diff=2.5)
            whirlwind = select(1, "Exhaust Whirlwind")
            return analysis_of([strike, whirlwind, select(2, "Exhaust True Grit")], best=whirlwind)
        return standard_analysis(live)

    bridge = FakeBridge()
    engine = FakeEngine(respond)
    session, published = make_session(bridge, engine)
    p0 = combat(seq=12)
    settle(session, bridge, p0)
    step_until(session, advising(published))
    screen = hand_select_over(p0, 12, ("c1", "c2", "c4"))
    settle(session, bridge, screen)  # opened by a relic: no move in the log
    step_until(session, lambda: len(engine.analyze_calls) == 2 and advising(published)())

    bridge.state = hand_select_over(p0, 12, ("c1", "c2", "c4"), selected=("c1",))
    for _ in range(4):
        session.step()
    assert grade_history(published) == []
    assert len(engine.analyze_calls) == 2  # the same position: nothing new to analyze

    settle(session, bridge, after_exhausting(p0, 12, "c1"))
    step_until(session, lambda: grade_history(published))
    [graded] = grade_history(published)
    assert (graded["played"]["label"], graded["label"]) == ("Exhaust Strike", "Inaccuracy")


def test_a_hand_selection_shows_the_snapshot_builders_warnings_with_the_engines(make_session):
    def respond(live):
        if live["pending_selection"] is not None:
            defend = select(0, "Defend")
            return analysis_of(
                [defend, select(1, "Strike", diff=2.0)],
                best=defend,
                reliable=False,
                warnings=["NEW_RELIC is not modelled"],
                unmapped=1,
            )
        return standard_analysis(live)

    bridge = FakeBridge()
    engine = FakeEngine(respond)
    session, published = make_session(bridge, engine)
    p0 = combat(seq=12)
    settle(session, bridge, p0)
    settle(session, bridge, hand_select(p0, seq=12))
    step_until(
        session, lambda: latest(published).status == "advice" and len(engine.analyze_calls) == 2
    )

    analyzed = engine.analyze_calls[1][0]
    assert analyzed["room_type"] == "monster"
    assert analyzed["pending_selection"]["kind"] == "HAND"
    recommendation = latest(published).recommendation
    assert recommendation["reliable"] is False
    # The builder found no play-pile card for Burning Pact (its own warning) and the
    # engine flagged unmodelled content: both reach the panel together.
    assert any("played_card" in w for w in recommendation["warnings"])
    assert "NEW_RELIC is not modelled" in recommendation["warnings"]
    # The engine only counts the selection options it could not map; that too
    # reaches the panel as a sentence.
    assert any("1 selection option(s)" in w for w in recommendation["warnings"])


def test_an_unreliable_position_still_gets_advice_and_its_grades_are_marked_unreliable(
    make_session,
):
    # Review Focus 4.
    bridge = FakeBridge()
    engine = FakeEngine(
        lambda live: p0_analysis(live, reliable=False, warnings=["NEW_RELIC is not modelled"])
    )
    session, published = make_session(bridge, engine)
    p0 = combat(seq=12)
    settle(session, bridge, p0)
    step_until(session, advising(published))

    advice = latest(published)
    assert advice.recommendation["best"]["card_uid"] == "c4"
    assert advice.recommendation["warnings"] == ["NEW_RELIC is not modelled"]
    assert "unreliable" in advice.status_text

    bridge.entries.append(play(13, "c1", "JAW_WORM_0"))
    session.step()
    assert latest(published).last_grade["unreliable"] is True


# --- the panel's JSON and the run loop -------------------------------------------------


def test_the_coach_state_serializes_to_json_for_the_panel(make_session):
    def respond(live):
        grit = card(live, "c4", p_death=math.nan)  # no data behind the death rate
        return analysis_of([grit, end_turn(diff=4.0)], best=grit)

    bridge = FakeBridge()
    session, published = make_session(bridge, FakeEngine(respond))
    settle(session, bridge, combat(seq=12))
    step_until(session, advising(published))

    data = json.loads(latest(published).to_json())
    assert set(data) == {
        "status",
        "status_text",
        "recommendation",
        "last_grade",
        "mode",
        "versions",
        "run",
    }
    assert data["status"] == "advice"
    assert data["versions"] == {"ok": True, "problems": []}
    assert data["run"] == {"act": 1, "floor": 6}
    assert data["recommendation"]["p_death"] is None  # NaN never reaches the panel
    assert data["recommendation"]["best"]["card_uid"] == "c4"


def test_run_polls_until_stopped_and_joins_its_engine_thread(make_session):
    bridge = FakeBridge(combat(seq=12))
    session, published = make_session(
        bridge, FakeEngine(standard_analysis), CoachSettings(poll_interval_s=0.01)
    )
    deadline = time.monotonic() + 5

    session.run(lambda: advising(published)() or time.monotonic() > deadline)

    assert latest(published).status == "advice"
    assert not session._worker._thread.is_alive()


def test_close_waits_out_an_analysis_and_its_retry_and_reports_whether_it_ended(
    make_session, tmp_path, monkeypatch
):
    # Review minor 3: the engine thread may be in an analysis (2 x budget + 5 s at
    # most, ruling R1) that the client retries once after a restart.
    bridge = FakeBridge()
    session, _ = make_session(
        bridge, FakeEngine(standard_analysis), CoachSettings(time_budget_s=2.0)
    )
    waited = []
    real_close = session._worker.close

    def spy(timeout):
        waited.append(timeout)
        return real_close(timeout)

    monkeypatch.setattr(session._worker, "close", spy)
    assert session.close() is True
    assert waited == [2 * (2 * 2.0 + 5.0)]

    monkeypatch.setattr(session._worker, "close", lambda timeout: False)
    assert session.close() is False
    [record] = run_log(tmp_path, "error")
    assert "did not stop" in record["error"]


def test_run_survives_an_unexpected_error_and_shows_it_as_one_sentence(make_session):
    class FlakyBridge(FakeBridge):
        def __init__(self, state):
            super().__init__(state)
            self.failed = False

        def get_state(self):
            if not self.failed:
                self.failed = True
                raise RuntimeError("boom\nwith a second line")
            return super().get_state()

    bridge = FlakyBridge(combat(seq=12))
    session, published = make_session(
        bridge, FakeEngine(standard_analysis), CoachSettings(poll_interval_s=0.01)
    )
    deadline = time.monotonic() + 5

    session.run(lambda: advising(published)() or time.monotonic() > deadline)

    [crashed] = [state for state in published if "RuntimeError" in state.status_text]
    assert crashed.status == "advice_off"
    assert "boom with a second line" in crashed.status_text
    assert latest(published).status == "advice"
