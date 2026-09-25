"""Tests for the floating panel window (coach/window.py).

pywebview is always faked: no test opens a real window.
"""

from __future__ import annotations

import logging
import re
import sys
import types

from coach.server import PANEL_DIR
from coach.window import open_window

URL = "http://127.0.0.1:15530/"


class FakeEvent:
    """A pywebview window event: handlers are added with `+=`."""

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeWindow:
    """What pywebview's create_window returns, as far as the panel's window uses it."""

    def __init__(self) -> None:
        self.events = types.SimpleNamespace(closing=FakeEvent())
        self.destroyed = 0

    def destroy(self) -> None:
        self.destroyed += 1


class FakeWebview(types.ModuleType):
    """The two pywebview calls the window makes. `start` raises `fail`, if given."""

    def __init__(self, fail: Exception | None = None) -> None:
        super().__init__("webview")
        self.fail = fail
        self.windows: list[dict] = []  # the options each window was created with
        self.created: list[FakeWindow] = []
        self.starts = 0

    def create_window(self, title, url=None, **options):
        self.windows.append({"title": title, "url": url, **options})
        self.created.append(FakeWindow())
        return self.created[-1]

    def start(self, *args, **kwargs):
        self.starts += 1
        if self.fail is not None:
            raise self.fail


def test_the_panel_opens_in_a_window_kept_above_the_game(monkeypatch, capsys, caplog):
    webview = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", webview)

    assert open_window(URL) is True  # once the window has been closed

    [window] = webview.windows
    assert window["url"] == URL
    assert (window["width"], window["height"], window["on_top"]) == (380, 560, True)
    assert webview.starts == 1
    assert capsys.readouterr().out == ""
    assert caplog.records == []


def test_the_window_takes_the_size_and_on_top_it_is_given(monkeypatch):
    webview = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", webview)

    open_window(URL, width=420, height=300, on_top=False)

    [window] = webview.windows
    assert (window["width"], window["height"], window["on_top"]) == (420, 300, False)


def test_the_window_opens_on_the_panels_own_background(monkeypatch):
    # The colour lives in panel.css: the window shows it until the page has loaded,
    # so it must be the same one, not a flash of another.
    css = (PANEL_DIR / "panel.css").read_text(encoding="utf-8")
    night = re.search(r"--night:\s*(#[0-9a-fA-F]{6})", css).group(1)
    webview = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", webview)

    open_window(URL)

    [window] = webview.windows
    assert window["background_color"].lower() == night.lower()


def test_a_quit_closes_the_window_instead_so_the_coach_stops_in_order(monkeypatch):
    # pywebview turns Ctrl-C in the terminal into a Quit, and a Quit ends the app with
    # exit(): webview.start() never returns and the coach never gets to stop its engine
    # and threads. So a quit, like a click on the close button, is cancelled and the
    # window closed in its place, which does make start() return.
    webview = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", webview)

    open_window(URL)

    [window] = webview.created
    [on_closing] = window.events.closing.handlers
    assert window.destroyed == 0
    assert on_closing() is False  # the quit or close under way is cancelled...
    assert window.destroyed == 1  # ...and the window closed instead


def test_without_pywebview_the_url_is_printed_for_the_browser(monkeypatch, capsys, caplog):
    monkeypatch.setitem(sys.modules, "webview", None)  # `import webview` fails

    with caplog.at_level(logging.WARNING, logger="coach.window"):
        assert open_window(URL) is False

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("The floating window could not open (")
    assert "\n" not in record.getMessage()
    assert capsys.readouterr().out == f"Open the coach panel in your browser: {URL}\n"


def test_a_window_that_cannot_start_falls_back_to_the_browser(monkeypatch, capsys, caplog):
    webview = FakeWebview(fail=RuntimeError("no display\n  to show it on"))
    monkeypatch.setitem(sys.modules, "webview", webview)

    with caplog.at_level(logging.WARNING, logger="coach.window"):
        assert open_window(URL) is False

    assert [record.getMessage() for record in caplog.records] == [
        "The floating window could not open (no display to show it on)."
    ]
    assert capsys.readouterr().out == f"Open the coach panel in your browser: {URL}\n"
