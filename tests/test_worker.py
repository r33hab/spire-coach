"""Tests for the one-thread job runner (coach/worker.py)."""

from __future__ import annotations

import threading
import time

from coach.worker import SKIPPED, Job, Worker


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for the worker"
        time.sleep(0.002)


def make_worker():
    delivered = []
    worker = Worker(lambda job, outcome: delivered.append((job.context, outcome)), name="t")
    return worker, delivered


def blocker(gate: threading.Event, started: threading.Event):
    def run():
        started.set()
        assert gate.wait(5), "the test never released the blocking job"
        return "blocker"

    return run


def test_jobs_run_one_at_a_time_in_order_and_every_outcome_is_delivered():
    worker, delivered = make_worker()
    try:
        worker.submit(Job(lambda: "first", context="first"))
        worker.submit(Job(lambda: 1 / 0, context="second"))
        worker.submit(Job(lambda: "third", context="third"))
        wait_until(lambda: len(delivered) == 3)
    finally:
        assert worker.close(5)

    assert [context for context, _ in delivered] == ["first", "second", "third"]
    assert delivered[0][1] == "first"
    assert isinstance(delivered[1][1], ZeroDivisionError)  # delivered, not raised


def test_the_latest_urgent_job_runs_next_and_an_earlier_urgent_one_falls_back_in_line():
    gate, started = threading.Event(), threading.Event()
    worker, delivered = make_worker()
    try:
        worker.submit(Job(blocker(gate, started), context="running"))
        assert started.wait(5)
        worker.submit(Job(lambda: None, context="a"))
        worker.submit(Job(lambda: None, context="b"), urgent=True)
        worker.submit(Job(lambda: None, context="c"), urgent=True)
        gate.set()
        wait_until(lambda: len(delivered) == 4)
    finally:
        assert worker.close(5)

    assert [context for context, _ in delivered] == ["running", "c", "a", "b"]


def test_close_drops_queued_jobs_and_reports_whether_the_thread_finished():
    gate, started = threading.Event(), threading.Event()
    worker, delivered = make_worker()
    worker.submit(Job(blocker(gate, started), context="running"))
    assert started.wait(5)
    worker.submit(Job(lambda: "never", context="queued"))

    assert worker.close(0.05) is False  # the running job is still blocked
    gate.set()
    assert worker.close(5) is True

    assert [context for context, _ in delivered] == ["running"]


def test_a_job_nobody_wants_by_its_turn_is_skipped_instead_of_run():
    gate, started = threading.Event(), threading.Event()
    worker, delivered = make_worker()
    ran = []
    still_wanted = {"stale": True}
    try:
        worker.submit(Job(blocker(gate, started), context="running"))
        assert started.wait(5)
        worker.submit(
            Job(lambda: ran.append("stale"), context="stale", wanted=lambda: still_wanted["stale"])
        )
        worker.submit(Job(lambda: ran.append("needed"), context="needed", wanted=lambda: True))
        still_wanted["stale"] = False  # asked when its turn comes, not when submitted
        gate.set()
        wait_until(lambda: len(delivered) == 3)
    finally:
        assert worker.close(5)

    assert ran == ["needed"]
    assert delivered[1] == ("stale", SKIPPED)
