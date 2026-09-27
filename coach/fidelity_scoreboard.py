"""The fidelity scoreboard: one command that scores the simulator against every
captured real session (roadmap task A1).

sts2's `scripts/fidelity_replay.py` replays one session log's `check` records through
the coach's own snapshot/converter/simulator path (`evaluate()`) and can name which
category -- enemy, card, relic, potion, power, reward, shop, event, map, or "other"
when a check cannot be attributed -- each one belongs to (`categorize()`). This module
does not repeat that walk: it loads whichever sts2 checkout `--sts2-repo` names,
reruns `evaluate()` over every session under `data/runs/*.jsonl`, and tallies
comparable/matching/mismatched checks and the top not-comparable reasons per act and
category. It writes the result under `data/fidelity/`:

- `<timestamp>.json`, one per run of this command, stamped with the sts2 commit used;
- `latest.json`, a copy of the newest one, for the dashboard and for people;
- a short table on stdout.

Usage:
    uv run python -m coach.fidelity_scoreboard --sts2-repo ~/Documents/git/sts2
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from coach.config import DEFAULT_DATA_DIR
from coach.saves import _stamp

DEFAULT_STS2_REPO = Path.home() / "Documents" / "git" / "sts2"
LATEST_NAME = "latest.json"
# Reasons kept per bucket: a session's own (act, category), and the run's totals (a
# few more, since it has more sessions' worth of variety to show).
TOP_REASONS_PER_SESSION = 5
TOP_REASONS_TOTAL = 10


def load_fidelity_replay(sts2_repo: Path) -> ModuleType:
    """Load `sts2_repo`'s own `scripts/fidelity_replay.py` as a module.

    Its own top-of-file `sys.path` insert reaches that checkout's `src/` for
    `sts2_gym`, and its `from coach.snapshot import ...` finds this same package: this
    module lives in `coach`, so importing it (however it was reached -- the `spire-
    coach` console script, or `python -m coach.fidelity_scoreboard`) already cached
    `coach` from here before this function ever runs, and a submodule import resolves
    against the cached package regardless of what `fidelity_replay.py` puts on
    `sys.path` afterwards.
    """
    path = sts2_repo / "scripts" / "fidelity_replay.py"
    if not path.is_file():
        raise FileNotFoundError(f"no scripts/fidelity_replay.py under {sts2_repo}")
    src = str(sts2_repo / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    spec = importlib.util.spec_from_file_location("sts2_fidelity_replay", path)
    if spec is None or spec.loader is None:  # pragma: no cover - importlib internals
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(sts2_repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(sts2_repo), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def sts2_commit(sts2_repo: Path) -> str | None:
    """`sts2_repo`'s current commit, or None outside a git checkout."""
    return _git(sts2_repo, "rev-parse", "HEAD")


def sts2_branch(sts2_repo: Path) -> str | None:
    """`sts2_repo`'s current branch, or None outside a git checkout (or detached)."""
    branch = _git(sts2_repo, "rev-parse", "--abbrev-ref", "HEAD")
    return branch if branch and branch != "HEAD" else None


def discover_sessions(runs_dir: Path) -> list[Path]:
    """Every session log under `runs_dir`, oldest first (its name is a timestamp)."""
    if not runs_dir.is_dir():
        return []
    return sorted(runs_dir.glob("*.jsonl"))


def _new_stats(replay_module: ModuleType) -> Any:
    return replay_module._ActStats()  # noqa: SLF001 - the shared bookkeeping, on purpose


def _tally(outcomes: list[Any], replay_module: ModuleType) -> dict[tuple[str, str], Any]:
    """Regroup `evaluate()`'s outcomes by (act, category), reusing `_ActStats`'s own
    bookkeeping -- replayed/comparable/matched/mismatched counts, mismatched fields, and
    not-comparable reasons grouped by template -- exactly as `fidelity_replay.main`
    does for its per-act table, just keyed one level finer.
    """
    buckets: dict[tuple[str, str], Any] = defaultdict(lambda: _new_stats(replay_module))
    for outcome in outcomes:
        category = replay_module.categorize(outcome)
        stats = buckets[(outcome.act, category)]
        stats.replayed += 1
        if not outcome.comparable:
            for warning in outcome.warnings:
                stats.add_warning(warning)
            continue
        stats.comparable += 1
        if outcome.matched:
            stats.matched += 1
        else:
            stats.mismatched += 1
            for mismatch_field in outcome.mismatch_fields:
                stats.mismatch_fields[mismatch_field] += 1
    return buckets


