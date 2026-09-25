"""The panel's floating window: a pywebview window kept above the game (spec 5.4).

`open_window` must be called on the main thread (pywebview's rule on macOS) and blocks
until the window is closed. When pywebview is missing or cannot start, the panel is
still one browser tab away: the reason goes to the log as one line and the URL is
printed for the browser.

Quitting closes the window too. On macOS pywebview turns Ctrl-C in the terminal into a
Quit, and a Quit ends the app with exit(), so nothing after `webview.start()` would run:
not the coach's shutdown either. Every quit is therefore cancelled and the window closed
in its place, which makes `open_window` return. A macOS logout or restart asks the app
to quit too, so the first attempt is cancelled once; the coach then closes and stops,
and a second attempt goes through. SIGTERM and SIGHUP close the window the same way.
"""

from __future__ import annotations

import logging
import signal

from coach.view import one_line

TITLE = "Spire Coach"
# The panel's own background (--night in panel/panel.css), shown until the page has
# loaded: the window never flashes another colour.
_BACKGROUND = "#141c29"

log = logging.getLogger(__name__)


def open_window(url: str, *, width: int = 380, height: int = 560, on_top: bool = True) -> bool:
    """Show the panel at `url` in a floating window until the user closes it.

    Returns True once the window has been closed (by the user, a Quit or Ctrl-C), or
    False at once when it could not open (the URL was printed instead): the caller keeps
    the coach running for the browser then.
    """
    try:
        import webview

        window = webview.create_window(
            TITLE,
            url=url,
            width=width,
            height=height,
            on_top=on_top,
            background_color=_BACKGROUND,
        )

        def close_instead() -> bool:
            window.destroy()
            return False  # cancels the quit, or the close, under way

        window.events.closing += close_instead
        _close_on_stop_signals(window)
        webview.start()
    except Exception as exc:  # any failure to show the window falls back to the browser
        log.warning("The floating window could not open (%s).", one_line(exc))
        print(f"Open the coach panel in your browser: {url}", flush=True)
        return False
    return True


def _close_on_stop_signals(window) -> None:
    """Make SIGTERM and SIGHUP close the window, as Ctrl-C does. While pywebview's Cocoa
    loop runs, Python's own signal handlers do not: signals reach it through PyObjC's
    Mach port, which is how pywebview handles Ctrl-C. Without PyObjC (not macOS), nothing
    changes."""
    try:
        from PyObjCTools import MachSignals
    except ImportError:
        return
    for signum in (signal.SIGTERM, signal.SIGHUP):
        MachSignals.signal(signum, lambda _signum: window.destroy())
