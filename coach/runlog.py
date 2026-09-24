"""Append-only run log for capture mode.

One JSONL file per run, under ``<data_dir>/captures/``. Every ``append`` is a
single, complete, flushed JSON line: ``{ts, kind, payload}``. Capture mode
is the only writer; nothing ever rewrites a line once it's on disk.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from coach.saves import _stamp

CAPTURES_DIRNAME = "captures"


class RunLog:
    def __init__(self, data_dir: Path, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or datetime.now
        captures_dir = Path(data_dir) / CAPTURES_DIRNAME
        captures_dir.mkdir(parents=True, exist_ok=True)
        self.path = captures_dir / f"{_stamp(self._now())}.jsonl"

    def append(self, kind: str, payload: dict) -> None:
        record = {"ts": self._now().isoformat(), "kind": kind, "payload": payload}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
