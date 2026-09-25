import json
from datetime import datetime

from coach.runlog import RunLog

NOW = datetime(2026, 9, 24, 12, 0, 0)


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_writes_to_a_stamped_jsonl_file_under_data_dir_captures(tmp_path):
    log = RunLog(tmp_path, now=lambda: NOW)
    assert log.path == tmp_path / "captures" / "20260924-120000.jsonl"
    assert log.path.parent.is_dir()


def test_append_writes_one_json_line_with_ts_kind_and_payload(tmp_path):
    log = RunLog(tmp_path, now=lambda: NOW)
    log.append("state", {"state_type": "monster"})

    lines = read_lines(log.path)
    assert len(lines) == 1
    assert lines[0]["kind"] == "state"
    assert lines[0]["payload"] == {"state_type": "monster"}
    assert lines[0]["ts"] == NOW.isoformat()


def test_a_subdirectory_can_replace_captures(tmp_path):
    # The live coach logs under runs/ (spec 5.5); capture mode keeps the default.
    log = RunLog(tmp_path, now=lambda: NOW, subdir="runs")
    assert log.path == tmp_path / "runs" / "20260924-120000.jsonl"
    assert log.path.parent.is_dir()
    assert not (tmp_path / "captures").exists()


def test_multiple_appends_produce_one_valid_json_object_per_line_in_order(tmp_path):
    log = RunLog(tmp_path, now=lambda: NOW)
    log.append("state", {"n": 1})
    log.append("action", {"n": 2})
    log.append("state", {"n": 3})

    lines = read_lines(log.path)
    assert [line["kind"] for line in lines] == ["state", "action", "state"]
    assert [line["payload"]["n"] for line in lines] == [1, 2, 3]


def test_append_uses_a_fresh_timestamp_from_the_now_callable_on_every_call(tmp_path):
    ticks = iter(
        [
            datetime(2026, 9, 24, 12, 0, 0),
            datetime(2026, 9, 24, 12, 0, 1),
            datetime(2026, 9, 24, 12, 0, 2),
        ]
    )
    log = RunLog(tmp_path, now=lambda: next(ticks))
    log.append("state", {})
    log.append("state", {})

    lines = read_lines(log.path)
    assert lines[0]["ts"] == "2026-09-24T12:00:01"
    assert lines[1]["ts"] == "2026-09-24T12:00:02"


def test_append_is_flushed_to_disk_before_returning(tmp_path):
    log = RunLog(tmp_path, now=lambda: NOW)
    log.append("state", {"flushed": True})

    # Read independently of the RunLog instance: if this isn't flushed, the
    # line won't be visible here.
    assert read_lines(log.path) == [
        {"ts": NOW.isoformat(), "kind": "state", "payload": {"flushed": True}}
    ]


def test_creating_a_runlog_creates_the_captures_directory_when_missing(tmp_path):
    data_dir = tmp_path / "does" / "not" / "exist"
    RunLog(data_dir, now=lambda: NOW)
    assert (data_dir / "captures").is_dir()


def test_default_now_is_the_real_clock(tmp_path):
    before = datetime.now()
    log = RunLog(tmp_path)
    log.append("state", {})
    after = datetime.now()

    lines = read_lines(log.path)
    ts = datetime.fromisoformat(lines[0]["ts"])
    assert before <= ts <= after
