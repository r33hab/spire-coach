"""Client for the out-of-process engine service.

The engine (analysis, one-step fidelity checks, and later the strategy
model) runs in the simulator's own Python environment, out of process, so
its heavier dependencies never touch the coach's own env and a native crash
only takes down the child, not the coach
(docs/superpowers/plans/2026-09-24-sim-engine-service.md). The protocol is
JSON lines over stdin/stdout: a request is ``{id, method, params}``, a
response is ``{id, ok: true, result}`` or ``{id, ok: false, error: {type,
message}}``. stdout carries protocol lines only; the service's stderr is
drained to a rotating log file, never to the coach's own stdout.

EngineClient is thread-safe: one lock serializes each request/response
cycle end to end (the service itself handles one request at a time anyway),
so ids from concurrent callers are always allocated and matched in order.
On EOF, a crash, or a timeout, the client restarts the service once and
retries the same request once; a second failure raises EngineUnavailable.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from coach.config import DEFAULT_DATA_DIR

_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUP_COUNT = 3
_READ_CHUNK_BYTES = 65536
_STOP_WAIT_S = 1.0


# --- exceptions --------------------------------------------------------------


class EngineError(Exception):
    """The engine service ran the request and reported ok: false."""

    def __init__(self, message: str, *, error_type: str = "error") -> None:
        super().__init__(message)
        self.error_type = error_type


class EngineUnavailable(EngineError):
    """The engine service could not be reached, even after one restart."""


# --- protocol dataclasses ------------------------------------------------------


@dataclass(frozen=True)
class Move:
    kind: str
    card_index: int | None
    card_uid: str | None
    card_id: str | None
    potion_slot: int | None
    potion_id: str | None
    target: str | None
    select_index: int | None
    label: str


@dataclass(frozen=True)
class MoveStat(Move):
    visits: int
    value_mean: float
    p_death: float
    paired_diff_vs_best: float | None
    paired_diff_stderr: float | None
    paired_n: int
    paired_death_diff_vs_best: float | None
    paired_death_diff_stderr: float | None
    hand_indices: list[int]


@dataclass(frozen=True)
class Analysis:
    reliable: bool
    warnings: list[str]
    best: Move
    line: list[Move]
    line_truncated_at_draw: bool
    moves: list[MoveStat]
    sims: int
    elapsed_s: float
    budget_exhausted: bool
    select_mapping_failures: int


@dataclass(frozen=True)
class CheckResult:
    comparable: bool
    deterministic_mismatches: list[dict]
    random_fields: list[str]
    implausible: list[dict]
    warnings: list[str]


@dataclass(frozen=True)
class StrategyResult:
    options: list[dict]
    model: dict


def _move_kwargs(data: dict) -> dict:
    return dict(
        kind=data["kind"],
        card_index=data.get("card_index"),
        card_uid=data.get("card_uid"),
        card_id=data.get("card_id"),
        potion_slot=data.get("potion_slot"),
        potion_id=data.get("potion_id"),
        target=data.get("target"),
        select_index=data.get("select_index"),
        label=data["label"],
    )


def _move_from_dict(data: dict) -> Move:
    return Move(**_move_kwargs(data))


def _move_stat_from_dict(data: dict) -> MoveStat:
    return MoveStat(
        **_move_kwargs(data),
        visits=data["visits"],
        value_mean=data["value_mean"],
        p_death=data["p_death"],
        paired_diff_vs_best=data.get("paired_diff_vs_best"),
        paired_diff_stderr=data.get("paired_diff_stderr"),
        paired_n=data["paired_n"],
        paired_death_diff_vs_best=data.get("paired_death_diff_vs_best"),
        paired_death_diff_stderr=data.get("paired_death_diff_stderr"),
        hand_indices=list(data["hand_indices"]),
    )


def _analysis_from_dict(data: dict) -> Analysis:
    return Analysis(
        reliable=data["reliable"],
        warnings=list(data["warnings"]),
        best=_move_from_dict(data["best"]),
        line=[_move_from_dict(m) for m in data["line"]],
        line_truncated_at_draw=data["line_truncated_at_draw"],
        moves=[_move_stat_from_dict(m) for m in data["moves"]],
        sims=data["sims"],
        elapsed_s=data["elapsed_s"],
        budget_exhausted=data["budget_exhausted"],
        select_mapping_failures=data["select_mapping_failures"],
    )


def _check_result_from_dict(data: dict) -> CheckResult:
    return CheckResult(
        comparable=data["comparable"],
        deterministic_mismatches=list(data["deterministic_mismatches"]),
        random_fields=list(data["random_fields"]),
        implausible=list(data["implausible"]),
        warnings=list(data["warnings"]),
    )


def _strategy_result_from_dict(data: dict) -> StrategyResult:
    return StrategyResult(options=list(data["options"]), model=dict(data["model"]))


def _default_cmd(workers: int) -> list[str]:
    sts2_dir = Path.home() / "Documents/git/sts2"
    return [
        "uv",
        "run",
        "--project",
        str(sts2_dir),
        "python",
        "-m",
        "sts2_gym.live.service",
        "--workers",
        str(workers),
    ]


def _drain_stderr(pipe, logger: logging.Logger) -> None:
    """Copy every stderr line from the service into the engine log.

    Runs on its own thread for the life of the child process: the pipe
    would otherwise fill up and stall the service once its OS buffer is
    full, and this keeps the service's own logging off the coach's stdout
    entirely.
    """
    try:
        for raw in iter(pipe.readline, b""):
            text = raw.decode("utf-8", errors="replace").rstrip("\n")
            if text:
                logger.info(text)
    except (OSError, ValueError):
        pass  # the pipe was closed while we were reading; shutting down.


class EngineClient:
    """Talks to the out-of-process engine service over JSON lines."""

    def __init__(
        self,
        cmd: list[str] | None = None,
        *,
        workers: int = 4,
        request_timeout_s: float = 10.0,
    ) -> None:
        self._cmd = cmd if cmd is not None else _default_cmd(workers)
        self._request_timeout_s = request_timeout_s
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stdout_buf = bytearray()
        self._next_id = 1
        self._logger = logging.getLogger(f"coach.engine.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._log_handler: RotatingFileHandler | None = None

    # --- public API ------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            self._ensure_started()

    def health(self) -> dict:
        return self._request("health", {}, self._request_timeout_s)

    def analyze(
        self,
        live: dict,
        *,
        time_budget_s: float = 1.5,
        seed: int = 0,
        min_child_sims: int = 200,
        confirm_pairs: int = 50,
    ) -> Analysis:
        params = {
            "live": live,
            "time_budget_s": time_budget_s,
            "seed": seed,
            "min_child_sims": min_child_sims,
            "confirm_pairs": confirm_pairs,
        }
        timeout_s = 2 * time_budget_s + 5.0  # ruling R1
        result = self._request("analyze", params, timeout_s)
        return _analysis_from_dict(result)

    def check(self, pre: dict, move: Move, post: dict) -> CheckResult:
        params = {"pre": pre, "move": dataclasses.asdict(move), "post": post}
        result = self._request("check", params, self._request_timeout_s)
        return _check_result_from_dict(result)

    def strategy(self, live_run: dict, decision: dict) -> StrategyResult | None:
        params = {"live_run": live_run, "decision": decision}
        try:
            result = self._request("strategy", params, self._request_timeout_s)
        except EngineError as exc:
            if exc.error_type == "no_model":
                return None
            raise
        return _strategy_result_from_dict(result)

    def close(self) -> None:
        with self._lock:
            self._discard_proc()
            if self._log_handler is not None:
                self._logger.removeHandler(self._log_handler)
                self._log_handler.close()
                self._log_handler = None

    # --- request/response --------------------------------------------------

    def _request(self, method: str, params: dict, timeout_s: float) -> dict:
        with self._lock:
            try:
                return self._request_once(method, params, timeout_s)
            except EngineUnavailable as exc:
                self._logger.warning("restarting the engine service after: %s", exc)
                self._restart()
                try:
                    return self._request_once(method, params, timeout_s)
                except EngineUnavailable as exc2:
                    # Don't leave a known-broken process attached: the next
                    # call should spawn fresh rather than repeat this.
                    self._discard_proc()
                    raise EngineUnavailable(
                        f"engine service unavailable after a restart: {exc2}"
                    ) from exc2

    def _request_once(self, method: str, params: dict, timeout_s: float) -> dict:
        self._ensure_started()
        req_id = self._next_id
        self._next_id += 1
        self._write_line(json.dumps({"id": req_id, "method": method, "params": params}))
        raw = self._read_line(timeout_s)
        try:
            response = json.loads(raw)
        except ValueError as exc:
            raise EngineUnavailable(f"the engine service sent invalid JSON: {raw!r}") from exc
        if response.get("id") != req_id:
            raise EngineUnavailable(
                f"the engine service returned response id {response.get('id')!r}, "
                f"expected {req_id!r}"
            )
        if response.get("ok"):
            return response.get("result") or {}
        error = response.get("error") or {}
        error_type = error.get("type") or "error"
        message = error.get("message") or "no message"
        raise EngineError(f"engine returned {error_type}: {message}", error_type=error_type)

    def _write_line(self, line: str) -> None:
        try:
            self._proc.stdin.write(line.encode("utf-8") + b"\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise EngineUnavailable(f"could not write to the engine service: {exc}") from exc

    def _read_line(self, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        fd = self._proc.stdout.fileno()
        while b"\n" not in self._stdout_buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineUnavailable(
                    f"the engine service did not respond within {timeout_s:.1f}s"
                )
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(fd, _READ_CHUNK_BYTES)
            except OSError as exc:
                raise EngineUnavailable(f"could not read from the engine service: {exc}") from exc
            if chunk == b"":
                raise EngineUnavailable("the engine service closed its output")
            self._stdout_buf.extend(chunk)
        line, _, rest = self._stdout_buf.partition(b"\n")
        self._stdout_buf = bytearray(rest)
        return bytes(line)

    # --- process lifecycle ---------------------------------------------------

    def _ensure_log_handler(self) -> None:
        if self._log_handler is not None:
            return
        log_path = DEFAULT_DATA_DIR / "logs" / "engine.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self._logger.addHandler(handler)
        self._log_handler = handler

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is not None:
            # It died between requests: reap it, close its pipes and join its
            # stderr drain thread before a fresh one replaces it.
            self._discard_proc()
        if self._proc is None:
            self._spawn()

    def _spawn(self) -> None:
        self._ensure_log_handler()
        try:
            proc = subprocess.Popen(
                self._cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise EngineUnavailable(f"could not start the engine service: {exc}") from exc
        self._proc = proc
        self._stdout_buf = bytearray()
        thread = threading.Thread(
            target=_drain_stderr, args=(proc.stderr, self._logger), daemon=True
        )
        thread.start()
        self._stderr_thread = thread

    def _restart(self) -> None:
        self._discard_proc()
        self._spawn()

    def _discard_proc(self) -> None:
        proc, self._proc = self._proc, None
        thread, self._stderr_thread = self._stderr_thread, None
        if proc is None:
            return
        self._stop_proc(proc)
        if thread is not None:
            thread.join(timeout=_STOP_WAIT_S)

    @staticmethod
    def _stop_proc(proc: subprocess.Popen) -> None:
        """Ask nicely (close stdin, its EOF stops the service cleanly), then
        escalate to terminate/kill. Always waits, so the child is reaped
        and no zombie or orphan is left behind.
        """
        if proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=_STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=_STOP_WAIT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=_STOP_WAIT_S)
        else:
            proc.wait()  # already exited; reap it
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
