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
the run log. What the panel shows is built by coach/view.py; the background thread is
coach/worker.py.

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
import queue
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
from coach.engine import Analysis, CheckResult, EngineClient, EngineError, EngineUnavailable, Move
from coach.runlog import RunLog
from coach.snapshot import SnapshotError, build_live_snapshot
from coach.view import (
    CoachState,
    compose,
    grade_dict,
    mismatch_sentence,
    one_line,
    plain_move,
    sentence,
    ungraded_dict,
)
from coach.worker import Job, Worker

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
_FIDELITY_WINDOW_MOVES = 10  # spec 6.3: a mismatch keeps its warning up for 10 moves
_WAITING_POLL_S = 1.0  # retry interval while the game is unreachable, as capture uses
_WORKER_JOIN_S = 5.0

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


_REFRESH = object()  # a message asking the session thread to republish
_GONE = object()  # a position that fell out of the timeline


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
        self._worker = Worker(
            lambda job, outcome: self._results.put((job, outcome)), name="coach-engine"
        )
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
            self._bridge_problem = one_line(exc)
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
            position.error = one_line(exc)
            self._runlog.append(
                "analysis",
                {"fingerprint": position.fingerprint, "error": f"snapshot: {position.error}"},
            )
            return False
        position.live_warnings = warnings
        return True

    def _submit_analysis(self, position: _Position) -> None:
        live, budget = position.live, self._settings.time_budget_s
        job = Job(
            lambda: self._engine.analyze(live, time_budget_s=budget),
            context=("analysis", position),
        )
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
            message = one_line(outcome)
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
            graded = grade_dict(grader.grade(stat, analysis, self._settings.thresholds))
        else:
            played = _move_from_entry(entry, position.state if position is not None else None)
            graded = ungraded_dict(played, analysis)
        number = self._record_grade(graded, position, seq=entry["seq"], entry=entry)
        if stat is not None:
            self._pending_checks.append(
                _PendingCheck(item.pre, entry["seq"], plain_move(stat), number)
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
            graded = grade_dict(grader.grade(found.played, analysis, self._settings.thresholds))
        else:
            graded = ungraded_dict(_UNIDENTIFIED_SELECTION, analysis)
        number = self._record_grade(graded, position, seq=item.pre.seq, entry=None)
        if found is not None:
            self._pending_checks.append(
                _PendingCheck(item.pre, item.pre.seq, plain_move(found.played), number)
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
            Job(lambda: self._engine.check(pre, move, post), context=("check", check, after))
        )

    def _on_check(self, check: _PendingCheck, after: _Seen, outcome: object) -> None:
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
                self._notes.append((check.move_number, mismatch_sentence(check.move, outcome, gap)))
            self._notes += [(check.move_number, text) for text in outcome.warnings]
        else:
            record["error"] = one_line(outcome)
            self._notes.append(
                (
                    check.move_number,
                    sentence(
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

    def _fidelity_warnings(self) -> list[str]:
        """The fidelity warnings of the last 10 graded moves (spec 6.3); older ones
        are dropped for good."""
        self._notes = [
            (number, text)
            for number, text in self._notes
            if self._moves_graded - number < _FIDELITY_WINDOW_MOVES
        ]
        return [text for _, text in self._notes]

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
            kind, *context = job.context
            if kind == "analysis":
                self._on_analysis(*context, outcome)
            else:
                self._on_check(*context, outcome)
        self._refresh()

    def _on_internal_error(self, exc: Exception) -> None:
        self._internal_error = f"{type(exc).__name__}: {one_line(exc)}"
        with contextlib.suppress(Exception):
            self._runlog.append("error", {"error": self._internal_error})
        with contextlib.suppress(Exception):
            self._refresh()

    # --- the panel -------------------------------------------------------------------

    def _refresh(self) -> None:
        state = compose(
            self._current,
            connected=self._connected,
            bridge_problem=self._bridge_problem,
            internal_error=self._internal_error,
            versions=self._versions,
            override_versions=self._settings.override_versions,
            mode=self._mode,
            last_grade=self._last_grade,
            run=self._run,
            fidelity_warnings=self._fidelity_warnings(),
            z=self._settings.thresholds.z,
            potion_value_hp=self._settings.potion_value_hp,
        )
        if state != self._published:
            self._published = state
            self._publish(state)


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


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
