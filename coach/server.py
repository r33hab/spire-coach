"""The coach's local server: the panel page, its live updates and its settings.

`create_app` builds the FastAPI app the panel talks to:

- `GET /` serves the panel (panel/index.html, with its CSS and JS under /panel/);
- `GET /state` returns the current CoachState as JSON;
- `WS /ws` sends the current state as soon as a panel connects, then every state
  published, so a panel that reconnects after a server restart or sleep is up to date
  at once (Review Focus 5);
- `POST /settings` takes `{mode?: "show"|"quiz", override_versions?: bool}` as JSON.
  A valid update goes to the coach whole; an invalid one is refused whole, with one
  sentence saying why.

`Broadcaster.publish` is the session's publish callback: it may be called from any
thread and fans each state out to every connected panel. The state provider must
return the newest state published, so the caller keeps each state before handing it
to the broadcaster; a panel that connects meanwhile then gets it one way or the other.

Offline by construction: `create_server` binds 127.0.0.1 only, and nothing the app
serves loads anything from the network (FastAPI's docs pages, which pull their
scripts from a CDN, are turned off).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import anyio
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from coach.live import MODES
from coach.view import CoachState, one_line, sentence

HOST = "127.0.0.1"  # offline: only this Mac can reach the panel
DEFAULT_PORT = 15530
PANEL_DIR = Path(__file__).resolve().parent.parent / "panel"

_SETTINGS = ("mode", "override_versions")
_INBOX_MAX = 64  # states a slow panel may fall behind by before it skips the oldest


# --- fanning states out to the panels -----------------------------------------------


class Broadcaster:
    """Fans every published CoachState out to the connected panels.

    `publish` may be called from any thread: the session publishes from its own. Each
    panel's states wait in its own inbox until the server's event loop sends them.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inboxes: set[_Inbox] = set()

    @property
    def clients(self) -> int:
        """How many panels are connected."""
        with self._lock:
            return len(self._inboxes)

    def publish(self, state: CoachState) -> None:
        message = state.to_json()
        with self._lock:
            inboxes = list(self._inboxes)
        for inbox in inboxes:
            inbox.put(message)

    @contextlib.contextmanager
    def _subscribe(self) -> Iterator[_Inbox]:
        """An inbox on the running event loop, for as long as one panel is connected."""
        inbox = _Inbox(asyncio.get_running_loop())
        with self._lock:
            self._inboxes.add(inbox)
        try:
            yield inbox
        finally:
            with self._lock:
                self._inboxes.discard(inbox)


class _Inbox:
    """One panel's states waiting to be sent: filled from any thread, read on the loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._messages: asyncio.Queue[str] = asyncio.Queue()

    def put(self, message: str) -> None:
        # The loop is closed once the server has stopped: no panel is left to tell.
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._put_now, message)

    def _put_now(self, message: str) -> None:
        if self._messages.qsize() >= _INBOX_MAX:
            self._messages.get_nowait()  # every state is whole: a slow panel skips ahead
        self._messages.put_nowait(message)

    async def get(self) -> str:
        return await self._messages.get()


# --- the app -------------------------------------------------------------------------


def create_app(
    state_provider: Callable[[], CoachState],
    on_settings: Callable[[dict], None],
    *,
    broadcaster: Broadcaster | None = None,
) -> FastAPI:
    """The panel's app. `state_provider` returns the current state; `on_settings` gets
    each valid settings update, with only the fields it sets. The app pushes what
    `broadcaster` publishes (a new one when none is given), which is also kept at
    `app.state.broadcaster`."""
    broadcaster = broadcaster if broadcaster is not None else Broadcaster()
    app = FastAPI(title="Spire Coach", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.broadcaster = broadcaster

    @app.get("/")
    async def panel() -> FileResponse:
        return FileResponse(PANEL_DIR / "index.html")

    @app.get("/state")
    async def state() -> Response:
        return Response(state_provider().to_json(), media_type="application/json")

    @app.post("/settings")
    async def settings(request: Request) -> JSONResponse:
        # Only a JSON body is read: a page on another site cannot send one without
        # asking first, and this server never says yes.
        media_type = request.headers.get("content-type", "").partition(";")[0]
        if media_type.strip().lower() != "application/json":
            return _refused(415, "Send the settings as JSON (Content-Type: application/json).")
        try:
            update = await request.json()
        except ValueError:
            return _refused(422, "The settings are not valid JSON.")
        problem = _settings_problem(update)
        if problem is not None:
            return _refused(422, problem)
        try:
            on_settings(update)
        except Exception as exc:  # the panel shows one sentence, never a traceback
            return _refused(
                500, sentence(f"The coach could not apply the settings: {one_line(exc)}")
            )
        return JSONResponse({"ok": True})

    @app.websocket("/ws")
    async def updates(websocket: WebSocket) -> None:
        await websocket.accept()
        # Subscribed before the current state is read: a state published in between
        # is queued behind it, never lost.
        with broadcaster._subscribe() as inbox:
            first = state_provider().to_json()
            async with anyio.create_task_group() as tasks:

                async def push() -> None:
                    with contextlib.suppress(WebSocketDisconnect):
                        await websocket.send_text(first)
                        while True:
                            await websocket.send_text(await inbox.get())
                    tasks.cancel_scope.cancel()

                tasks.start_soon(push)
                await _until_closed(websocket)
                tasks.cancel_scope.cancel()

    app.mount("/panel", StaticFiles(directory=PANEL_DIR), name="panel")
    return app


def create_server(app: FastAPI, *, port: int = DEFAULT_PORT) -> uvicorn.Server:
    """A uvicorn server for `app` that listens on 127.0.0.1 only, never the network.

    Run it with `server.run()` (in a thread); stop it with `server.should_exit = True`.
    """
    return uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))


# --- helpers -------------------------------------------------------------------------


async def _until_closed(websocket: WebSocket) -> None:
    """Return once the panel has gone away. The panel sends nothing: reading is how a
    close is noticed at once, and not only at the next publish."""
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass


def _settings_problem(update: object) -> str | None:
    """Why a settings update is refused, as one sentence, or None when it is valid."""
    if not isinstance(update, dict):
        return "The settings must be a JSON object."
    unknown = [key for key in update if key not in _SETTINGS]
    if unknown:
        return sentence(f"Unknown setting: {', '.join(unknown)}")
    if "mode" in update and update["mode"] not in MODES:
        return 'The mode must be "show" or "quiz".'
    if "override_versions" in update and not isinstance(update["override_versions"], bool):
        return "override_versions must be true or false."
    return None


def _refused(status: int, problem: str) -> JSONResponse:
    return JSONResponse({"error": problem}, status_code=status)