def _merge_stats(replay_module: ModuleType, stats_list: list[Any]) -> Any:
    """One `_ActStats` with every one of `stats_list` folded in."""
    merged = _new_stats(replay_module)
    for stats in stats_list:
        merged.replayed += stats.replayed
        merged.comparable += stats.comparable
        merged.matched += stats.matched
        merged.mismatched += stats.mismatched
        merged.mismatch_fields += stats.mismatch_fields
        merged.not_comparable_reasons += stats.not_comparable_reasons
        for template, message in stats.reason_examples.items():
            merged.reason_examples.setdefault(template, message)
        for template, by_kind in stats.reason_ids.items():
            for kind, tokens in by_kind.items():
                merged.reason_ids[template][kind] |= tokens
    return merged


def _serialize_bucket(stats: Any, *, top_reasons: int) -> dict[str, Any]:
    reasons = []
    for template, count in stats.not_comparable_reasons.most_common(top_reasons):
        ids_by_kind = stats.reason_ids.get(template, {})
        ids = {kind: sorted(tokens) for kind, tokens in sorted(ids_by_kind.items()) if tokens}
        reasons.append({"reason": template, "count": count, "ids": ids})
    return {
        "replayed": stats.replayed,
        "comparable": stats.comparable,
        "matched": stats.matched,
        "mismatched": stats.mismatched,
        "not_comparable": stats.replayed - stats.comparable,
        "mismatch_fields": dict(stats.mismatch_fields.most_common()),
        "reasons": reasons,
    }


def _acts_json(
    outcomes: list[Any], replay_module: ModuleType, *, top_reasons: int
) -> dict[str, Any]:
    """`{act: {categories: {category: bucket}, totals: bucket}}` for one flat list of
    outcomes -- a session's own, or every session's combined, for the run's totals."""
    buckets = _tally(outcomes, replay_module)
    acts: dict[str, Any] = {}
    for act_key in sorted({outcome.act for outcome in outcomes}):
        categories = {
            category: _serialize_bucket(
                buckets.get((act_key, category), _new_stats(replay_module)),
                top_reasons=top_reasons,
            )
            for category in replay_module.CATEGORIES
        }
        act_total = _merge_stats(
            replay_module,
            [stats for (act, _category), stats in buckets.items() if act == act_key],
        )
        acts[act_key] = {
            "categories": categories,
            "totals": _serialize_bucket(act_total, top_reasons=top_reasons),
        }
    return acts


def build_scoreboard(
    runs_dir: Path,
    sts2_repo: Path,
    *,
    resamples: int | None = None,
    now: datetime | None = None,
    replay_module: ModuleType | None = None,
) -> dict[str, Any]:
    """Score every session log under `runs_dir` against `sts2_repo` (or a caller-given
    `replay_module`, for tests): the JSON document this module writes and prints."""
    replay_module = replay_module or load_fidelity_replay(sts2_repo)
    if resamples is None:
        resamples = replay_module.fidelity.DEFAULT_RESAMPLES
    now = now or datetime.now()

    sessions_json = []
    all_outcomes: list[Any] = []
    for path in discover_sessions(runs_dir):
        records = replay_module.load_records(path)
        result = replay_module.evaluate(records, resamples=resamples)
        all_outcomes.extend(result.outcomes)
        sessions_json.append(
            {
                "path": path.name,
                "checks_total": result.checks_total,
                "skipped_no_post": result.skipped_no_post,
                "skipped_no_endpoint": result.skipped_no_endpoint,
                "logged_errors": result.logged_errors,
                "acts": _acts_json(
                    result.outcomes, replay_module, top_reasons=TOP_REASONS_PER_SESSION
                ),
            }
        )

    return {
        "generated_at": now.isoformat(),
        "resamples": resamples,
        "sts2": {
            "repo": str(sts2_repo),
            "commit": sts2_commit(sts2_repo),
            "branch": sts2_branch(sts2_repo),
        },
        "sessions": sessions_json,
        "totals": _acts_json(all_outcomes, replay_module, top_reasons=TOP_REASONS_TOTAL),
    }


