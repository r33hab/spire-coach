#!/usr/bin/env python3
"""Fake engine service for tests.

Speaks the coach<->engine JSON-lines protocol
(docs/superpowers/plans/2026-09-24-sim-engine-service.md) with canned,
shape-complete responses, so tests can exercise EngineClient without a real
simulator or network. Run as: python fake_engine_service.py <control-file>.

Behavior is driven by a small JSON "control file" (argv[1]), re-read on
every request so a test can steer a running instance, or steer what a
*restarted* instance will do:

  {"ok_false": {"type": "...", "message": "..."}}
      Every request gets this ok: false error instead of a canned result.
  {"hang": true}
      Every request is accepted but never answered (simulates a hang).
  {"crash_once": true}
      The first request this process instance receives crashes the process
      (no response) and rewrites the control file with crash_once cleared.
      A freshly restarted instance then reads the updated file and answers
      normally -- this is how a test proves a crash-then-restart retry
      actually recovers, since the marker survives the restart even though
      no process memory does.
  {"crash_always": true}
      Every request, in every process instance, crashes the process (no
      response). Unlike crash_once this is never cleared, so it simulates a
      service that stays broken across a restart.
  {"log_requests_to": "<path>"}
      Every request ({id, method, params}) is appended as one JSON line to
      <path>, so a test can check the ids and params a client actually
      sent.
  {"reply_with_wrong_id": true}
      Every response carries an id that does not match its request, so a
      test can prove the client detects a desynced pipe instead of quietly
      pairing a response with the wrong request.
  {"null_stats": true}
      analyze's move statistics carry null for value_mean and p_death, as the
      real service sends a statistic with no data behind it (NaN, inf).

A missing or unreadable control file means no special behavior: every
request gets its canned result. Every request also logs one line to
stderr, so tests can confirm stderr never reaches stdout (it would corrupt
the protocol stream) and does reach the coach's own log file.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def _read_control(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_control(path: Path, control: dict) -> None:
    path.write_text(json.dumps(control), encoding="utf-8")


def _canned_result(method: str, control: dict) -> dict | None:
    move = {
        "kind": "card",
        "card_index": 0,
        "card_uid": "c1",
        "card_id": "Strike",
        "potion_slot": None,
        "potion_id": None,
        "target": "e1",
        "select_index": None,
        "label": "Strike → Jaw Worm",
    }
    move_stat = {
        **move,
        "visits": 100,
        "value_mean": 0.5,
        "p_death": 0.01,
        "paired_diff_vs_best": 0.0,
        "paired_diff_stderr": 0.1,
        "paired_n": 50,
        "paired_death_diff_vs_best": 0.0,
        "paired_death_diff_stderr": 0.01,
        "hand_indices": [0],
        "potion_slots": [],
        "select_indices": [],
    }
    if control.get("null_stats"):
        move_stat.update(value_mean=None, p_death=None)
    results = {
        "health": {
            "service_version": "test-1",
            "native_api_version": 1,
            "sim_commit": "deadbeef",
            "game_version_target": "v0.107.1",
            "workers": 4,
            "strategy_model": None,
            "combat_prior": None,
        },
        "analyze": {
            "reliable": True,
            "warnings": [],
            "best": move,
            "line": [move],
            "line_truncated_at_draw": False,
            "moves": [move_stat],
            "sims": 1000,
            "elapsed_s": 0.5,
            "budget_exhausted": False,
            "select_mapping_failures": 0,
        },
        "check": {
            "comparable": True,
            "deterministic_mismatches": [],
            "random_fields": [],
            "implausible": [],
            "warnings": [],
        },
        "strategy": {
            "options": [{"option": "aggro", "value": 1.0}],
            "model": {"generation": 1},
        },
        "shutdown": {},
    }
    return results.get(method)


def main() -> None:
    control_path = Path(sys.argv[1])

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        request = json.loads(raw_line)
        req_id = request["id"]
        method = request["method"]
        params = request.get("params") or {}

        print(f"fake-engine: handling {method} id={req_id}", file=sys.stderr, flush=True)

        control = _read_control(control_path)

        log_requests_to = control.get("log_requests_to")
        if log_requests_to:
            with open(log_requests_to, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": req_id, "method": method, "params": params}) + "\n")

        if control.get("crash_always"):
            sys.exit(1)

        if control.get("crash_once"):
            _write_control(control_path, {**control, "crash_once": False})
            sys.exit(1)

        if control.get("hang"):
            time.sleep(3600)
            continue

        ok_false = control.get("ok_false")
        if ok_false is not None:
            response = {"id": req_id, "ok": False, "error": ok_false}
        else:
            result = _canned_result(method, control)
            if result is None:
                response = {
                    "id": req_id,
                    "ok": False,
                    "error": {"type": "unknown_method", "message": f"unknown method {method!r}"},
                }
            else:
                response = {"id": req_id, "ok": True, "result": result}

        if control.get("reply_with_wrong_id"):
            response["id"] = req_id + 999

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
