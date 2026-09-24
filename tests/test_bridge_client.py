import json

import httpx
import pytest

from coach.bridge import BridgeAuthError, BridgeError, BridgeUnavailable, ModClient

BASE_URL = "http://127.0.0.1:15526"


def make_client(handler, *, token="test-token", **kwargs):
    transport = httpx.MockTransport(handler)
    return ModClient(BASE_URL, token, transport=transport, **kwargs)


def json_response(status_code, payload):
    return httpx.Response(status_code, json=payload)


# --- construction ------------------------------------------------------------


def test_rejects_a_base_url_whose_host_is_not_localhost():
    with pytest.raises(ValueError, match="127.0.0.1 or localhost"):
        ModClient("http://example.com:15526", "tok")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_accepts_the_two_allowed_hosts(host):
    transport = httpx.MockTransport(lambda request: json_response(200, {"status": "ok"}))
    ModClient(f"http://{host}:15526", "tok", transport=transport).close()


# --- auth header ---------------------------------------------------------------


def test_every_request_sends_the_token_header():
    captured = {}

    def handler(request):
        captured["header"] = request.headers.get("x-sts2-token")
        return json_response(200, {"status": "ok"})

    make_client(handler, token="secret-token").get_state()

    assert captured["header"] == "secret-token"


def test_no_token_means_no_token_header_is_sent():
    captured = {}

    def handler(request):
        captured["has_header"] = "x-sts2-token" in request.headers
        return json_response(200, {"status": "ok"})

    make_client(handler, token=None).get_state()

    assert captured["has_header"] is False


# --- hidden param never appears --------------------------------------------------


def test_hidden_param_never_appears_in_any_request_url():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return json_response(200, {"status": "ok", "latest_seq": 0, "entries": []})

    client = make_client(handler)
    client.hello()
    client.get_state()
    client.compendium()
    client.actions_since(5)
    client.post_action({"action": "end_turn"})

    assert urls
    assert all("hidden" not in url for url in urls)


# --- endpoints -----------------------------------------------------------------


def test_hello_calls_get_root():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        return json_response(200, {"status": "ok"})

    make_client(handler).hello()

    assert captured == {"method": "GET", "path": "/"}


def test_get_state_calls_get_singleplayer():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        return json_response(200, {"status": "ok"})

    make_client(handler).get_state()

    assert captured == {"method": "GET", "path": "/api/v1/singleplayer"}


def test_compendium_calls_get_compendium():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        return json_response(200, {"status": "ok"})

    make_client(handler).compendium()

    assert captured == {"method": "GET", "path": "/api/v1/compendium"}


def test_post_action_puts_wait_and_wait_timeout_ms_in_the_body():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return json_response(200, {"status": "ok"})

    make_client(handler).post_action({"action": "end_turn"}, wait=False, wait_timeout_ms=500)

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/singleplayer"
    assert captured["body"] == {"action": "end_turn", "wait": False, "wait_timeout_ms": 500}


def test_post_action_defaults_are_wait_true_and_10000ms():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return json_response(200, {"status": "ok"})

    make_client(handler).post_action({"action": "play_card", "card_uid": "c1"})

    assert captured["body"] == {
        "action": "play_card",
        "card_uid": "c1",
        "wait": True,
        "wait_timeout_ms": 10000,
    }


def test_post_action_sends_json_content_type():
    captured = {}

    def handler(request):
        captured["content_type"] = request.headers.get("content-type")
        return json_response(200, {"status": "ok"})

    make_client(handler).post_action({"action": "end_turn"})

    assert captured["content_type"] == "application/json"


def test_actions_since_passes_since_as_a_query_param():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return json_response(200, {"status": "ok", "latest_seq": 42, "entries": []})

    result = make_client(handler).actions_since(17)

    assert captured["path"] == "/api/v1/actions"
    assert captured["params"] == {"since": "17"}
    assert result == {"status": "ok", "latest_seq": 42, "entries": []}


# --- errors ----------------------------------------------------------------------


def test_401_raises_bridge_auth_error_with_the_remedy_text():
    def handler(request):
        return json_response(401, {"status": "error", "error": "Missing or invalid token"})

    with pytest.raises(BridgeAuthError, match="reinstall the mod"):
        make_client(handler).get_state()


def test_403_from_the_origin_guard_raises_bridge_auth_error_with_the_server_message():
    def handler(request):
        return json_response(
            403, {"status": "error", "error": "Browser origins are not permitted"}
        )

    with pytest.raises(BridgeAuthError, match="Browser origins are not permitted"):
        make_client(handler).get_state()


def test_403_with_no_server_message_falls_back_to_a_message_naming_the_remedy():
    def handler(request):
        return json_response(403, {"status": "error"})

    with pytest.raises(BridgeAuthError, match="no Origin header is sent"):
        make_client(handler).get_state()


def test_connect_error_raises_bridge_unavailable():
    def handler(request):
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(BridgeUnavailable):
        make_client(handler).get_state()


def test_timeout_raises_bridge_unavailable():
    def handler(request):
        raise httpx.TimeoutException("timed out", request=request)

    with pytest.raises(BridgeUnavailable):
        make_client(handler).get_state()


def test_503_with_retry_raises_bridge_unavailable():
    def handler(request):
        return json_response(503, {"status": "error", "retry": True})

    with pytest.raises(BridgeUnavailable):
        make_client(handler).get_state()


def test_non_json_response_raises_bridge_error():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(BridgeError):
        make_client(handler).get_state()


# --- no Origin header ---------------------------------------------------------------


def test_no_request_ever_carries_an_origin_header():
    captured = []

    def handler(request):
        captured.append("origin" in request.headers)
        return json_response(200, {"status": "ok", "latest_seq": 0, "entries": []})

    client = make_client(handler)
    client.hello()
    client.get_state()
    client.compendium()
    client.actions_since(5)
    client.post_action({"action": "end_turn"})

    assert captured
    assert not any(captured)


# --- close -------------------------------------------------------------------------


def test_close_closes_the_underlying_http_client():
    client = make_client(lambda request: json_response(200, {"status": "ok"}))
    client.close()

    with pytest.raises(RuntimeError):
        client.get_state()
