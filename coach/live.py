"""The live coach loop: watch the game, analyze each combat decision, grade each move.

`CoachSession.step()` is one poll. It reads the mod's state, lets the `Stabilizer`
decide when a position has settled, and classifies it. A settled combat decision (a
combat turn, or an in-combat selection: a hand selection, a card grid or a
choose-a-card screen) gets a live snapshot and an engine analysis on the session's one
background thread, and the panel is told "thinking", then "advice". The same poll
reads the mod's action log: each in-combat move a human made is graded against the
analysis of the position it was made from, and once the next position settles, the
move is fidelity-checked (`engine.check`) in the background, with any mismatch written
to `<data_dir>/gaps/<ts>.json`. States, moves, analyses, grades and checks all go to
the run log.

Matching a move to its position. An action-log entry names no position, only a seq,
and every state the mod reports carries `action_log_seq`, the newest seq at that
moment. So each settled position is kept, with its live snapshot and its analysis, in
a small bounded timeline together with the seq it was first seen at. The move with seq
N was made from the last position seen at seq N - 1 (in the entry's act, floor and
round); its "after" for the fidelity check is the very next settled position, if that
one is still at seq N. A move whose position never settled on screen, or had no
analysis, is Ungraded (Review Focus 1). Only human, in-combat moves are graded
(Review Focus 3, ruling R4). A gap in the log (the game restarted, or entries were
dropped) empties the timeline, so nothing is graded across it (ruling R5).

In-combat selections are not logged as moves. A selection is graded when the next
position settles at the same seq, by diffing the two with `grader.grade_selection`.

Stale analyses. Every engine call runs on one worker thread, the newest position's
analysis first (spec 7.1: when the player acts before a search finishes, "the coach
starts on the new position"). An older position's analysis still runs, only to grade
the move made from it, and never reaches the panel (Review Focus 1).

Threading. Worker results are applied on the session's own thread, in `step()` and in
`run()`'s waits between polls, so only that thread ever touches the session's state;
`set_mode` just records the mode and asks that thread to refresh the panel.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from coach import grader
from coach.bridge.client import BridgeError, ModClient
from coach.bridge.decisions import (
    Decision,
    DecisionKind,
    Stabilizer,
    classify,
    fingerprint,
    is_settled,
)
from coach.bridge.versions import VersionStatus, check_versions, read_game_version
from coach.engine import (
    Analysis,
    CheckResult,
    EngineClient,
    EngineError,
    EngineUnavailable,
    Move,
    MoveStat,
)
from coach.runlog import RunLog
from coach.snapshot import SnapshotError, build_live_snapshot

MODES = ("show", "quiz")

_COMBAT_DECISIONS = frozenset({DecisionKind.COMBAT, DecisionKind.COMBAT_SELECT})
# Ruling R4: only these in-combat kinds are graded. An entry whose round is null was
# made outside combat (selling a potion to the merchant, say) and is only logged.
_GRADABLE_ENTRY_KINDS = frozenset({"play_card", "use_potion", "end_turn"})
_ENTRY_MOVE_KINDS = {"play_card": "card", "use_potion": "potion", "end_turn": "end_turn"}
# A position's analysis is still on its way: the moves made from it wait for it.
_IN_FLIGHT = frozenset({"pending", "restarting"})

_SEEN_MAX = 64  # settled positions kept for matching moves to where they were made
_SOURCES_MAX = 256  # recent action-log seqs whose source (human/api) is remembered
_ALTERNATIVES_MAX = 3
_FIDELITY_WINDOW_MOVES = 10  # spec 6.3: a mismatch keeps its warning up for 10 moves
_DEATH_VALUE_HP = -100.0  # spec 7.2 (the search's DEATH_VALUE): what a death is worth
_WAITING_POLL_S = 1.0  # retry interval while the game is unreachable, as capture uses
_WORKER_JOIN_S = 5.0

_MOVE_FIELDS = tuple(f.name for f in dataclasses.fields(Move))
_MOVE_IDENTITY_FIELDS = tuple(name for name in _MOVE_FIELDS if name != "label")
_UNIDENTIFIED_SELECTION = Move(
    kind="select",
    card_index=None,
    card_uid=None,
    card_id=None,
    potion_slot=None,
    potion_id=None,
    target=None,
    select_index=None,
    label="Card selection",
)


# --- settings and the published state ------------------------------------------------


@dataclass
class CoachSettings:
    """The session's settings. The session reads them on every poll, so a caller may
    flip `override_versions` while it runs (the panel's settings do)."""

    mode: str = "show"  # "show" | "quiz"; change it while running with set_mode()
    override_versions: bool = False  # advise even when the versions don't match
    time_budget_s: float = 1.5  # the engine's search budget per position
    poll_interval_s: float = 0.2
    thresholds: grader.Thresholds = grader.DEFAULT_THRESHOLDS
    # What the engine's value counts per potion still held (spec 7.2: 10 HP by
    # default); must match the engine's own setting to read HP loss off its values.
    potion_value_hp: float = 10.0


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

    def to_dict(self) -> dict:
        return _json_safe(dataclasses.asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), allow_nan=False)


