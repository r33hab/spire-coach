"""`coach.fidelity_scoreboard`: scoring the simulator against captured sessions.

`build_scoreboard` takes an sts2 checkout's own `scripts/fidelity_replay.py` (loaded by
`load_fidelity_replay`) or, here, a fake standing in for its small interface --
`CATEGORIES`, `_ActStats`, `categorize`, `load_records`, `evaluate` -- so these tests
never need a real sts2 checkout or the native simulator. That interface itself is
exercised for real by `scripts/fidelity_replay.py`'s own suite in the sts2 repo, and by
running the actual scoreboard against the actual sessions (see the roadmap report).
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

from coach import fidelity_scoreboard as fsb

CATEGORIES = (
    "enemy",
    "card",
    "relic",
    "potion",
    "power",
    "reward",
    "shop",
    "event",
    "map",
    "other",
)


class _FakeStats:
    """Stands in for sts2's `_ActStats`: the same bookkeeping surface, none of its
    warning-templating (these tests use whole messages as their own template)."""

    def __init__(self) -> None:
        self.replayed = 0
        self.comparable = 0
        self.matched = 0
        self.mismatched = 0
        self.mismatch_fields: Counter[str] = Counter()
        self.not_comparable_reasons: Counter[str] = Counter()
        self.reason_examples: dict[str, str] = {}
        self.reason_ids: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    def add_warning(self, message: str) -> None:
        self.not_comparable_reasons[message] += 1
        self.reason_examples.setdefault(message, message)


def outcome(
    act: str,
    *,
    comparable: bool,
    matched: bool | None = None,
    warnings: tuple[str, ...] = (),
    mismatch_fields: tuple[str, ...] = (),
    move_kind: str = "card",
) -> SimpleNamespace:
    """A fake `CheckOutcome`: same fields, a plain namespace instead of the real
    (frozen) dataclass."""
    return SimpleNamespace(
        act=act,
        move_kind=move_kind,
        comparable=comparable,
        matched=matched,
        warnings=warnings,
        mismatch_fields=mismatch_fields,
    )


class _FakeReplayResult:
    def __init__(self, outcomes: list[SimpleNamespace]) -> None:
        self.outcomes = outcomes
        self.checks_total = len(outcomes)
        self.skipped_no_post = 0
        self.skipped_no_endpoint = 0
        self.logged_errors = 0


class FakeReplayModule:
    """Stands in for a loaded `scripts/fidelity_replay.py`: canned outcomes per
    session file name, and the same category rule `categorize` documents -- a
    warning naming "relic"/"power"/"enemy"/"potion" wins, then a mismatched field
    naming "powers", then the move's own kind."""

    CATEGORIES = CATEGORIES

    def __init__(self, outcomes_by_session: dict[str, list[SimpleNamespace]]) -> None:
        self._outcomes_by_session = outcomes_by_session
        self.fidelity = SimpleNamespace(DEFAULT_RESAMPLES=16)
        self.resamples_used: list[int] = []

    def _ActStats(self) -> _FakeStats:
        return _FakeStats()

    def categorize(self, check_outcome: SimpleNamespace) -> str:
        if not check_outcome.comparable:
            for warning in check_outcome.warnings:
                for cat in ("relic", "power", "enemy", "potion"):
                    if cat in warning:
                        return cat
            return "other"
        if any("powers" in field for field in check_outcome.mismatch_fields):
            return "power"
        return {"potion": "potion", "end_turn": "enemy", "select": "card", "skip": "other"}.get(
            check_outcome.move_kind, "card" if check_outcome.move_kind == "card" else "other"
        )

    def load_records(self, path: Path) -> str:
        return path.name

    def evaluate(self, records: str, *, resamples: int) -> _FakeReplayResult:
        self.resamples_used.append(resamples)
        return _FakeReplayResult(self._outcomes_by_session[records])


def _write_session(runs_dir: Path, name: str) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / name
    path.write_text("")  # the fake module never reads this; only its name matters
    return path


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("commit", "--allow-empty", "-q", "-m", "initial")


# --- discover_sessions -----------------------------------------------------------------


def test_discover_sessions_is_empty_for_a_missing_directory(tmp_path: Path) -> None:
    assert fsb.discover_sessions(tmp_path / "runs") == []


