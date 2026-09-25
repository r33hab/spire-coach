"""Tests for the panel's server (coach/server.py), through Starlette's TestClient.

Nothing binds a port: TestClient runs the app in-process. It addresses the app the way
the panel does, as http://127.0.0.1:15530, since the app refuses any other host.
`Coach` stands in for the live session and the `coach` command's wiring around it: the
state it last published, which the app reads, and the settings the app hands it.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import threading
import time
from collections.abc import Callable

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from coach.bridge.versions import VersionStatus
from coach.server import PANEL_DIR, Broadcaster, create_app, create_server
from coach.view import CoachState

PANEL_URL = "http://127.0.0.1:15530"  # where the panel's page is opened, on the default port


def coach_state(**overrides) -> CoachState:
    fields = dict(
        status="idle",
        status_text="Connected: waiting for a combat decision.",
        recommendation=None,
        last_grade=None,
        mode="show",
        versions=VersionStatus(ok=True, problems=[]),
        run={"act": 1, "floor": 2},
    )
    fields.update(overrides)
    return CoachState(**fields)


def move(label: str, kind: str = "card") -> dict:
    return {
        "kind": kind,
        "card_index": 0 if kind == "card" else None,
        "card_uid": "c1" if kind == "card" else None,
        "card_id": "BASH" if kind == "card" else None,
        "potion_slot": None,
        "potion_id": None,
        "target": "JAW_WORM_0" if kind == "card" else None,
        "select_index": None,
        "label": label,
    }


THINKING = coach_state(status="thinking", status_text="Analyzing this position…")
ADVICE = coach_state(
    status="advice",
    status_text="Advice ready.",
    recommendation={
        "best": move("Bash → Jaw Worm"),
        "line": [move("Bash → Jaw Worm"), move("End turn", "end_turn")],
        "alternatives": [
            {
                "move": move("End turn", "end_turn"),
                "cost_mean_hp": 6.5,
                "cost_lower_hp": 4.1,
                "p_death_delta": math.nan,  # no data behind it: the panel gets null
            }
        ],
        "expected_hp_loss": 4.0,
        "p_death": math.inf,
        "reliable": True,
        "warnings": [],
    },
)


class Coach:
    """The session's side of the app. `publish` does what the coach command's wiring
    does: keep the state for /state and for panels that connect later, then push it
    to the panels connected now."""

    def __init__(self) -> None:
        self.state = coach_state()
        self.settings: list[dict] = []
        self.broadcaster = Broadcaster()

    def publish(self, state: CoachState) -> None:
        self.state = state
        self.broadcaster.publish(state)


@pytest.fixture
def coach() -> Coach:
    return Coach()


def start_receiving(panel, timeout: float = 5.0) -> Callable[[], dict]:
    """Start waiting for the next state `panel` receives. The function returned gives
    it, and fails, rather than hanging, when none comes within `timeout`."""
    received: dict = {}

    def wait() -> None:
        try:
            received["state"] = panel.receive_json()
        except Exception as exc:  # re-raised on the test's own thread below
            received["error"] = exc

    waiter = threading.Thread(target=wait, daemon=True)
    waiter.start()

    def result() -> dict:
        waiter.join(timeout)
        if waiter.is_alive():
            pytest.fail(f"no state reached the panel within {timeout:.0f} s")
        if "error" in received:
            raise received["error"]
        return received["state"]

    return result


def receive(panel) -> dict:
    return start_receiving(panel)()


@pytest.fixture
def client(coach):
    app = create_app(lambda: coach.state, coach.settings.append, broadcaster=coach.broadcaster)
    with TestClient(app, base_url=PANEL_URL) as client:
        yield client


# --- GET /state and the panel's files ------------------------------------------------


def test_state_is_the_current_coach_state_as_strict_json(client, coach):
    response = client.get("/state")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == coach.state.to_dict()

    coach.state = ADVICE
    response = client.get("/state")

    assert response.json() == ADVICE.to_dict()
    assert response.json()["recommendation"]["p_death"] is None  # inf is not JSON
    assert "NaN" not in response.text and "Infinity" not in response.text


def test_the_panel_page_is_served_with_the_files_it_references(client):
    page = client.get("/")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    references = re.findall(r'(?:href|src)="(/[^"]*)"', page.text)
    assert sorted(references) == ["/panel/panel.css", "/panel/panel.js"]
    for reference, kind in zip(sorted(references), ("text/css", "javascript"), strict=True):
        response = client.get(reference)
        assert response.status_code == 200
        assert kind in response.headers["content-type"]


@pytest.mark.parametrize("name", ["index.html", "panel.css", "panel.js"])
def test_the_panel_loads_nothing_from_outside_this_mac(name):
    text = (PANEL_DIR / name).read_text(encoding="utf-8")

    # An address with a host, unless it is this Mac's own (the page's CSP names them).
    external = re.search(
        r"(?:https?|wss?)://(?!(?:127\.0\.0\.1|localhost)[:/])[\w.-]"
        r"|(?:src|href)\s*=\s*[\"']//|url\(\s*[\"']?//|@import",
        text,
        re.IGNORECASE,
    )

    assert external is None, f"{name} loads {external.group(0)!r} from elsewhere"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_there_are_no_api_docs_pages(client, path):
    # FastAPI's docs pages load their scripts from a CDN: the coach stays offline.
    assert client.get(path).status_code == 404


# --- the Host header: DNS rebinding --------------------------------------------------

HTTP_ROUTES = [("GET", "/"), ("GET", "/state"), ("POST", "/settings"), ("GET", "/panel/panel.css")]


def request(client, method: str, path: str, host: str):
    body = {"mode": "quiz"} if method == "POST" else None
    return client.request(method, path, headers={"host": host}, json=body)


@pytest.mark.parametrize("host", ["127.0.0.1:15530", "localhost:15530"])
@pytest.mark.parametrize("method, path", HTTP_ROUTES)
def test_requests_addressed_to_the_panels_own_host_are_answered(client, method, path, host):
    assert request(client, method, path, host).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example:15530",  # a page whose own name was rebound to 127.0.0.1
        "evil.example",
        "127.0.0.1.evil.example:15530",
        "127.0.0.1:8000",  # addressed to another port
        "127.0.0.1",  # the port left out: only port 80 may be
        "localhost",
    ],
)
@pytest.mark.parametrize("method, path", HTTP_ROUTES)
def test_requests_addressed_to_any_other_host_are_refused(client, coach, method, path, host):
    # After DNS rebinding a page on another site counts as same-origin with the coach
    # in the browser, but the browser still names that site in the Host header.
    response = request(client, method, path, host)

    assert response.status_code == 400
    assert response.json() == {
        "error": "The coach only answers requests addressed to 127.0.0.1 or localhost "
        "on its own port."
    }
    assert coach.settings == []  # nothing was applied


def test_on_port_80_the_host_leaves_the_port_out_as_browsers_send_it(coach):
    app = create_app(lambda: coach.state, coach.settings.append)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/state").status_code == 200  # Host: 127.0.0.1
        assert client.get("/state", headers={"host": "localhost"}).status_code == 200
        assert client.get("/state", headers={"host": "evil.example"}).status_code == 400


# --- POST /settings ------------------------------------------------------------------


@pytest.mark.parametrize(
    "update",
    [
        {"mode": "quiz"},
        {"mode": "show"},
        {"override_versions": True},
        {"override_versions": False},
        {"mode": "quiz", "override_versions": True},
        {},
    ],
)
def test_valid_settings_are_handed_to_the_coach_as_sent(client, coach, update):
    response = client.post("/settings", json=update)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert coach.settings == [update]


@pytest.mark.parametrize(
    "body, problem",
    [
        ({"mode": "hard"}, 'The mode must be "show" or "quiz".'),
        ({"mode": "Quiz"}, 'The mode must be "show" or "quiz".'),
        ({"mode": None}, 'The mode must be "show" or "quiz".'),
        ({"mode": True}, 'The mode must be "show" or "quiz".'),
        ({"override_versions": "yes"}, "override_versions must be true or false."),
        ({"override_versions": 1}, "override_versions must be true or false."),
        ({"override_versions": None}, "override_versions must be true or false."),
        ({"volume": 3}, "Unknown setting: volume."),
        # One bad field refuses the whole update: the good one is not applied either.
        ({"mode": "quiz", "override_versions": "no"}, "override_versions must be true or false."),
        (["mode", "quiz"], "The settings must be a JSON object."),
        ("quiz", "The settings must be a JSON object."),
        (None, "The settings must be a JSON object."),
    ],
)
def test_invalid_settings_are_refused_whole_with_one_sentence(client, coach, body, problem):
    response = client.post(
        "/settings", content=json.dumps(body), headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert response.json() == {"error": problem}
    assert coach.settings == []


@pytest.mark.parametrize(
    "content_type, body, status",
    [
        ("application/json; charset=utf-8", b'{"mode": "quiz"}', 200),
        ("application/json", b"{mode: quiz}", 422),  # not JSON
        ("text/plain", b'{"mode": "quiz"}', 415),  # what a page on another site can send
        (None, b'{"mode": "quiz"}', 415),
    ],
)
def test_settings_are_only_read_from_a_json_body(client, coach, content_type, body, status):
    headers = {"Content-Type": content_type} if content_type else {}

    response = client.post("/settings", content=body, headers=headers)

    assert response.status_code == status
    if status == 200:
        assert coach.settings == [{"mode": "quiz"}]
    else:
        error = response.json()["error"]
        assert error.endswith(".") and "\n" not in error
        assert coach.settings == []


def test_settings_the_coach_cannot_apply_come_back_as_one_sentence(coach):
    def on_settings(update: dict) -> None:
        raise RuntimeError("the session\n  has stopped")

    app = create_app(lambda: coach.state, on_settings)
    with TestClient(app, base_url=PANEL_URL) as client:
        response = client.post("/settings", json={"mode": "quiz"})

    assert response.status_code == 500
    assert response.json() == {
        "error": "The coach could not apply the settings: the session has stopped."
    }


# --- WS /ws --------------------------------------------------------------------------


def test_a_panel_gets_the_current_state_as_soon_as_it_connects(client, coach):
    coach.state = ADVICE

    with client.websocket_connect("/ws") as panel:
        assert receive(panel) == ADVICE.to_dict()


def test_every_publish_reaches_every_connected_panel_in_order(client, coach):
    with client.websocket_connect("/ws") as first, client.websocket_connect("/ws") as second:
        for panel in (first, second):
            assert receive(panel) == coach.state.to_dict()

        coach.publish(THINKING)
        coach.publish(ADVICE)

        for panel in (first, second):
            assert receive(panel) == THINKING.to_dict()
            assert receive(panel) == ADVICE.to_dict()


def test_a_publish_from_the_sessions_own_thread_wakes_a_waiting_panel(client, coach):
    # The session publishes from its own thread while the server's loop sits idle,
    # waiting for a state: the publish itself has to wake the loop.
    with client.websocket_connect("/ws") as panel:
        receive(panel)
        next_state = start_receiving(panel)
        time.sleep(0.2)  # the loop is asleep, waiting

        session = threading.Thread(target=coach.publish, args=(ADVICE,))
        session.start()
        session.join()

        assert next_state() == ADVICE.to_dict()


def test_a_panel_that_reconnects_gets_what_it_missed_at_once(client, coach):
    # Review Focus 5: after a server restart or sleep, the panel connects again and
    # the newest state is sent on connect.
    with client.websocket_connect("/ws") as panel:
        receive(panel)
        assert coach.broadcaster.clients == 1
    assert coach.broadcaster.clients == 0  # a panel that went away is dropped

    coach.publish(ADVICE)  # while no panel is connected

    with client.websocket_connect("/ws") as panel:
        assert receive(panel) == ADVICE.to_dict()
        coach.publish(THINKING)
        assert receive(panel) == THINKING.to_dict()


def test_a_panel_that_closes_is_dropped_at_once_not_at_the_next_publish(client, coach):
    with client.websocket_connect("/ws") as panel:
        receive(panel)

        panel.close()
        deadline = time.monotonic() + 5.0
        while coach.broadcaster.clients and time.monotonic() < deadline:
            time.sleep(0.01)

        assert coach.broadcaster.clients == 0


@pytest.mark.parametrize(
    "url, origin",
    [
        ("ws://127.0.0.1:15530/ws", "http://127.0.0.1:15530"),  # a tab, or the window
        ("ws://127.0.0.1:15530/ws", "http://localhost:15530"),
        ("ws://127.0.0.1/ws", "http://127.0.0.1"),  # an origin leaves out the default port
    ],
)
def test_the_panels_own_page_may_open_the_socket(client, coach, url, origin):
    with client.websocket_connect(url, headers={"origin": origin}) as panel:
        assert receive(panel) == coach.state.to_dict()


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "http://evil.example:15530",  # a DNS-rebinding page
        "http://127.0.0.1:8000",  # another page on this Mac
        "https://127.0.0.1:15530",
        "http://127.0.0.1.evil.example:15530",
        "null",  # a file:// page or a sandboxed frame
    ],
)
def test_a_page_from_anywhere_else_is_refused_before_the_socket_opens(client, coach, origin):
    # Browsers apply no CORS to a WebSocket: without this, any page open in the
    # browser could read the coach's state.
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("ws://127.0.0.1:15530/ws", headers={"origin": origin}):
            pass

    assert refused.value.code == 1008  # policy violation
    assert coach.broadcaster.clients == 0


def test_a_client_that_sends_no_origin_may_connect(client, coach):
    # No Origin header means no browser page: a local script, say.
    with client.websocket_connect("ws://127.0.0.1:15530/ws") as panel:
        assert receive(panel) == coach.state.to_dict()


def test_the_app_makes_its_own_broadcaster_when_given_none(coach):
    app = create_app(lambda: coach.state, coach.settings.append)

    with TestClient(app, base_url=PANEL_URL) as client, client.websocket_connect("/ws") as panel:
        receive(panel)
        app.state.broadcaster.publish(THINKING)

        assert receive(panel) == THINKING.to_dict()


# --- binding -------------------------------------------------------------------------


def test_the_server_listens_on_this_mac_only(coach):
    app = create_app(lambda: coach.state, coach.settings.append)

    server = create_server(app, port=15531)

    assert (server.config.host, server.config.port) == ("127.0.0.1", 15531)
    assert create_server(app).config.port == 15530  # the default port
    assert "host" not in inspect.signature(create_server).parameters
    assert server.config.log_level == "critical"  # the coach reports its own problems