# --- what the session keeps ----------------------------------------------------------


@dataclass(eq=False)
class _Position:
    """One settled position and what the coach made of it.

    `status`: "none" (not a combat decision), "new", "blocked" (versions don't match),
    "unreadable" (no live snapshot), "pending" (analysis queued or running),
    "restarting" (analysis retried after the engine went down), "done", "failed".
    """

    fingerprint: str
    decision: Decision
    state: dict
    room_type: str | None
    status: str = "none"
    live: dict | None = None
    live_warnings: list[str] = field(default_factory=list)
    analysis: Analysis | None = None
    error: str | None = None
    retried: bool = False
    revealed: bool = False  # a move from it was graded: quiz mode may show its advice


@dataclass(eq=False)
class _Seen:
    """A settled position as it was seen, with the action log's newest seq at that
    moment (the seq of the first poll that showed it)."""

    seq: int | None
    position: _Position


@dataclass(eq=False)
class _PendingGrade:
    """A move waiting for the analysis of the position it was made from. `entry` is
    its action-log entry; an in-combat selection has none, but has `post`, the next
    settled position (what `grader.grade_selection` diffs against)."""

    pre: _Seen | None
    entry: dict | None
    post: _Seen | None = None


@dataclass(eq=False)
class _PendingCheck:
    """A graded move waiting for the settled position after it."""

    pre: _Seen
    post_seq: int | None  # the seq that position must still be at
    move: Move
    move_number: int  # which graded move this was, for the warning window


@dataclass(eq=False)
class _Job:
    kind: str  # "analysis" | "check"
    run: Callable[[], object]
    context: object


_REFRESH = object()  # a message asking the session thread to republish
_GONE = object()  # a position that fell out of the timeline


# --- the engine worker ---------------------------------------------------------------


class _Worker:
    """The one background thread every engine call runs on.

    The newest position's analysis always goes next, so the panel catches up with the
    game first. Everything else, including an older position's analysis that a grade
    still needs, runs in submission order after it. Each job's outcome, its result or
    the exception it raised, is handed to `deliver`.
    """

    def __init__(self, deliver: Callable[[_Job, object], None]) -> None:
        self._deliver = deliver
        self._cond = threading.Condition()
        self._jobs: deque[_Job] = deque()
        self._urgent: _Job | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name="coach-engine", daemon=True)
        self._thread.start()

    def submit(self, job: _Job, *, urgent: bool = False) -> None:
        with self._cond:
            self._jobs.append(job)
            if urgent:
                self._urgent = job
            self._cond.notify()

    def close(self, timeout: float) -> None:
        with self._cond:
            self._closed = True
            self._jobs.clear()
            self._urgent = None
            self._cond.notify_all()
        self._thread.join(timeout)

    def _take(self) -> _Job | None:
        with self._cond:
            while not self._jobs and not self._closed:
                self._cond.wait()
            if self._closed:
                return None
            job = self._urgent if self._urgent is not None else self._jobs[0]
            self._jobs.remove(job)
            if job is self._urgent:
                self._urgent = None
            return job

    def _loop(self) -> None:
        while (job := self._take()) is not None:
            try:
                outcome = job.run()
            except Exception as exc:  # delivered, not raised: the session decides
                outcome = exc
            self._deliver(job, outcome)


# --- the session ---------------------------------------------------------------------