def write_scoreboard(doc: dict[str, Any], fidelity_dir: Path) -> tuple[Path, Path]:
    """Write `doc` to `<fidelity_dir>/<timestamp>.json` and `<fidelity_dir>/latest.json`.

    Returns both paths.
    """
    fidelity_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=2)
    generated_at = datetime.fromisoformat(doc["generated_at"])
    stamped = fidelity_dir / f"{_stamp(generated_at)}.json"
    stamped.write_text(text, encoding="utf-8")
    latest = fidelity_dir / LATEST_NAME
    latest.write_text(text, encoding="utf-8")
    return stamped, latest


# --- the stdout table -----------------------------------------------------------------

_COLUMNS = ("replayed", "comparable", "matched", "mismatched", "not_comparable")
_HEADINGS = ("replayed", "comparable", "matched", "mismatched", "not-comp.")


def _row(label: str, bucket: dict[str, Any], *, label_width: int) -> str:
    cells = "  ".join(f"{bucket[col]:>10d}" for col in _COLUMNS)
    return f"  {label:<{label_width}}  {cells}"


def _table(title: str, rows: list[tuple[str, dict[str, Any]]], *, label_width: int) -> list[str]:
    lines = [title]
    lines.append(f"  {'':<{label_width}}  " + "  ".join(f"{h:>10}" for h in _HEADINGS))
    for label, bucket in rows:
        lines.append(_row(label, bucket, label_width=label_width))
    return lines


def format_table(doc: dict[str, Any]) -> str:
    """The short text table `main` prints: totals by act, by category, and one line a
    session (its own act numbers, so a single session's row reproduces exactly what
    `fidelity_replay.py` would print for it)."""
    sts2 = doc["sts2"]
    commit = (sts2["commit"] or "?")[:10]
    lines = [
        f"Simulator fidelity scoreboard -- sts2 {sts2['repo']} @ {commit}"
        + (f" ({sts2['branch']})" if sts2["branch"] else ""),
        f"{len(doc['sessions'])} session(s), resamples={doc['resamples']}",
        "",
    ]

    act_rows = [(act, bucket["totals"]) for act, bucket in sorted(doc["totals"].items())]
    if act_rows:
        label_width = max(len(label) for label, _ in act_rows)
        lines += _table("Totals by act:", act_rows, label_width=label_width)
        lines.append("")

    category_totals: dict[str, dict[str, Any]] = {}
    for act_bucket in doc["totals"].values():
        for category, bucket in act_bucket["categories"].items():
            running = category_totals.setdefault(category, dict.fromkeys(_COLUMNS, 0))
            for col in _COLUMNS:
                running[col] += bucket[col]
    category_rows = [(cat, category_totals[cat]) for cat in sorted(category_totals)]
    if category_rows:
        label_width = max(len(label) for label, _ in category_rows)
        lines += _table("Totals by category:", category_rows, label_width=label_width)
        lines.append("")

    lines.append("Per session:")
    for session in doc["sessions"]:
        parts = [
            f"act {act.removeprefix('act ')}: {b['totals']['comparable']}/"
            f"{b['totals']['replayed']} comparable ({b['totals']['matched']} matched, "
            f"{b['totals']['mismatched']} mismatched)"
            for act, b in sorted(session["acts"].items())
        ]
        lines.append(
            f"  {session['path']}: {session['checks_total']} checks -- " + "; ".join(parts)
        )
    return "\n".join(lines)


# --- the command ------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sts2-repo",
        type=Path,
        default=DEFAULT_STS2_REPO,
        help="the sts2 checkout to score against (default: %(default)s)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="where `runs/` and `fidelity/` live by default (default: %(default)s)",
    )
    parser.add_argument(
        "--runs-dir", type=Path, default=None, help="session logs (default: <data-dir>/runs)"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="where to write the scoreboard (default: <data-dir>/fidelity)",
    )
    parser.add_argument(
        "--resamples",
        type=int,
        default=None,
        help="resamples per check (default: the sts2 checkout's own default)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    runs_dir = args.runs_dir or (args.data_dir / "runs")
    out_dir = args.out_dir or (args.data_dir / "fidelity")

    if not discover_sessions(runs_dir):
        print(f"No session logs found under {runs_dir}.", file=sys.stderr)
        return 1

    doc = build_scoreboard(runs_dir, args.sts2_repo, resamples=args.resamples)
    stamped, latest = write_scoreboard(doc, out_dir)
    print(format_table(doc))
    print(f"\nWrote {stamped} and {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