def test_discover_sessions_finds_jsonl_files_sorted_by_name(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    _write_session(runs_dir, "20260926-220853.jsonl")
    _write_session(runs_dir, "20260925-202400.jsonl")
    (runs_dir / "notes.txt").write_text("not a session")

    got = fsb.discover_sessions(runs_dir)

    assert [p.name for p in got] == ["20260925-202400.jsonl", "20260926-220853.jsonl"]


# --- sts2_commit / sts2_branch -----------------------------------------------------------


def test_sts2_commit_and_branch_read_a_real_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "sts2"
    _init_git_repo(repo)

    commit = fsb.sts2_commit(repo)
    branch = fsb.sts2_branch(repo)

    assert commit is not None and len(commit) == 40
    assert branch in ("main", "master")  # whatever this git's default branch name is


def test_sts2_commit_is_none_outside_a_git_checkout(tmp_path: Path) -> None:
    assert fsb.sts2_commit(tmp_path) is None
    assert fsb.sts2_branch(tmp_path) is None


# --- load_fidelity_replay ---------------------------------------------------------------


def test_load_fidelity_replay_raises_when_the_script_is_missing(tmp_path: Path) -> None:
    try:
        fsb.load_fidelity_replay(tmp_path)
    except FileNotFoundError as exc:
        assert "fidelity_replay.py" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_load_fidelity_replay_loads_an_arbitrary_module_by_path(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "fidelity_replay.py").write_text("ANSWER = 42\n")
    (tmp_path / "src").mkdir()  # load_fidelity_replay always adds <repo>/src to sys.path

    module = fsb.load_fidelity_replay(tmp_path)

    assert module.ANSWER == 42


# --- build_scoreboard: the tallies ------------------------------------------------------


def test_build_scoreboard_reproduces_a_sessions_own_per_act_numbers(tmp_path: Path) -> None:
    """A single session's act numbers, matching the roadmap's own report: Act 1 46 of
    62 comparable (46 of 46 matching), Act 2 8 of 69 (8 of 8), Act 3 48 of 129 (47 of
    48, one mismatch) -- reduced to a few checks per act, the shape that matters."""
    runs_dir = tmp_path / "runs"
    session = _write_session(runs_dir, "20260926-220853.jsonl")
    outcomes = (
        [outcome("act 1", comparable=True, matched=True) for _ in range(46)]
        + [
            outcome(
                "act 1",
                comparable=False,
                warnings=("pre: enemies[0].powers[0]: SKITTISH_POWER is not in the live snapshot",),
            )
            for _ in range(16)
        ]
        + [outcome("act 2", comparable=True, matched=True) for _ in range(8)]
        + [
            outcome(
                "act 2",
                comparable=False,
                warnings=("pre: enemy BOWLBUG_ROCK (BOWLBUG_ROCK_0): AI state is generic",),
            )
            for _ in range(61)
        ]
        + [outcome("act 3", comparable=True, matched=True) for _ in range(47)]
        + [
            outcome(
                "act 3",
                comparable=True,
                matched=False,
                mismatch_fields=("player.powers.DEXTERITY_POWER",),
                move_kind="end_turn",
            )
        ]
        + [
            outcome(
                "act 3",
                comparable=False,
                warnings=("relic LIZARD_TAIL is not in the live snapshot",),
            )
            for _ in range(81)
        ]
    )
    module = FakeReplayModule({session.name: outcomes})

    doc = fsb.build_scoreboard(runs_dir, tmp_path / "sts2", replay_module=module)

    (session_doc,) = doc["sessions"]
    act1, act2, act3 = (session_doc["acts"][a]["totals"] for a in ("act 1", "act 2", "act 3"))
    assert (act1["comparable"], act1["matched"], act1["replayed"]) == (46, 46, 62)
    assert (act2["comparable"], act2["matched"], act2["replayed"]) == (8, 8, 69)
    assert (act3["comparable"], act3["matched"], act3["mismatched"], act3["replayed"]) == (
        48,
        47,
        1,
        129,
    )
    # The categories a check could not be attributed to stay at zero, not absent.
    assert session_doc["acts"]["act 1"]["categories"]["map"]["replayed"] == 0
    assert set(session_doc["acts"]["act 1"]["categories"]) == set(CATEGORIES)


def test_build_scoreboard_buckets_by_category(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    session = _write_session(runs_dir, "s.jsonl")
    outcomes = [
        outcome("act 1", comparable=True, matched=True, move_kind="potion"),
        outcome("act 1", comparable=True, matched=True, move_kind="card"),
        outcome(
            "act 1",
            comparable=True,
            matched=False,
            mismatch_fields=("player.powers.DEXTERITY_POWER",),
            move_kind="end_turn",
        ),
        outcome("act 1", comparable=False, warnings=("relic LIZARD_TAIL: fields missing",)),
    ]
    module = FakeReplayModule({session.name: outcomes})

    doc = fsb.build_scoreboard(runs_dir, tmp_path / "sts2", replay_module=module)

    categories = doc["sessions"][0]["acts"]["act 1"]["categories"]
    assert categories["potion"]["comparable"] == 1 and categories["potion"]["matched"] == 1
    assert categories["card"]["comparable"] == 1
    assert categories["power"]["mismatched"] == 1
    assert categories["power"]["mismatch_fields"] == {"player.powers.DEXTERITY_POWER": 1}
    assert categories["relic"]["not_comparable"] == 1
    assert categories["relic"]["reasons"] == [
        {"reason": "relic LIZARD_TAIL: fields missing", "count": 1, "ids": {}}
    ]


def test_build_scoreboard_totals_combine_every_session(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    first = _write_session(runs_dir, "20260101-000000.jsonl")
    second = _write_session(runs_dir, "20260102-000000.jsonl")
    module = FakeReplayModule(
        {
            first.name: [outcome("act 1", comparable=True, matched=True) for _ in range(3)],
            second.name: [outcome("act 1", comparable=True, matched=True) for _ in range(5)],
        }
    )

    doc = fsb.build_scoreboard(runs_dir, tmp_path / "sts2", replay_module=module)

    assert doc["totals"]["act 1"]["totals"]["comparable"] == 8
    assert doc["totals"]["act 1"]["totals"]["replayed"] == 8
    assert len(doc["sessions"]) == 2


def test_build_scoreboard_uses_the_replay_modules_default_resamples(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    session = _write_session(runs_dir, "s.jsonl")
    module = FakeReplayModule({session.name: [outcome("act 1", comparable=True, matched=True)]})

    fsb.build_scoreboard(runs_dir, tmp_path / "sts2", replay_module=module)
    fsb.build_scoreboard(runs_dir, tmp_path / "sts2", replay_module=module, resamples=64)

    assert module.resamples_used == [16, 64]


def test_build_scoreboard_records_the_sts2_repo_commit_and_branch(tmp_path: Path) -> None:
    repo = tmp_path / "sts2"
    _init_git_repo(repo)
    runs_dir = tmp_path / "runs"
    session = _write_session(runs_dir, "s.jsonl")
    module = FakeReplayModule({session.name: [outcome("act 1", comparable=True, matched=True)]})

    doc = fsb.build_scoreboard(runs_dir, repo, replay_module=module)

    assert doc["sts2"]["repo"] == str(repo)
    assert doc["sts2"]["commit"] is not None
    assert doc["sts2"]["branch"] in ("main", "master")


def test_build_scoreboard_is_empty_but_valid_with_no_sessions(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    module = FakeReplayModule({})

    doc = fsb.build_scoreboard(runs_dir, tmp_path / "sts2", replay_module=module)

    assert doc["sessions"] == []
    assert doc["totals"] == {}


# --- write_scoreboard --------------------------------------------------------------------


def test_write_scoreboard_writes_a_stamped_file_and_latest(tmp_path: Path) -> None:
    doc = {"generated_at": "2026-09-27T18:30:00", "sessions": [], "totals": {}}
    fidelity_dir = tmp_path / "data" / "fidelity"

    stamped, latest = fsb.write_scoreboard(doc, fidelity_dir)

    assert stamped.name == "20260927-183000.json"
    assert latest.name == "latest.json"
    assert json.loads(stamped.read_text()) == doc
    assert json.loads(latest.read_text()) == doc


def test_write_scoreboard_overwrites_latest_on_a_second_run(tmp_path: Path) -> None:
    fidelity_dir = tmp_path / "fidelity"
    fsb.write_scoreboard({"generated_at": "2026-09-27T18:30:00", "a": 1}, fidelity_dir)

    _, latest = fsb.write_scoreboard({"generated_at": "2026-09-27T18:31:00", "a": 2}, fidelity_dir)

    assert json.loads(latest.read_text())["a"] == 2
    assert len(list(fidelity_dir.glob("*.json"))) == 3  # two stamped files, plus latest.json


# --- format_table --------------------------------------------------------------------


def test_format_table_shows_the_sts2_repo_and_per_session_act_numbers(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    session = _write_session(runs_dir, "20260926-220853.jsonl")
    outcomes = [outcome("act 1", comparable=True, matched=True) for _ in range(46)] + [
        outcome("act 1", comparable=False, warnings=("power gap",)) for _ in range(16)
    ]
    module = FakeReplayModule({session.name: outcomes})
    doc = fsb.build_scoreboard(runs_dir, tmp_path / "sts2-checkout", replay_module=module)

    table = fsb.format_table(doc)

    assert "sts2-checkout" in table
    assert "20260926-220853.jsonl" in table
    assert "46/62 comparable" in table
    assert "Totals by act:" in table
    assert "Totals by category:" in table


# --- the run layer (roadmap A3) -----------------------------------------------------


RUN_CATEGORIES = ("neow", "card_reward", "shop")


class _FakeRunReplay:
    """Stands in for sts2's `scripts/replay_real_run.py`: `evaluate` returns its document
    shape, one real run with a seed in its label and one committed capture."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[Path], bool]] = []

    def evaluate(self, session_paths: list[Path], *, fixtures: bool = False) -> dict:
        self.calls.append((list(session_paths), fixtures))

        def stats(compared: int, matched: int, first: str | None = None) -> dict:
            return {"compared": compared, "matched": matched, "first_divergence": first}

        real = {
            "label": "SEEDSEED01-a2",
            "seed": "SEEDSEED01",
            "ascension": 2,
            "sources": ["20260926-220853.jsonl floors 0-48"],
            "floors": 48,
            "stopped": None,
            "modded": ["character WATCHER-WATCHER"],
            "notes": ["MODDED session (...): ..."],
            "categories": {
                "neow": stats(1, 1),
                "card_reward": stats(
                    7, 2, "floor 8 card_reward cards offered: game=[] emulator=[]"
                ),
                "shop": stats(0, 0),
            },
        }
        capture = {
            "label": "QS2GYXRKWN-a8",
            "seed": "QS2GYXRKWN",
            "ascension": 8,
            "sources": ["QS2GYXRKWN-a8.json"],
            "floors": 13,
            "stopped": None,
            "modded": [],
            "notes": [],
            "categories": {c: stats(4, 4) for c in RUN_CATEGORIES},
        }
        return {
            "categories": list(RUN_CATEGORIES),
            "runs": [real, capture],
            "unmatched": [],
            "totals": {c: stats(4, 4) for c in RUN_CATEGORIES},
            "modded_totals": {
                "neow": stats(1, 1),
                "card_reward": stats(
                    7, 2, "SEEDSEED01-a2: floor 8 card_reward cards offered: game=[] emulator=[]"
                ),
                "shop": stats(0, 0),
            },
        }


def test_the_run_layer_never_carries_a_real_runs_seed() -> None:
    doc = fsb.build_run_fidelity([Path("a.jsonl")], _FakeRunReplay())

    assert "SEEDSEED01" not in json.dumps(doc)
    assert doc["runs"][0]["name"] == "20260926-220853.jsonl floors 0-48 (A2)"
    assert doc["runs"][0]["modded"] == ["character WATCHER-WATCHER"]
    assert doc["modded_totals"]["card_reward"]["first_divergence"].startswith(
        "20260926-220853.jsonl floors 0-48 (A2): floor 8"
    )


def test_build_scoreboard_scores_the_run_layer_over_runs_and_captures(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    captures = tmp_path / "captures"
    runs_dir.mkdir()
    captures.mkdir()
    (runs_dir / "20260926-220853.jsonl").write_text("", encoding="utf-8")
    (captures / "20260925-043442.jsonl").write_text("", encoding="utf-8")
    run_replay = _FakeRunReplay()

    doc = fsb.build_scoreboard(
        runs_dir,
        tmp_path,
        replay_module=FakeReplayModule({"20260926-220853.jsonl": []}),
        run_replay_module=run_replay,
    )

    ((paths, fixtures),) = run_replay.calls
    assert [p.name for p in paths] == ["20260926-220853.jsonl", "20260925-043442.jsonl"]
    assert fixtures is True
    assert doc["runs"]["totals"]["shop"] == {"compared": 4, "matched": 4, "first_divergence": None}


def test_format_table_shows_the_run_layer_and_its_modded_runs(tmp_path: Path) -> None:
    doc = fsb.build_scoreboard(
        tmp_path,
        tmp_path,
        replay_module=FakeReplayModule({}),
        run_replay_module=_FakeRunReplay(),
        capture_dirs=[],
    )

    table = fsb.format_table(doc)

    assert "Run layer (seed-exact replay): 1 unmodded run(s), 1 modded run(s) not counted" in table
    assert "card_reward      4/4" in table