class CoachSession:
    """The live loop: bridge -> Stabilizer -> snapshot -> engine -> panel, and the
    action log -> grade -> fidelity check."""

    def __init__(
        self,
        client: ModClient,
        engine: EngineClient,
        *,
        publish: Callable[[CoachState], None],
        data_dir: Path,
        settings: CoachSettings,
    ) -> None:
        if settings.mode not in MODES:
            raise ValueError(f"mode must be 'show' or 'quiz', got {settings.mode!r}")
        self._client = client
        self._engine = engine
        self._publish = publish
        self._data_dir = Path(data_dir)
        self._settings = settings
        self._runlog = RunLog(self._data_dir)
        self._results: queue.SimpleQueue = queue.SimpleQueue()
        self._worker = _Worker(lambda job, outcome: self._results.put((job, outcome)))
        self._stabilizer = Stabilizer()
        self._streak_fingerprint: str | None = None
        self._streak_seq: int | None = None

        self._mode = settings.mode
        self._connected = False
        self._bridge_problem: str | None = None
        self._internal_error: str | None = None
        self._game_version: str | None = None
        self._versions = VersionStatus(ok=True, problems=[])
        self._run: dict = {"act": None, "floor": None}
        self._room_type: str | None = None

        self._last_seq: int | None = None
        self._seen: deque[_Seen] = deque(maxlen=_SEEN_MAX)
        self._current: _Position | None = None
        self._last_analyzed: _Position | None = None
        self._pending_grades: deque[_PendingGrade] = deque()
        self._pending_checks: list[_PendingCheck] = []
        self._sources: dict[int, str | None] = {}
        self._moves_graded = 0
        self._notes: list[tuple[int, str]] = []  # (move number, fidelity warning)
        self._last_grade: dict | None = None
        self._published: CoachState | None = None

    # --- public API ------------------------------------------------------------------

    def step(self) -> None:
        """One poll: the state, the action log, and any engine results that came in."""
        self._apply_results()
        try:
            state = self._client.get_state()
            self._on_state(state)
            self._poll_actions(state)
        except BridgeError as exc:
            self._connected = False
            self._bridge_problem = _one_line(exc)
        self._retry_blocked()
        self._apply_results()
        self._refresh()

    def run(self, stop: Callable[[], bool]) -> None:
        """Poll until `stop()` says otherwise, then stop the engine thread."""
        try:
            while not stop():
                self._internal_error = None
                try:
                    self.step()
                    delay = self._settings.poll_interval_s if self._connected else _WAITING_POLL_S
                    self._wait(delay)
                except Exception as exc:  # one bad poll must never stop the coach
                    self._on_internal_error(exc)
                    time.sleep(self._settings.poll_interval_s)
        finally:
            self.close()

    def set_mode(self, mode: str) -> None:
        """Switch between "show" and "quiz". Safe to call from any thread."""
        if mode not in MODES:
            raise ValueError(f"mode must be 'show' or 'quiz', got {mode!r}")
        self._mode = mode
        self._results.put(_REFRESH)

    def close(self) -> None:
        """Stop the engine thread (the engine and the bridge belong to the caller)."""
        self._worker.close(_WORKER_JOIN_S)

    # --- polling ---------------------------------------------------------------------

    def _on_state(self, state: dict) -> None:
        self._connected = True
        self._bridge_problem = None
        # The seq a position is kept with is the one of the first poll that showed it:
        # a move clicked between that poll and the one that settles it is not yet on
        # screen, but already in the log.
        key = fingerprint(state) if is_settled(state) else None
        if key != self._streak_fingerprint:
            self._streak_fingerprint = key
            self._streak_seq = _int_or_none(state.get("action_log_seq"))
        settled = self._stabilizer.feed(state)
        if settled is not None:
            self._on_settled(settled, self._streak_seq)

    def _on_settled(self, state: dict, seq: int | None) -> None:
        decision = classify(state)
        self._game_version = read_game_version()
        self._versions = check_versions(self._game_version, state)
        run = state.get("run") if isinstance(state.get("run"), dict) else {}
        self._run = {"act": run.get("act"), "floor": run.get("floor")}
        key = fingerprint(state)
        self._runlog.append(
            "state",
            {
                "fingerprint": key,
                "seq": seq,
                "state": state,
                "decision": {
                    "kind": decision.kind.value,
                    "state_type": decision.state_type,
                    "room_type": decision.room_type,
                },
                "versions": {"ok": self._versions.ok, "problems": self._versions.problems},
            },
        )
        # A selection's own state_type is not a room type: it takes the last fight's.
        if decision.kind is DecisionKind.COMBAT:
            self._room_type = decision.room_type
        elif decision.kind is DecisionKind.NON_COMBAT:
            self._room_type = None

        position = self._position_for(key, state, decision)
        previous = self._seen[-1] if self._seen else None
        seen = _Seen(seq, position)
        self._seen.append(seen)
        if decision.kind in _COMBAT_DECISIONS:
            self._current = position
            self._maybe_analyze(position)
        else:
            self._current = None
        if previous is not None:
            self._queue_selection_grade(previous, seen)
        self._process_grades()
        self._refresh()

    def _position_for(self, key: str, state: dict, decision: Decision) -> _Position:
        last = self._last_analyzed
        if last is not None and last.fingerprint == key:
            return last  # only a position whose fingerprint differs is analyzed again
        room_type = self._room_type if decision.kind is DecisionKind.COMBAT_SELECT else None
        status = "new" if decision.kind in _COMBAT_DECISIONS else "none"
        return _Position(key, decision, state, room_type, status=status)

    def _poll_actions(self, state: dict) -> None:
        if self._last_seq is None:
            # Moves made before the session started have no analysis to be graded on.
            self._last_seq = _int_or_none(state.get("action_log_seq"))
            if self._last_seq is None:
                baseline = self._client.actions_since(0).get("latest_seq")
                self._last_seq = _int_or_none(baseline) or 0
        since = self._last_seq
        response = self._client.actions_since(since)
        latest = _int_or_none(response.get("latest_seq"))
        entries = [
            entry
            for entry in response.get("entries") or []
            if isinstance(entry, dict) and (_int_or_none(entry.get("seq")) or 0) > since
        ]
        newest = max([since, latest or 0, *(entry["seq"] for entry in entries)])
        if (latest is not None and latest < since) or (entries and entries[0]["seq"] > since + 1):
            self._on_log_gap(since, latest, entries)
            return
        for entry in entries:
            self._on_entry(entry)
        self._last_seq = newest

    def _on_log_gap(self, since: int, latest: int | None, entries: list[dict]) -> None:
        """Ruling R5: the game restarted (its log is behind ours) or entries were
        dropped. Positions from before the gap cannot be matched to moves after it:
        they are dropped, and nothing across the gap is graded."""
        for entry in entries:
            self._runlog.append("action", entry)
        self._runlog.append(
            "log_gap",
            {
                "since": since,
                "latest_seq": latest,
                "first_seq": entries[0]["seq"] if entries else None,
            },
        )
        base = latest if latest is not None else since
        self._last_seq = max([base, *(entry["seq"] for entry in entries)])
        self._seen.clear()
        self._pending_grades.clear()
        self._pending_checks.clear()
        self._sources.clear()

    def _on_entry(self, entry: dict) -> None:
        self._runlog.append("action", entry)
        source = entry.get("source")
        self._sources[entry["seq"]] = source
        while len(self._sources) > _SOURCES_MAX:
            del self._sources[next(iter(self._sources))]
        if source != "human":
            return  # Review Focus 3: a move the api made is never graded
        if entry.get("kind") not in _GRADABLE_ENTRY_KINDS or entry.get("round") is None:
            return  # ruling R4: only in-combat moves are graded
        self._pending_grades.append(_PendingGrade(pre=self._find_pre(entry), entry=entry))
        self._process_grades()

    def _find_pre(self, entry: dict) -> _Seen | None:
        """The position this move was made from: the last one seen at the seq before it."""
        wanted = entry["seq"] - 1
        for seen in reversed(self._seen):
            if seen.seq == wanted:
                return seen if _made_from(entry, seen.position) else None
        return None

    def _retry_blocked(self) -> None:
        """A position blocked by the version check is analyzed once the override is on."""
        position = self._current
        if position is not None and position.status == "blocked" and self._advice_allowed():
            self._maybe_analyze(position)

    # --- analysis --------------------------------------------------------------------

    def _advice_allowed(self) -> bool:
        return self._versions.ok or self._settings.override_versions

    def _maybe_analyze(self, position: _Position) -> None:
        if position.status not in ("new", "blocked"):
            return
        if not self._advice_allowed():
            position.status = "blocked"
            return
        if position.live is None and not self._build_live(position):
            return
        position.status = "pending"
        self._last_analyzed = position
        self._submit_analysis(position)

    def _build_live(self, position: _Position) -> bool:
        warnings: list[str] = []
        try:
            position.live = build_live_snapshot(
                position.state,
                game_version=self._game_version or "unknown",
                room_type=position.room_type,
                warnings=warnings,
            )
        except SnapshotError as exc:
            position.status = "unreadable"
            position.error = _one_line(exc)
            self._runlog.append(
                "analysis",
                {"fingerprint": position.fingerprint, "error": f"snapshot: {position.error}"},
            )
            return False
        position.live_warnings = warnings
        return True

    def _submit_analysis(self, position: _Position) -> None:
        live, budget = position.live, self._settings.time_budget_s
        job = _Job("analysis", lambda: self._engine.analyze(live, time_budget_s=budget), position)
        self._worker.submit(job, urgent=True)

    def _on_analysis(self, position: _Position, outcome: object) -> None:
        current = position is self._current
        record = {"fingerprint": position.fingerprint, "current": current, "live": position.live}
        if isinstance(outcome, Analysis):
            position.analysis = outcome
            position.status = "done"
            record["snapshot_warnings"] = position.live_warnings
            record["analysis"] = dataclasses.asdict(outcome)
        else:
            message = _one_line(outcome)
            record["error"] = message
            if isinstance(outcome, EngineUnavailable) and current and not position.retried:
                # The client already restarted the service once; start it afresh and
                # try this position one more time while the panel says so.
                position.retried = True
                position.status = "restarting"
                position.error = message
                self._submit_analysis(position)
            elif isinstance(outcome, EngineUnavailable):
                position.status = "failed"
                position.error = f"the engine service is unavailable ({message})"
            elif isinstance(outcome, EngineError):
                position.status = "failed"
                position.error = f"the engine could not analyze it ({message})"
            else:
                position.status = "failed"
                position.error = (
                    f"the engine's reply could not be read ({type(outcome).__name__}: {message})"
                )
        self._runlog.append("analysis", record)
        self._process_grades()

    # --- grading ---------------------------------------------------------------------

    def _queue_selection_grade(self, previous: _Seen, seen: _Seen) -> None:
        """An in-combat selection was made if the next settled position follows it
        with no move in between."""
        if (
            previous.position.decision.kind is not DecisionKind.COMBAT_SELECT
            or seen.position is previous.position  # the same screen again: no pick yet
            or seen.seq != previous.seq  # a move came in between: the diff would mix them
            or self._sources.get(previous.seq) == "api"  # the api is playing, not the player
        ):
            return
        self._pending_grades.append(_PendingGrade(pre=previous, entry=None, post=seen))

    def _process_grades(self) -> None:
        """Grade every waiting move whose position's analysis has resolved, in order."""
        while self._pending_grades:
            item = self._pending_grades[0]
            if item.pre is not None and item.pre.position.status in _IN_FLIGHT:
                break  # the moves after it wait too, so grades reach the panel in order
            self._pending_grades.popleft()
            if item.entry is not None:
                self._grade_move(item)
            else:
                self._grade_selection(item)
        self._resolve_checks()

    def _grade_move(self, item: _PendingGrade) -> None:
        entry = item.entry
        position = item.pre.position if item.pre is not None else None
        analysis = position.analysis if position is not None else None
        stat = None
        if analysis is not None:
            stat = grader.identify_move(entry, position.live, analysis)
        if stat is not None:
            graded = _grade_dict(grader.grade(stat, analysis, self._settings.thresholds))
        else:
            played = _move_from_entry(entry, position.state if position is not None else None)
            graded = _ungraded_dict(played, analysis)
        number = self._record_grade(graded, position, seq=entry["seq"], entry=entry)
        if stat is not None:
            self._pending_checks.append(
                _PendingCheck(item.pre, entry["seq"], _plain_move(stat), number)
            )

    def _grade_selection(self, item: _PendingGrade) -> None:
        position, post = item.pre.position, item.post.position
        analysis = position.analysis
        found = None
        if analysis is not None and post.live is not None:
            found = grader.grade_selection(position.live, post.live, analysis)
        if found is not None:
            # grade_selection grades on the default thresholds: regrade its pick on
            # the configured ones.
            graded = _grade_dict(grader.grade(found.played, analysis, self._settings.thresholds))
        else:
            graded = _ungraded_dict(_UNIDENTIFIED_SELECTION, analysis)
        number = self._record_grade(graded, position, seq=item.pre.seq, entry=None)
        if found is not None:
            self._pending_checks.append(
                _PendingCheck(item.pre, item.pre.seq, _plain_move(found.played), number)
            )

    def _record_grade(
        self, graded: dict, position: _Position | None, *, seq: int | None, entry: dict | None
    ) -> int:
        self._moves_graded += 1
        self._last_grade = graded
        if position is not None:
            position.revealed = True
        self._runlog.append(
            "grade",
            {
                "fingerprint": position.fingerprint if position is not None else None,
                "seq": seq,
                "entry": entry,
                "grade": graded,
            },
        )
        self._refresh()  # every grade reaches the panel, even when several land at once
        return self._moves_graded

    # --- fidelity checks -------------------------------------------------------------

    def _resolve_checks(self) -> None:
        waiting = []
        for check in self._pending_checks:
            after = self._seen_after(check.pre)
            if after is None:
                waiting.append(check)  # the position after the move has not settled yet
            elif after is _GONE or after.seq != check.post_seq or after.position.live is None:
                # Another move came first, the fight ended, or the timeline moved on:
                # there is no fair "after" to compare this move's outcome with.
                self._runlog.append(
                    "check",
                    {
                        "pre_fingerprint": check.pre.position.fingerprint,
                        "move": dataclasses.asdict(check.move),
                        "skipped": "no settled combat position right after the move",
                    },
                )
            else:
                self._submit_check(check, after)
        self._pending_checks = waiting

    def _seen_after(self, seen: _Seen) -> _Seen | object | None:
        """The settled position right after `seen`: None if none yet, _GONE if `seen`
        fell out of the timeline."""
        for index, item in enumerate(self._seen):
            if item is seen:
                return self._seen[index + 1] if index + 1 < len(self._seen) else None
        return _GONE

    def _submit_check(self, check: _PendingCheck, after: _Seen) -> None:
        pre, post, move = check.pre.position.live, after.position.live, check.move
        self._worker.submit(
            _Job("check", lambda: self._engine.check(pre, move, post), (check, after))
        )

    def _on_check(self, context: tuple[_PendingCheck, _Seen], outcome: object) -> None:
        check, after = context
        record = {
            "pre_fingerprint": check.pre.position.fingerprint,
            "post_fingerprint": after.position.fingerprint,
            "move": dataclasses.asdict(check.move),
        }
        if isinstance(outcome, CheckResult):
            record["result"] = dataclasses.asdict(outcome)
            if outcome.deterministic_mismatches or outcome.implausible:
                gap = self._write_gap(check, after, outcome)
                record["gap"] = str(gap)
                self._notes.append(
                    (check.move_number, _mismatch_sentence(check.move, outcome, gap))
                )
            self._notes += [(check.move_number, text) for text in outcome.warnings]
        else:
            record["error"] = _one_line(outcome)
            self._notes.append(
                (
                    check.move_number,
                    _sentence(
                        f"The fidelity check after {check.move.label} failed: {record['error']}"
                    ),
                )
            )
        self._runlog.append("check", record)

    def _write_gap(self, check: _PendingCheck, after: _Seen, result: CheckResult) -> Path:
        gaps = self._data_dir / "gaps"
        gaps.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = gaps / f"{stamp}.json"
        suffix = 1
        while path.exists():
            path = gaps / f"{stamp}-{suffix}.json"
            suffix += 1
        report = {
            "pre": check.pre.position.live,
            "post": after.position.live,
            "move": dataclasses.asdict(check.move),
            "check": dataclasses.asdict(result),
        }
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return path

    # --- engine results --------------------------------------------------------------

    def _apply_results(self) -> None:
        while True:
            try:
                message = self._results.get_nowait()
            except queue.Empty:
                return
            self._apply(message)

    def _wait(self, delay: float) -> None:
        """Wait out the poll interval, applying engine results the moment they land."""
        deadline = time.monotonic() + delay
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                message = self._results.get(timeout=remaining)
            except queue.Empty:
                return
            self._apply(message)

    def _apply(self, message: object) -> None:
        """Apply one worker result, and show what it changed before the next one."""
        if message is not _REFRESH:
            job, outcome = message
            if job.kind == "analysis":
                self._on_analysis(job.context, outcome)
            else:
                self._on_check(job.context, outcome)
        self._refresh()

    def _on_internal_error(self, exc: Exception) -> None:
        self._internal_error = f"{type(exc).__name__}: {_one_line(exc)}"
        with contextlib.suppress(Exception):
            self._runlog.append("error", {"error": self._internal_error})
        with contextlib.suppress(Exception):
            self._refresh()

    # --- the panel -------------------------------------------------------------------

    def _refresh(self) -> None:
        state = self._compose()
        if state != self._published:
            self._published = state
            self._publish(state)

    def _compose(self) -> CoachState:
        def shown(status: str, text: str, recommendation: dict | None = None) -> CoachState:
            return CoachState(
                status=status,
                status_text=text,
                recommendation=recommendation,
                last_grade=self._last_grade,
                mode=self._mode,
                versions=self._versions,
                run=dict(self._run),
            )

        if self._internal_error is not None:
            return shown(
                "advice_off",
                _sentence(
                    f"Advice is off: the coach hit an internal error ({self._internal_error})"
                ),
            )
        if not self._connected:
            if self._bridge_problem:
                return shown(
                    "waiting_for_game", _sentence(f"Waiting for the game: {self._bridge_problem}")
                )
            return shown("waiting_for_game", "Waiting for the game.")
        position = self._current
        if position is None:
            return shown("idle", "Connected: waiting for a combat decision.")
        if not self._advice_allowed():
            problems = _sentence("Advice is off: " + "; ".join(self._versions.problems))
            return shown(
                "advice_off",
                f"{problems} Turn on the version override in the settings to see advice anyway.",
            )
        if position.status == "unreadable":
            return shown(
                "advice_off",
                _sentence(
                    f"Advice is off for this position: the coach could not read it "
                    f"({position.error})"
                ),
            )
        if position.status == "failed":
            return shown(
                "advice_off", _sentence(f"Advice is off for this position: {position.error}")
            )
        if position.status == "restarting":
            return shown(
                "engine_restarting",
                _sentence(f"The engine service stopped ({position.error}); restarting it"),
            )
        if position.status != "done":
            return shown("thinking", "Analyzing this position…")
        recommendation = self._recommendation(position)
        return shown("advice", self._advice_text(position, recommendation), recommendation)

    def _advice_text(self, position: _Position, recommendation: dict) -> str:
        if recommendation["best"] is None:
            return "Quiz: make your move, then see its grade and the best move."
        if not position.analysis.reliable:
            return "Advice ready, but unreliable: see the warnings."
        return "Advice ready."

    def _recommendation(self, position: _Position) -> dict:
        analysis = position.analysis
        hidden = self._mode == "quiz" and not position.revealed
        best_stat = next((s for s in analysis.moves if _same_move(s, analysis.best)), None)
        return {
            "best": None if hidden else _move_dict(analysis.best),
            "line": None if hidden else [_move_dict(move) for move in analysis.line],
            "alternatives": None if hidden else self._alternatives(analysis),
            "expected_hp_loss": self._expected_hp_loss(position.live, best_stat),
            "p_death": _finite(best_stat.p_death) if best_stat is not None else None,
            "reliable": analysis.reliable,
            "warnings": self._warnings(position),
        }

    def _alternatives(self, analysis: Analysis) -> list[dict]:
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
            lower = None
            if diff is not None and stderr is not None:
                lower = diff - self._settings.thresholds.z * stderr
            alternatives.append(
                {
                    "move": _move_dict(stat),
                    "cost_mean_hp": diff,
                    "cost_lower_hp": lower,
                    "p_death_delta": _finite(stat.paired_death_diff_vs_best),
                }
            )
        return alternatives

    def _expected_hp_loss(self, live: dict, best_stat: MoveStat | None) -> float | None:
        """HP the best line is expected to lose if the fight is won.

        The engine's value is end HP plus `potion_value_hp` per potion still held, or
        -100 for a death (spec 7.2). Taking the deaths out of the mean leaves the value
        of a win; the loss is what is held now (HP plus potions, valued the same way)
        minus that. A potion the line drinks counts as its value in HP.
        """
        if best_stat is None:
            return None
        value, p_death = _finite(best_stat.value_mean), _finite(best_stat.p_death)
        if value is None or p_death is None or p_death >= 1.0:
            return None
        player = live["player"]
        held = sum(1 for slot in player["potions"] if slot["id"] is not None)
        now = player["hp"] + self._settings.potion_value_hp * held
        won = (value - p_death * _DEATH_VALUE_HP) / (1.0 - p_death)
        return max(0.0, now - won)

    def _warnings(self, position: _Position) -> list[str]:
        """The snapshot builder's warnings with the engine's, then the coach's own."""
        analysis = position.analysis
        texts = [*position.live_warnings, *analysis.warnings]
        if analysis.select_mapping_failures:
            texts.append(
                f"{analysis.select_mapping_failures} selection option(s) could not be matched "
                "to the cards on screen and are missing from the advice."
            )
        if not self._versions.ok:  # advice with a version problem means the override is on
            texts.append(
                _sentence("The version check is overridden: " + "; ".join(self._versions.problems))
            )
        texts += self._fidelity_warnings()
        return list(dict.fromkeys(_one_line(text) for text in texts))

    def _fidelity_warnings(self) -> list[str]:
        """The fidelity warnings of the last 10 graded moves (spec 6.3); older ones
        are dropped for good."""
        self._notes = [
            (number, text)
            for number, text in self._notes
            if self._moves_graded - number < _FIDELITY_WINDOW_MOVES
        ]
        return [text for _, text in self._notes]


