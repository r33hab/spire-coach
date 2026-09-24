import copy
import json
from pathlib import Path

from coach.bridge.versions import (
    EXPECTED_MOD_SCHEMA,
    VersionStatus,
    check_versions,
    read_game_version,
)
from coach.config import EXPECTED_GAME_VERSION

FIXTURES = Path(__file__).parent / "fixtures" / "mod"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- read_game_version -----------------------------------------------------


def test_reads_the_version_field_from_release_info(tmp_path):
    release_info = tmp_path / "release_info.json"
    release_info.write_text((FIXTURES / "release_info.json").read_text(encoding="utf-8"))
    assert read_game_version(release_info) == "v0.107.1"


def test_returns_none_when_the_file_is_missing(tmp_path):
    assert read_game_version(tmp_path / "does_not_exist.json") is None


def test_returns_none_when_the_file_is_malformed_json(tmp_path):
    release_info = tmp_path / "release_info.json"
    release_info.write_text("{not valid json", encoding="utf-8")
    assert read_game_version(release_info) is None


def test_returns_none_when_the_version_field_is_absent(tmp_path):
    release_info = tmp_path / "release_info.json"
    release_info.write_text(json.dumps({"commit": "59260271"}), encoding="utf-8")
    assert read_game_version(release_info) is None


def test_returns_none_when_the_document_is_not_an_object(tmp_path):
    release_info = tmp_path / "release_info.json"
    release_info.write_text(json.dumps(["v0.107.1"]), encoding="utf-8")
    assert read_game_version(release_info) is None


# --- check_versions ----------------------------------------------------------


def test_expected_mod_schema_is_4():
    assert EXPECTED_MOD_SCHEMA == 4


def test_ok_when_everything_matches():
    payload = load("state_monster_player_turn.json")
    assert check_versions(EXPECTED_GAME_VERSION, payload) == VersionStatus(ok=True, problems=[])


def test_flags_a_game_version_mismatch():
    payload = load("state_monster_player_turn.json")
    status = check_versions("v0.108.0", payload)
    assert status.ok is False
    assert len(status.problems) == 1
    assert "v0.108.0" in status.problems[0]


def test_flags_an_unknown_game_version():
    payload = load("state_monster_player_turn.json")
    status = check_versions(None, payload)
    assert status.ok is False
    assert any("game version" in problem for problem in status.problems)


def test_flags_a_schema_version_mismatch():
    payload = load("state_monster_player_turn.json")
    payload["schema_version"] = 3
    status = check_versions(EXPECTED_GAME_VERSION, payload)
    assert status.ok is False
    assert any("schema_version" in problem for problem in status.problems)


def test_flags_a_missing_schema_version():
    payload = load("state_monster_player_turn.json")
    del payload["schema_version"]
    status = check_versions(EXPECTED_GAME_VERSION, payload)
    assert status.ok is False
    assert any("schema_version" in problem for problem in status.problems)


def test_flags_a_mod_version_that_does_not_start_with_0_4():
    payload = load("state_monster_player_turn.json")
    payload["mod_version"] = "0.3.9"
    status = check_versions(EXPECTED_GAME_VERSION, payload)
    assert status.ok is False
    assert any("mod_version" in problem for problem in status.problems)


def test_flags_a_missing_mod_version():
    payload = load("state_monster_player_turn.json")
    del payload["mod_version"]
    status = check_versions(EXPECTED_GAME_VERSION, payload)
    assert status.ok is False
    assert any("mod_version" in problem for problem in status.problems)


def test_reports_one_problem_per_field_when_everything_is_wrong():
    payload = load("state_monster_player_turn.json")
    payload["schema_version"] = 3
    payload["mod_version"] = "0.3.0"
    status = check_versions("v0.999.0", payload)
    assert status.ok is False
    assert len(status.problems) == 3


def test_version_status_is_frozen_and_comparable():
    a = VersionStatus(ok=False, problems=["x"])
    b = VersionStatus(ok=False, problems=["x"])
    assert a == b


def test_check_versions_does_not_mutate_the_payload():
    payload = load("state_monster_player_turn.json")
    original = copy.deepcopy(payload)
    check_versions(EXPECTED_GAME_VERSION, payload)
    assert payload == original
