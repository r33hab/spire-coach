import dataclasses
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from coach.engine import (
    Analysis,
    CheckResult,
    EngineClient,
    EngineError,
    EngineUnavailable,
    Move,
    MoveStat,
    StrategyResult,
)

FAKE_SERVICE = Path(__file__).parent / "fakes" / "fake_engine_service.py"

A_MOVE = Move(
    kind="card",
    card_index=0,
    card_uid="c1",
    card_id="Strike",
    potion_slot=None,
    potion_id=None,
    target="e1",
    select_index=None,
    label="Strike → Jaw Worm",
)


def make_client(tmp_path, control=None, **kwargs):
    """An EngineClient wired to the fake service script.

    `control` is written up front as the fake's control file (see
    fake_engine_service.py for the scenarios it supports).
    """
    control_path = tmp_path / "control.json"
    if control is not None:
        control_path.write_text(json.dumps(control), encoding="utf-8")
    cmd = [sys.executable, str(FAKE_SERVICE), str(control_path)]
    return EngineClient(cmd=cmd, **kwargs)


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- construction / default cmd ---------------------------------------------


def test_default_cmd_targets_the_sts2_service_with_the_worker_count():
    client = EngineClient(workers=6)
    assert client._cmd == [
        "uv",
        "run",
        "--project",
        str(Path.home() / "Documents/git/sts2"),
        "python",
        "-m",
        "sts2_gym.live.service",
        "--workers",
        "6",
    ]


def test_default_cmd_workers_defaults_to_four():
    client = EngineClient()
    assert client._cmd[-1] == "4"


def test_the_default_launch_runs_in_the_sts2_repo_with_its_sources_on_pythonpath(monkeypatch):
    # The simulator's package is not installed in its own env, so the service only
    # imports when launched from the repo with src/ and rl/ on PYTHONPATH.
    monkeypatch.setenv("SPIRE_COACH_TEST_VAR", "kept")
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    sts2_dir = Path.home() / "Documents/git/sts2"

    client = EngineClient()

    assert client._cwd == sts2_dir
    assert client._env["PYTHONPATH"] == f"{sts2_dir}/src{os.pathsep}{sts2_dir}/rl"
    assert client._env["SPIRE_COACH_TEST_VAR"] == "kept"  # the rest of the env is kept
    assert client._env["PATH"] == os.environ["PATH"]


def test_a_custom_command_inherits_the_coachs_cwd_and_env():
    client = EngineClient(cmd=["python", "-c", "pass"])
    assert client._cwd is None
    assert client._env is None


def test_the_service_is_spawned_with_the_launch_cwd_and_env(monkeypatch):
    spawned = {}

    def recording_popen(cmd, **kwargs):
        spawned.update(kwargs, cmd=cmd)
        raise OSError("recorded, not started")

    monkeypatch.setattr("coach.engine.subprocess.Popen", recording_popen)
    client = EngineClient()

    with pytest.raises(EngineUnavailable, match="recorded, not started"):
        client.start()

    assert spawned["cmd"] == client._cmd
    assert spawned["cwd"] == client._cwd
    assert spawned["env"] == client._env


# --- dataclasses are frozen and mirror the protocol shapes -------------------


def test_move_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        A_MOVE.label = "changed"


