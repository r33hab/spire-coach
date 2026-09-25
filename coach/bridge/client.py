"""HTTP client for the STS2MCP mod's local API.

The coach is offline and talks only to the mod running on this machine, so
the client refuses any base_url whose host isn't 127.0.0.1 or localhost.
It never sends an Origin header (httpx doesn't by default; keep it that
way) because the mod rejects any request that carries one.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
DEFAULT_TIMEOUT_S = 2.0

_TOKEN_REJECTED = (
    "token rejected: reinstall the mod (spire-coach mod install) or check STS2_MCP.conf"
)
_MOD_BUSY = "mod is busy (main thread overloaded); retry the request"
_FORBIDDEN_FALLBACK = (
    "request rejected (403 Forbidden): the mod refuses browser-origin requests; "
    "make sure no Origin header is sent"
)


class BridgeError(Exception):
    """Base class for bridge-client errors."""


class BridgeUnavailable(BridgeError):
    """The mod could not be reached: connection refused, timed out, or overloaded."""


class BridgeAuthError(BridgeError):
    """The mod rejected the request: bad token or a disallowed request origin."""


def _server_error(response: httpx.Response, default: str) -> str:
    try:
        data = response.json()
    except ValueError:
        return default
    if isinstance(data, dict) and isinstance(data.get("error"), str) and data["error"]:
        return data["error"]
    return default


class ModClient:
    """Talks to the STS2MCP mod's local HTTP API."""

    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        host = urlsplit(base_url).hostname
        if host not in ALLOWED_HOSTS:
            raise ValueError(
                "base_url must be 127.0.0.1 or localhost (the coach is offline and only "
                f"talks to the local mod), got {base_url!r}"
            )
        self._token = token
        self._timeout_s = timeout_s
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)

    def close(self) -> None:
        self._client.close()

    def hello(self) -> dict:
        return self._request("GET", "/")

    def profiles(self) -> dict:
        """The save profiles and which one is current (read-only)."""
        return self._request("GET", "/api/v1/profiles")

    def get_state(self) -> dict:
        # No `hidden` parameter here on purpose: there is no way to request
        # hidden-information fields through this client.
        return self._request("GET", "/api/v1/singleplayer")

    def post_action(
        self, action: dict, *, wait: bool = True, wait_timeout_ms: int = 10000
    ) -> dict:
        body = {**action, "wait": wait, "wait_timeout_ms": wait_timeout_ms}
        return self._request("POST", "/api/v1/singleplayer", json_body=body)

    def actions_since(self, seq: int) -> dict:
        return self._request("GET", "/api/v1/actions", params={"since": seq})

    def compendium(self) -> dict:
        return self._request("GET", "/api/v1/compendium")

    def _headers(self) -> dict[str, str]:
        return {"X-STS2-Token": self._token} if self._token else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        try:
            response = self._client.request(
                method, path, params=params, json=json_body, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise BridgeUnavailable(
                f"mod did not respond within {self._timeout_s}s: is the game busy or paused?"
            ) from exc
        except httpx.TransportError as exc:
            raise BridgeUnavailable(
                "could not reach the mod: is the game running with the mod loaded?"
            ) from exc

        if response.status_code == 401:
            raise BridgeAuthError(_TOKEN_REJECTED)
        if response.status_code == 403:
            raise BridgeAuthError(_server_error(response, _FORBIDDEN_FALLBACK))
        if response.status_code == 503:
            raise BridgeUnavailable(_MOD_BUSY)

        try:
            return response.json()
        except ValueError as exc:
            raise BridgeError(
                f"mod sent a non-JSON response ({response.status_code} from {path}): "
                "check the mod is running and the port in STS2_MCP.conf is correct"
            ) from exc
