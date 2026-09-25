"""The dashboard's snapshot of a training job, and its app."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from coach import dashboard
from coach.bridge.client import BridgeError

RULES_EVAL = {
    "n": 200,
    "mean_floor": 12.875,
    "ci95": [12.25, 13.5],
    "boss_kill_rate": 0.08,
    "wilson95": [0.05, 0.126],
    "mean_value": 13.515,
}


def _write(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _job(root: Path, *, champion_generation: int | None = None) -> Path:
    """A job two generations in: gen 0 finished (not promoted), gen 1 still playing."""
    _write(
        root / "status.json",
        {
            "generation": 1,
            "phase": "generating",
            "runs_this_generation": 500,
            "games_played_total": 2500,
            "runs_per_hour": 2900.0,
            "champion": {"generation": champion_generation, "eval": RULES_EVAL},
            "quarantined_seeds": {"S1": {}},
            "last_update": "2026-09-25T18:38:21+00:00",
        },
    )
    _write(root / "config.json", {"loop": {"runs_per_generation": 2000, "eps0": 0.15}})
    (root / "loop.pid").write_text(str(os.getpid()))
    (root / "loop.log").write_text("".join(f"line {i}\n" for i in range(100)))
    gen0 = root / "gen_0000"
    _write(
        gen0 / "selfplay.json",
        {
            "played_by": None,
            "runs": 2000,
            "mean_floor": 12.6,
            "boss_kill_rate": 0.07,
            "seconds": 2480,
        },
    )
    _write(
        gen0 / "eval.json",
        {"policy": {"kind": "learner"}, "summary": {**RULES_EVAL, "mean_floor": 13.1}},
    )
    _write(
        gen0 / "promotion.json",
        {"promoted": False, "rolled_back": False, "comparison": {"champion": RULES_EVAL}},
    )
    (gen0 / "retries.jsonl").write_text('{"seed": "A"}\n{"seed": "B"}\n')
    (root / "gen_0001" / "shards").mkdir(parents=True)
    return root


def test_no_status_file_means_no_job(tmp_path: Path) -> None:
    assert dashboard.collect_training(tmp_path) == {"found": False, "root": str(tmp_path)}


def test_a_job_is_summed_up_from_the_loop_files(tmp_path: Path) -> None:
    got = dashboard.collect_training(_job(tmp_path))

    assert got["found"] is True
    assert got["running"] is True and got["pid"] == os.getpid()
    assert got["runs_per_generation"] == 2000
    assert got["status"]["runs_this_generation"] == 500
    assert got["baseline"] == RULES_EVAL  # the rules still hold the title
    assert got["log"] == [f"line {i}" for i in range(60, 100)]
    gen0, gen1 = got["generations"]
    assert gen0["generation"] == 0
    assert gen0["selfplay"]["mean_floor"] == 12.6 and gen0["selfplay"]["played_by"] is None
    assert gen0["eval"]["mean_floor"] == 13.1
    assert gen0["result"] == "not promoted"
    assert gen0["champion_then"] == RULES_EVAL
    assert gen0["retries"] == 2
    assert gen1 == {
        "generation": 1,
        "selfplay": None,
        "eval": None,
        "result": None,
        "champion_then": None,
        "retries": 0,
    }


def test_the_baseline_survives_a_promotion(tmp_path: Path) -> None:
    root = _job(tmp_path, champion_generation=0)
    # After a promotion the status holds the learner; the rules' eval is kept aside.
    _write(
        root / "champion" / "eval.prev.json", {"policy": {"kind": "rules"}, "summary": RULES_EVAL}
    )

    assert dashboard.collect_training(root)["baseline"] == RULES_EVAL


def test_the_baseline_falls_back_on_generation_0s_comparison(tmp_path: Path) -> None:
    root = _job(tmp_path, champion_generation=3)

    assert dashboard.collect_training(root)["baseline"] == RULES_EVAL


def test_broken_or_missing_files_show_as_missing(tmp_path: Path) -> None:
    root = _job(tmp_path)
    (root / "gen_0000" / "eval.json").write_text("{not json")
    (root / "config.json").unlink()

    got = dashboard.collect_training(root)

    assert got["generations"][0]["eval"] is None
    assert got["runs_per_generation"] is None


def test_a_dead_loop_is_not_running(tmp_path: Path) -> None:
    root = _job(tmp_path)
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    (root / "loop.pid").write_text(child.stdout.strip())

    got = dashboard.collect_training(root)

    assert got["running"] is False and got["pid"] == int(child.stdout)


def test_training_root_prefers_the_environment(tmp_path: Path) -> None:
    assert dashboard.training_root({dashboard.TRAINING_ROOT_ENV: str(tmp_path)}) == tmp_path


def test_json_safe_drops_nan_and_infinity() -> None:
    assert dashboard.json_safe({"a": [float("nan"), 1.5, float("inf")], "b": (2,)}) == {
        "a": [None, 1.5, None],
        "b": [2],
    }


class _FakeMod:
    def __init__(self, up: bool) -> None:
        self.up = up

    def hello(self) -> dict:
        if not self.up:
            raise BridgeError("could not reach the mod")
        return {"version": "0.4.0", "commit": "6231e95"}

    def profiles(self) -> dict:
        return {"current_profile_id": 2}


def test_game_status_reports_the_profile_or_that_the_game_is_off() -> None:
    assert dashboard.game_status(_FakeMod(up=True)) == {
        "running": True,
        "mod_version": "0.4.0",
        "mod_commit": "6231e95",
        "profile": 2,
    }
    assert dashboard.game_status(_FakeMod(up=False))["running"] is False


def test_coach_status_when_nothing_listens() -> None:
    with socket.socket() as sock:  # a port that was free a moment ago
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    assert dashboard.coach_status(port) == {
        "running": False,
        "panel_url": f"http://127.0.0.1:{port}/",
    }


def test_model_info_reads_the_export(tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    assert dashboard.model_info(path) is None
    _write(
        path,
        {"generation": 4, "selfplay_boss_kill_rate": 0.11, "eval": RULES_EVAL, "eval_meta": {}},
    )

    got = dashboard.model_info(path)

    assert got["generation"] == 4 and got["eval"] == RULES_EVAL and "eval_meta" not in got


def test_the_app_serves_the_page_and_the_snapshot_only_to_itself(tmp_path: Path) -> None:
    app = dashboard.create_app(lambda: {"training": {"found": False}, "value": float("nan")})
    with TestClient(app, base_url="http://127.0.0.1:15531") as client:
        page = client.get("/")
        api = client.get("/api/dashboard")
        foreign = client.get("/api/dashboard", headers={"host": "evil.example:15531"})

    assert page.status_code == 200 and "Spire Coach Dashboard" in page.text
    assert api.status_code == 200 and api.json() == {"training": {"found": False}, "value": None}
    assert api.headers["cache-control"] == "no-store"
    assert foreign.status_code == 400
