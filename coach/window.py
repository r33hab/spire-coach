"""The panel's floating window: a pywebview window kept above the game (spec 5.4).

`open_window` must be called on the main thread (pywebview's rule on macOS) and blocks
until the window is closed. When pywebview is missing or cannot start, the panel is
still one browser tab away: the reason goes to the log as one line and the URL is
printed for the browser.
"""

from __future__ import annotations

import logging

from coach.view import one_line

TITLE = "Spire Coach"
_BACKGROUND = "#0d0f14"  # the panel's own background, so the window never flashes white

log = logging.getLogger(__name__)


def open_window(url: str, *, width: int = 380, height: int = 560, on_top: bool = True) -> bool:
    """Show the panel at `url` in a floating window until the user closes it.

    Returns True once the window has been closed, or False at once when it could not
    open (the URL was printed instead): the caller keeps the coach running for the
    browser then.
    """
    try:
        import webview

        webview.create_window(
            TITLE,
            url=url,
            width=width,
            height=height,
            on_top=on_top,
            background_color=_BACKGROUND,
        )
        webview.start()
    except Exception as exc:  # any failure to show the window falls back to the browser
        log.warning("The floating window could not open (%s).", one_line(exc))
        print(f"Open the coach panel in your browser: {url}", flush=True)
        return False
    return True
