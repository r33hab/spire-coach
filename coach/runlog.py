"""Append-only run log.

One JSONL file per run, under ``<data_dir>/<subdir>/``: ``captures/`` for capture
mode (the default) and ``runs/`` for the live coach (spec 5.5). Every ``append`` is a
single, complete, flushed JSON line: ``{ts, kind, payload}``. Nothing ever rewrites a
line once it's on disk.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from coach.saves import _stamp

CAPTURES_DIRNAME = "captures"


class RunLog:
    def __init__(
        self,
        data_dir: Path,
        *,
        now: Callable[[], datetime] | None = None,
        subdir: str = CAPTURES_DIRNAME,
    ) -> None:
        self._now = now or datetime.now
        log_dir = Path(data_dir) / subdir
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"{_stamp(self._now())}.jsonl"

    def append(self, kind: str, payload: dict) -> None:
        record = {"ts": self._now().isoformat(), "kind": kind, "payload": payload}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