# --- helpers -------------------------------------------------------------------------


def _made_from(entry: dict, position: _Position) -> bool:
    """Whether this action-log entry can have been made from this position: a move is
    made from a combat turn, in the same act, floor and round the entry reports."""
    if position.decision.kind is not DecisionKind.COMBAT:
        return False
    run = position.state.get("run") or {}
    battle = position.state.get("battle") or {}
    return (entry.get("act"), entry.get("floor"), entry.get("round")) == (
        run.get("act"),
        run.get("floor"),
        battle.get("round"),
    )


def _move_from_entry(entry: dict, state: dict | None) -> Move:
    """The Move an action-log entry describes, labelled with the card, potion and enemy
    names from the state it was made in when that is known, else with the entry's ids."""
    state = state or {}
    player = state.get("player") or {}
    enemies = (state.get("battle") or {}).get("enemies") or []
    kind = _ENTRY_MOVE_KINDS[entry["kind"]]
    target = entry.get("target")
    target_name = next(
        (enemy.get("name") for enemy in enemies if enemy.get("entity_id") == target), None
    )
    card_index = card_uid = card_id = slot = potion_id = None
    if kind == "card":
        card_uid, card_id = entry.get("card_uid"), entry.get("card_id")
        in_hand = next((c for c in player.get("hand") or [] if c.get("uid") == card_uid), {})
        card_index = in_hand.get("index")
        name = in_hand.get("name") or card_id or "a card"
    elif kind == "potion":
        slot = entry.get("slot")
        held = next((p for p in player.get("potions") or [] if p.get("slot") == slot), {})
        potion_id = held.get("id")
        name = held.get("name") or potion_id or f"the potion in slot {slot}"
    else:
        name = "End turn"
    aimed_at = target_name or target
    return Move(
        kind=kind,
        card_index=card_index,
        card_uid=card_uid,
        card_id=card_id,
        potion_slot=slot,
        potion_id=potion_id,
        target=target,
        select_index=None,
        label=f"{name} → {aimed_at}" if aimed_at else name,
    )


