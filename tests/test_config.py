import json

import pytest

from coach.config import CONF_FILE_NAME, DEFAULT_BASE_URL, load_config, read_token


def write_conf(mods_dir, **fields):
    (mods_dir / CONF_FILE_NAME).write_text(json.dumps(fields), encoding="utf-8")


def test_defaults_when_env_is_empty(tmp_path):
    cfg = load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path)})
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.token is None
    assert cfg.poll_interval_s == 0.2
    assert cfg.mods_dir == tmp_path


def test_read_token_is_none_when_the_conf_file_is_missing(tmp_path):
    assert read_token(tmp_path) is None


def test_read_token_is_none_when_the_key_is_missing(tmp_path):
    write_conf(tmp_path, port=15526, allow_hidden_state=False)
    assert read_token(tmp_path) is None


def test_read_token_is_none_when_the_token_is_blank(tmp_path):
    write_conf(tmp_path, port=15526, token="   ")
    assert read_token(tmp_path) is None


def test_token_is_read_from_the_conf_file(tmp_path):
    write_conf(tmp_path, port=15526, token="abc123")
    assert read_token(tmp_path) == "abc123"
    assert load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path)}).token == "abc123"


def test_env_token_overrides_the_file(tmp_path):
    write_conf(tmp_path, port=15526, token="from-file")
    cfg = load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path), "SPIRE_COACH_TOKEN": "from-env"})
    assert cfg.token == "from-env"


def test_trailing_slash_is_stripped_from_base_url(tmp_path):
    cfg = load_config(
        {"SPIRE_COACH_MODS_DIR": str(tmp_path), "SPIRE_COACH_BASE_URL": "http://127.0.0.1:9999/"}
    )
    assert cfg.base_url == "http://127.0.0.1:9999"


def test_poll_interval_and_data_dir_come_from_env(tmp_path):
    cfg = load_config(
        {
            "SPIRE_COACH_MODS_DIR": str(tmp_path),
            "SPIRE_COACH_POLL_S": "0.5",
            "SPIRE_COACH_DATA_DIR": str(tmp_path / "d"),
        }
    )
    assert cfg.poll_interval_s == 0.5
    assert cfg.data_dir == tmp_path / "d"


# --- corrupt conf file -------------------------------------------------------


def test_read_token_is_none_when_the_conf_file_is_not_valid_json(tmp_path):
    (tmp_path / CONF_FILE_NAME).write_text("{not valid json", encoding="utf-8")
    assert read_token(tmp_path) is None


def test_load_config_does_not_raise_when_the_conf_file_is_not_valid_json(tmp_path):
    (tmp_path / CONF_FILE_NAME).write_text("{not valid json", encoding="utf-8")
    cfg = load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path)})
    assert cfg.token is None


# --- invalid poll interval ----------------------------------------------------


def test_non_numeric_poll_interval_raises_a_clear_value_error(tmp_path):
    with pytest.raises(
        ValueError, match=r"invalid setting SPIRE_COACH_POLL_S=abc: must be a number"
    ):
        load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path), "SPIRE_COACH_POLL_S": "abc"})
