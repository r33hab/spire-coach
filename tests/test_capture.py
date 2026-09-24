import copy
import json
from pathlib import Path

import pytest

from coach.bridge.client import BridgeAuthError, BridgeUnavailable
from coach.capture import CaptureStats, capture_loop
from coach.runlog import RunLog

FIXTURES = Path(__file__).parent / "fixtures" / "mod"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def settled_state():
    return load("state_monster_player_turn.json")


@pytest.fixture
def resolving_state(settled_state):
    state = copy.deepcopy(settled_state)
    state["is_resolving"] = True
    return state


class FakeClient:
    """Replays a scripted sequence of get_state() results, one per call.

    Each scripted item is either a state dict (returned) or an exception
    instance (raised). actions_since() is scripted independently, keyed by
    the `since` value it's called with, defaulting to an empty response.
    """

    def __init__(self, get_state_script, actions_script=None):
        self._get_state_script = list(get_state_script)
        self._actions_script = actions_script or {}
        self.get_state_calls = 0
        self.actions_since_calls = []

    def get_state(self):
        self.get_state_calls += 1
        item = self._get_state_script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def actions_since(self, since):
        self.actions_since_calls.append(since)
        return self._actions_script.get(since, {"status": "ok", "latest_seq": since, "entries": []})


def make_log(tmp_path):
    return RunLog(tmp_path)


def read_records(log):
    if not log.path.exists():
        return []
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(autouse=True)
def fixed_game_version(monkeypatch):
    # Every test gets a matching game version unless it overrides this itself;
    # capture_loop must never touch the real filesystem for it.
    monkeypatch.setattr("coach.capture.read_game_version", lambda: "v0.107.1")


def never_stop():
    return False


# --- unsettled states are never logged ---------------------------------------


def test_unsettled_state_is_not_logged(resolving_state, tmp_path):
    client = FakeClient([resolving_state, BridgeAuthError("token rejected: x")])
    log = make_log(tmp_path)

    stats = capture_loop(client, log, poll_interval_s=0, stop=never_stop, sleep=lambda s: None)

    assert stats == CaptureStats(records=0, actions=0, errors=1)
    assert read_records(log) == []


# --- settling: two identical settled polls emit exactly one record ----------


def test_settled_state_is_logged_once_after_two_identical_polls(settled_state, tmp_path):
    client = FakeClient(
        [
            copy.deepcopy(settled_state),
            copy.deepcopy(settled_state),
            BridgeAuthError("token rejected: x"),
        ]
    )
    log = make_log(tmp_path)

    stats = capture_loop(client, log, poll_interval_s=0, stop=never_stop, sleep=lambda s: None)

    assert stats == CaptureStats(records=1, actions=0, errors=1)
    records = read_records(log)
    assert len(records) == 1
    assert records[0]["kind"] == "state"
    assert records[0]["payload"]["state"] == settled_state


def test_state_record_carries_the_decision(settled_state, tmp_path):
    client = FakeClient(
        [
            copy.deepcopy(settled_state),
            copy.deepcopy(settled_state),
            BridgeAuthError("token rejected: x"),
        ]
    )
    log = make_log(tmp_path)

    capture_loop(client, log, poll_interval_s=0, stop=never_stop, sleep=lambda s: None)

    decision = read_records(log)[0]["payload"]["decision"]
    assert decision == {"kind": "combat", "state_type": "monster", "room_type": "monster"}


def test_state_record_carries_the_version_status_when_versions_match(settled_state, tmp_path):
    client = FakeClient(
        [
            copy.deepcopy(settled_state),
            copy.deepcopy(settled_state),
            BridgeAuthError("token rejected: x"),
        ]
    )
    log = make_log(tmp_path)

    capture_loop(client, log, poll_interval_s=0, stop=never_stop, sleep=lambda s: None)

    versions = read_records(log)[0]["payload"]["versions"]
    assert versions == {"ok": True, "problems": []}


def test_state_record_stamps_a_version_mismatch_but_still_records(
    settled_state, tmp_path, monkeypatch
):
    monkeypatch.setattr("coach.capture.read_game_version", lambda: "v0.999.0")
    client = FakeClient(
        [
            copy.deepcopy(settled_state),
            copy.deepcopy(settled_state),
            BridgeAuthError("token rejected: x"),
        ]
    )
    log = make_log(tmp_path)

    stats = capture_loop(client, log, poll_interval_s=0, stop=never_stop, sleep=lambda s: None)

    assert stats.records == 1
    versions = read_records(log)[0]["payload"]["versions"]
    assert versions["ok"] is False
    assert any("v0.999.0" in problem for problem in versions["problems"])


# --- actions_since --------------------------------------------------------------


