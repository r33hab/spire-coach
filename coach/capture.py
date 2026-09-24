"""Capture mode: poll the mod, record every settled state and player action.

Runs until `stop()` says otherwise. A lost connection to the game/mod never
crashes the loop: it prints one "waiting" message, retries once a second,
and prints one "reconnected" message when the mod answers again. A rejected
token is different: it can't recover on its own, so the loop stops and
reports it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from coach.bridge.client import BridgeAuthError, BridgeUnavailable, ModClient
from coach.bridge.decisions import Stabilizer, classify
from coach.bridge.versions import check_versions, read_game_version
from coach.runlog import RunLog

_WAITING_MESSAGE = "waiting for game…"
_RECONNECTED_MESSAGE = "game connected"


@dataclass(frozen=True)
class CaptureStats:
    records: int
    actions: int
    errors: int


def capture_loop(
    client: ModClient,
    log: RunLog,
    *,
    poll_interval_s: float,
    stop: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> CaptureStats:
    stabilizer = Stabilizer()
    records = actions = errors = 0
    last_seq = 0
    waiting_for_game = False

    while not stop():
        try:
            state = client.get_state()
            stabilized = stabilizer.feed(state)
            if stabilized is not None:
                decision = classify(stabilized)
                versions = check_versions(read_game_version(), stabilized)
                log.append(
                    "state",
                    {
                        "state": stabilized,
                        "decision": {
                            "kind": decision.kind.value,
                            "state_type": decision.state_type,
                            "room_type": decision.room_type,
                        },
                        "versions": {"ok": versions.ok, "problems": versions.problems},
                    },
                )
                records += 1

            result = client.actions_since(last_seq)
            last_seq = result.get("latest_seq", last_seq)
            for entry in result.get("entries", []):
                log.append("action", entry)
                actions += 1
        except BridgeAuthError as exc:
            out(str(exc))
            errors += 1
            return CaptureStats(records=records, actions=actions, errors=errors)
        except BridgeUnavailable:
            if not waiting_for_game:
                out(_WAITING_MESSAGE)
                waiting_for_game = True
            sleep(1)
            continue

        if waiting_for_game:
            out(_RECONNECTED_MESSAGE)
            waiting_for_game = False

        sleep(poll_interval_s)

    return CaptureStats(records=records, actions=actions, errors=errors)