def test_move_stat_is_frozen_and_carries_every_paired_stat():
    stat = MoveStat(
        kind="card",
        card_index=0,
        card_uid="c1",
        card_id="Strike",
        potion_slot=None,
        potion_id=None,
        target="e1",
        select_index=None,
        label="Strike",
        visits=10,
        value_mean=0.1,
        p_death=0.0,
        paired_diff_vs_best=0.0,
        paired_diff_stderr=0.1,
        paired_n=30,
        paired_death_diff_vs_best=0.0,
        paired_death_diff_stderr=0.02,
        hand_indices=[0, 2],
        potion_slots=(),
        select_indices=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        stat.visits = 20
    assert stat.hand_indices == [0, 2]


# --- round trip for every method ---------------------------------------------


def test_health_round_trips_to_a_plain_dict(tmp_path):
    client = make_client(tmp_path)
    try:
        result = client.health()
    finally:
        client.close()
    assert result == {
        "service_version": "test-1",
        "native_api_version": 1,
        "sim_commit": "deadbeef",
        "game_version_target": "v0.107.1",
        "workers": 4,
        "strategy_model": None,
        "combat_prior": None,
    }


def test_analyze_round_trips_to_an_analysis_of_moves(tmp_path):
    client = make_client(tmp_path)
    try:
        result = client.analyze({"some": "live-snapshot"})
    finally:
        client.close()

    assert isinstance(result, Analysis)
    assert result.reliable is True
    assert result.warnings == []
    assert isinstance(result.best, Move)
    assert result.best.card_id == "Strike"
    assert result.best.target == "e1"
    assert result.line == [result.best]
    assert result.line_truncated_at_draw is False
    assert result.sims == 1000
    assert result.elapsed_s == 0.5
    assert result.budget_exhausted is False
    assert result.select_mapping_failures == 0
    assert len(result.moves) == 1
    assert isinstance(result.moves[0], MoveStat)
    assert result.moves[0].visits == 100
    assert result.moves[0].paired_n == 50
    assert result.moves[0].hand_indices == [0]
    assert result.moves[0].potion_slots == ()
    assert result.moves[0].select_indices == ()


def test_analyze_reads_a_null_value_mean_and_p_death_as_none(tmp_path):
    # The service sends a statistic with no data behind it (NaN, inf) as null.
    client = make_client(tmp_path, control={"null_stats": True})
    try:
        result = client.analyze({"some": "live-snapshot"})
    finally:
        client.close()

    assert result.moves[0].value_mean is None
    assert result.moves[0].p_death is None


def test_analyze_passes_time_budget_seed_and_the_r2_search_params_through(tmp_path):
    log_path = tmp_path / "requests.jsonl"
    client = make_client(tmp_path, control={"log_requests_to": str(log_path)})
    try:
        client.analyze(
            {"x": 1}, time_budget_s=2.0, seed=7, min_child_sims=300, confirm_pairs=80
        )
    finally:
        client.close()

    assert read_jsonl(log_path) == [
        {
            "id": 1,
            "method": "analyze",
            "params": {
                "live": {"x": 1},
                "time_budget_s": 2.0,
                "seed": 7,
                "min_child_sims": 300,
                "confirm_pairs": 80,
            },
        }
    ]


def test_analyze_defaults_match_r2(tmp_path):
    log_path = tmp_path / "requests.jsonl"
    client = make_client(tmp_path, control={"log_requests_to": str(log_path)})
    try:
        client.analyze({"x": 1})
    finally:
        client.close()

    assert read_jsonl(log_path)[0]["params"] == {
        "live": {"x": 1},
        "time_budget_s": 1.5,
        "seed": 0,
        "min_child_sims": 200,
        "confirm_pairs": 50,
    }


def test_check_does_not_add_a_resamples_param_the_brief_did_not_ask_for(tmp_path):
    log_path = tmp_path / "requests.jsonl"
    client = make_client(tmp_path, control={"log_requests_to": str(log_path)})
    try:
        client.check({"pre": True}, A_MOVE, {"post": True})
    finally:
        client.close()

    params = read_jsonl(log_path)[0]["params"]
    assert "resamples" not in params
    assert params["pre"] == {"pre": True}
    assert params["post"] == {"post": True}
    assert params["move"] == dataclasses.asdict(A_MOVE)


def test_check_round_trips_to_a_check_result(tmp_path):
    client = make_client(tmp_path)
    try:
        result = client.check({"pre": True}, A_MOVE, {"post": True})
    finally:
        client.close()
    assert result == CheckResult(
        comparable=True,
        deterministic_mismatches=[],
        random_fields=[],
        implausible=[],
        warnings=[],
    )


def test_strategy_round_trips_to_a_strategy_result(tmp_path):
    client = make_client(tmp_path)
    try:
        result = client.strategy({"live_run": True}, {"decision": True})
    finally:
        client.close()
    assert result == StrategyResult(
        options=[{"option": "aggro", "value": 1.0}],
        model={"generation": 1},
    )


# --- ok: false -> EngineError -------------------------------------------------


def test_ok_false_raises_engine_error_with_the_services_message(tmp_path):
    client = make_client(
        tmp_path,
        control={"ok_false": {"type": "conversion", "message": "unknown card id XYZ"}},
    )
    try:
        with pytest.raises(EngineError, match="unknown card id XYZ"):
            client.health()
    finally:
        client.close()


def test_ok_false_does_not_trigger_a_restart(tmp_path):
    log_path = tmp_path / "requests.jsonl"
    client = make_client(
        tmp_path,
        control={
            "ok_false": {"type": "conversion", "message": "bad snapshot"},
            "log_requests_to": str(log_path),
        },
    )
    try:
        with pytest.raises(EngineError):
            client.health()
    finally:
        client.close()
    # Exactly one request reached the service: no retry for an application
    # error the service was perfectly able to answer.
    assert len(read_jsonl(log_path)) == 1


# --- a crash mid-request triggers a restart and retry -------------------------


def test_a_crash_mid_request_is_retried_against_a_fresh_process(tmp_path):
    client = make_client(tmp_path, control={"crash_once": True})
    try:
        client.start()
        old_pid = client._proc.pid

        result = client.health()  # 1st attempt crashes; retry must succeed

        assert result["service_version"] == "test-1"
        assert client._proc is not None
        assert client._proc.pid != old_pid
        assert client._proc.poll() is None
    finally:
        client.close()


def test_a_process_that_died_between_requests_is_discarded_before_the_respawn(tmp_path):
    client = make_client(tmp_path)
    try:
        client.start()
        dead = client._proc
        dead_drain = client._stderr_thread
        dead.kill()
        dead.wait()

        result = client.health()  # finds the dead process and must respawn

        assert result["service_version"] == "test-1"
        assert client._proc is not dead
        # The dead process was discarded properly, not just dropped: its pipes are
        # closed and its stderr drain thread has been joined.
        assert dead.stdin.closed
        assert dead.stdout.closed
        assert dead.stderr.closed
        assert not dead_drain.is_alive()
    finally:
        client.close()


def test_on_restart_is_called_when_the_client_restarts_the_service_mid_request(tmp_path):
    restarts = []
    client = make_client(tmp_path, control={"crash_once": True})
    client.on_restart = lambda: restarts.append(client._proc.pid if client._proc else None)
    try:
        client.start()
        crashed_pid = client._proc.pid

        client.health()

        # Called once, as the restart begins: the crashed process was still attached.
        assert restarts == [crashed_pid]
    finally:
        client.close()


def test_on_restart_is_called_when_a_dead_service_is_replaced_between_requests(tmp_path):
    restarts = []
    client = make_client(tmp_path, on_restart=lambda: restarts.append("restart"))
    try:
        client.health()  # the first start is not a restart
        assert restarts == []
        client._proc.kill()
        client._proc.wait()

        client.health()

        assert restarts == ["restart"]
    finally:
        client.close()


def test_a_failing_on_restart_callback_does_not_break_the_request(tmp_path, monkeypatch):
    monkeypatch.setattr("coach.engine.DEFAULT_DATA_DIR", tmp_path)  # its traceback is logged

    def broken():
        raise RuntimeError("callback bug")

    client = make_client(tmp_path, control={"crash_once": True}, on_restart=broken)
    try:
        assert client.health()["service_version"] == "test-1"
    finally:
        client.close()


def test_two_crashes_in_a_row_raise_engine_unavailable(tmp_path):
    # crash_always is never cleared, so both the first attempt and the
    # post-restart retry hit a process that crashes immediately.
    client = make_client(tmp_path, control={"crash_always": True})
    try:
        with pytest.raises(EngineUnavailable):
            client.health()
        # The second, still-broken process is discarded rather than left
        # attached for the next call to trip over.
        assert client._proc is None
    finally:
        client.close()


# --- a hang times out ----------------------------------------------------------


def test_a_hang_times_out_and_raises_engine_unavailable(tmp_path, monkeypatch):
    spawned_pids = []
    original_spawn = EngineClient._spawn

    def recording_spawn(self):
        original_spawn(self)
        spawned_pids.append(self._proc.pid)

    monkeypatch.setattr(EngineClient, "_spawn", recording_spawn)
    client = make_client(tmp_path, control={"hang": True}, request_timeout_s=0.2)
    try:
        with pytest.raises(EngineUnavailable):
            client.health()
        # The wedged process is discarded, not left attached for next time.
        assert client._proc is None
    finally:
        client.close()
    # Both hung processes (the first attempt's and the restarted retry's) were
    # killed and reaped: neither pid exists any more.
    assert len(spawned_pids) == 2
    for pid in spawned_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_analyze_uses_the_r1_timeout_2x_budget_plus_5s(tmp_path, monkeypatch):
    # Spy on the real read (still executed) rather than actually waiting out
    # a multi-second timeout: this checks the value analyze() computes and
    # forwards, fast and precisely.
    captured = {}
    original_read_line = EngineClient._read_line

    def spy_read_line(self, timeout_s):
        captured["timeout_s"] = timeout_s
        return original_read_line(self, timeout_s)

    monkeypatch.setattr(EngineClient, "_read_line", spy_read_line)
    client = make_client(tmp_path)
    try:
        client.analyze({}, time_budget_s=1.5)
    finally:
        client.close()
    assert captured["timeout_s"] == pytest.approx(2 * 1.5 + 5.0)


def test_health_check_and_strategy_use_the_plain_request_timeout(tmp_path, monkeypatch):
    captured = {}
    original_read_line = EngineClient._read_line

    def spy_read_line(self, timeout_s):
        captured.setdefault("timeouts", []).append(timeout_s)
        return original_read_line(self, timeout_s)

    monkeypatch.setattr(EngineClient, "_read_line", spy_read_line)
    client = make_client(tmp_path, request_timeout_s=3.3)
    try:
        client.health()
        client.check({"pre": True}, A_MOVE, {"post": True})
        client.strategy({"live_run": True}, {"decision": True})
    finally:
        client.close()
    assert captured["timeouts"] == [3.3, 3.3, 3.3]


# --- R3: thread-safe, one lock serializes each request/response cycle ---------


def test_concurrent_callers_are_serialized_and_every_id_is_matched(tmp_path):
    log_path = tmp_path / "requests.jsonl"
    client = make_client(tmp_path, control={"log_requests_to": str(log_path)})
    errors = []

    def worker():
        try:
            client.health()
        except Exception as exc:  # any exception here should fail the test, not crash a thread
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
    finally:
        client.close()

    assert not errors
    assert all(not t.is_alive() for t in threads)
    ids = [entry["id"] for entry in read_jsonl(log_path)]
    # 8 fully serialized requests: no id is skipped, reused, or interleaved.
    assert sorted(ids) == list(range(1, 9))


# --- responses are matched by id ----------------------------------------------


def test_a_mismatched_response_id_is_treated_as_a_transport_failure(tmp_path):
    client = make_client(tmp_path, control={"reply_with_wrong_id": True})
    try:
        with pytest.raises(EngineUnavailable):
            client.health()
    finally:
        client.close()


def test_request_ids_increase_monotonically(tmp_path):
    log_path = tmp_path / "requests.jsonl"
    client = make_client(tmp_path, control={"log_requests_to": str(log_path)})
    try:
        client.health()
        client.health()
        client.health()
    finally:
        client.close()
    ids = [entry["id"] for entry in read_jsonl(log_path)]
    assert ids == [1, 2, 3]


# --- no_model -> None ----------------------------------------------------------


def test_strategy_returns_none_on_no_model(tmp_path):
    client = make_client(
        tmp_path,
        control={"ok_false": {"type": "no_model", "message": "no champion exported"}},
    )
    try:
        result = client.strategy({"live_run": True}, {"decision": True})
    finally:
        client.close()
    assert result is None


def test_strategy_still_raises_engine_error_for_other_error_types(tmp_path):
    client = make_client(
        tmp_path,
        control={"ok_false": {"type": "conversion", "message": "bad live_run"}},
    )
    try:
        with pytest.raises(EngineError, match="bad live_run"):
            client.strategy({"live_run": True}, {"decision": True})
    finally:
        client.close()


# --- close() leaves no child process -------------------------------------------


def test_close_leaves_no_child_process(tmp_path):
    client = make_client(tmp_path)
    client.health()
    pid = client._proc.pid

    client.close()

    assert client._proc is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_close_before_start_is_a_no_op(tmp_path):
    client = make_client(tmp_path)
    client.close()
    assert client._proc is None


def test_close_is_idempotent(tmp_path):
    client = make_client(tmp_path)
    client.health()
    client.close()
    client.close()


# --- stderr goes to the rotating log file, never to stdout ---------------------


def test_stderr_goes_to_the_log_file_and_never_corrupts_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr("coach.engine.DEFAULT_DATA_DIR", tmp_path)
    client = make_client(tmp_path)

    client.health()
    client.close()

    log_path = tmp_path / "logs" / "engine.log"
    assert log_path.is_file()
    assert "fake-engine: handling health" in log_path.read_text(encoding="utf-8")