def _plain_move(move: Move) -> Move:
    """Only a Move's own fields (a MoveStat is a Move plus its statistics)."""
    return Move(**{name: getattr(move, name) for name in _MOVE_FIELDS})


def _move_dict(move: Move) -> dict:
    return dataclasses.asdict(_plain_move(move))


def _same_move(a: Move, b: Move) -> bool:
    return all(getattr(a, name) == getattr(b, name) for name in _MOVE_IDENTITY_FIELDS)


def _grade_dict(grade: grader.Grade) -> dict:
    graded = dataclasses.asdict(grade)
    graded["best"] = _move_dict(grade.best)
    graded["played"] = _move_dict(grade.played)
    return _json_safe(graded)


def _ungraded_dict(played: Move, analysis: Analysis | None) -> dict:
    """The Grade shape for a move with no analysis to grade it on, or one the analysis
    does not have."""
    return {
        "label": "Ungraded",
        "cost_lower_hp": None,
        "cost_mean_hp": None,
        "death_risk_delta_lower": None,
        "best": _move_dict(analysis.best) if analysis is not None else None,
        "played": _move_dict(played),
        "unreliable": analysis is not None and not analysis.reliable,
    }


def _mismatch_sentence(move: Move, result: CheckResult, gap: Path) -> str:
    findings = [
        f"{m.get('field')} predicted {m.get('predicted')}, the game showed {m.get('actual')}"
        for m in result.deterministic_mismatches
    ] + [
        f"{m.get('field')} came out {m.get('actual')}, which the simulator cannot produce"
        for m in result.implausible
    ]
    more = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
    return _one_line(
        f"Fidelity mismatch after {move.label}: {findings[0]}{more}; gap report {gap.name}."
    )


def _one_line(value: object) -> str:
    """An exception or message as one line of text: no newline, never a stack trace."""
    text = " ".join(str(value).split())
    if not text and isinstance(value, BaseException):
        return type(value).__name__
    return text


def _sentence(text: str) -> str:
    text = _one_line(text)
    return text if text.endswith((".", "!", "?", "…")) else f"{text}."


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
