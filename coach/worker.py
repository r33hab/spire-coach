"""One background thread that runs jobs one at a time, the urgent one first.

The live coach runs every engine call here (coach/live.py): the analysis of the
position on screen is submitted as urgent and goes next, and everything else (an older
position's analysis that a grade still needs, a fidelity check) runs after it, in
submission order. Each job's outcome, its result or the exception it raised, is handed
to `deliver` on the worker thread; what it means is the caller's business.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(eq=False)
class Job:
    run: Callable[[], object]
    context: object = None  # the caller's own record of what the job is for


class Worker:
    """One background thread; the most recent urgent job always runs next."""

    def __init__(self, deliver: Callable[[Job, object], None], *, name: str = "worker") -> None:
        self._deliver = deliver
        self._cond = threading.Condition()
        self._jobs: deque[Job] = deque()
        self._urgent: Job | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def submit(self, job: Job, *, urgent: bool = False) -> None:
        """Queue `job`. An urgent one goes ahead of every other queued job; an earlier
        urgent job that has not started yet falls back to its place in line."""
        with self._cond:
            self._jobs.append(job)
            if urgent:
                self._urgent = job
            self._cond.notify()

    def close(self, timeout: float) -> bool:
        """Drop the queued jobs, let the running one finish, and wait up to `timeout`
        seconds for the thread to end. True if it did."""
        with self._cond:
            self._closed = True
            self._jobs.clear()
            self._urgent = None
            self._cond.notify_all()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _take(self) -> Job | None:
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
            except Exception as exc:  # delivered, not raised: the caller decides
                outcome = exc
            self._deliver(job, outcome)