def test_new_action_entries_are_logged_as_action_records(settled_state, tmp_path):
    entry_1 = {"seq": 1, "kind": "play_card", "source": "human"}
    entry_2 = {"seq": 2, "kind": "end_turn", "source": "human"}
    client = FakeClient(
        [copy.deepcopy(settled_state), BridgeAuthError("token rejected: x")],
        actions_script={0: {"status": "ok", "latest_seq": 2, "entries": [entry_1, entry_2]}},
    )
    log = make_log(tmp_path)

    stats = capture_loop(client, log, poll_interval_s=0, stop=never_stop, sleep=lambda s: None)

    assert stats.actions == 2
    action_records = [r for r in read_records(log) if r["kind"] == "action"]
    assert [r["payload"] for r in action_records] == [entry_1, entry_2]


def test_actions_since_is_called_with_the_latest_seq_seen_so_far(settled_state, tmp_path):
    client = FakeClient(
        [copy.deepcopy(settled_state), copy.deepcopy(settled_state), BridgeAuthError("x")],
        actions_script={
            0: {"status": "ok", "latest_seq": 5, "entries": [{"seq": 5, "kind": "end_turn"}]},
            5: {"status": "ok", "latest_seq": 5, "entries": []},
        },
    )
    log = make_log(tmp_path)

    capture_loop(client, log, poll_interval_s=0, stop=never_stop, sleep=lambda s: None)

    assert client.actions_since_calls == [0, 5]


# --- BridgeUnavailable: waits, retries, reconnects ---------------------------


def test_bridge_unavailable_prints_waiting_once_then_reconnects(settled_state, tmp_path):
    client = FakeClient(
        [
            BridgeUnavailable("could not reach the mod"),
            BridgeUnavailable("could not reach the mod"),
            copy.deepcopy(settled_state),
            BridgeAuthError("token rejected: x"),
        ]
    )
    log = make_log(tmp_path)
    messages = []
    sleeps = []

    capture_loop(
        client,
        log,
        poll_interval_s=0,
        stop=never_stop,
        sleep=sleeps.append,
        out=messages.append,
    )

    assert messages == ["waiting for game…", "game connected", "token rejected: x"]
    # Two outage retries, each a 1s sleep; no poll_interval_s sleep between them.
    assert sleeps.count(1) == 2


def test_bridge_unavailable_never_raises_out_of_the_loop(resolving_state, tmp_path):
    client = FakeClient(
        [BridgeUnavailable("gone"), BridgeUnavailable("gone"), BridgeAuthError("x")]
    )
    log = make_log(tmp_path)

    # Must not raise: capture never crashes on a lost connection.
    stats = capture_loop(client, log, poll_interval_s=0, stop=never_stop, sleep=lambda s: None)
    assert stats.errors == 1


# --- BridgeAuthError: stop immediately, count an error -----------------------


def test_auth_error_prints_the_message_and_stops_with_an_error_counted(tmp_path):
    client = FakeClient([BridgeAuthError("token rejected: reinstall the mod")])
    log = make_log(tmp_path)
    messages = []

    stats = capture_loop(
        client, log, poll_interval_s=0, stop=never_stop, sleep=lambda s: None, out=messages.append
    )

    assert stats == CaptureStats(records=0, actions=0, errors=1)
    assert messages == ["token rejected: reinstall the mod"]
    assert read_records(log) == []


# --- stop() ends the loop without touching the client ------------------------


def test_stop_true_from_the_start_never_polls_the_client(tmp_path):
    client = FakeClient([])
    log = make_log(tmp_path)

    stats = capture_loop(client, log, poll_interval_s=0, stop=lambda: True, sleep=lambda s: None)

    assert stats == CaptureStats(records=0, actions=0, errors=0)
    assert client.get_state_calls == 0


# --- the full scripted scenario from the brief -------------------------------


def test_full_scenario_resolving_then_settled_twice_then_unavailable_then_back_then_auth_error(
    resolving_state, settled_state, tmp_path
):
    client = FakeClient(
        [
            resolving_state,  # resolving: unsettled, not logged
            copy.deepcopy(settled_state),  # settled poll 1/2: withheld
            copy.deepcopy(settled_state),  # settled poll 2/2: emitted -> 1 state record
            BridgeUnavailable("could not reach the mod"),  # unavailable
            BridgeUnavailable("could not reach the mod"),  # still unavailable
            copy.deepcopy(settled_state),  # back: same position, already emitted
            BridgeAuthError("token rejected: reinstall the mod"),  # an auth error
        ]
    )
    log = make_log(tmp_path)
    messages = []

    stats = capture_loop(
        client, log, poll_interval_s=0, stop=never_stop, sleep=lambda s: None, out=messages.append
    )

    assert stats == CaptureStats(records=1, actions=0, errors=1)
    records = read_records(log)
    assert len(records) == 1
    assert records[0]["kind"] == "state"
    assert messages == [
        "waiting for game…",
        "game connected",
        "token rejected: reinstall the mod",
    ]
