from coach.config import DEFAULT_BASE_URL, TOKEN_FILE_NAME, load_config, read_token


def test_defaults_when_env_is_empty(tmp_path):
    cfg = load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path)})
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.token is None
    assert cfg.poll_interval_s == 0.2
    assert cfg.mods_dir == tmp_path


def test_token_is_read_from_the_mods_dir(tmp_path):
    (tmp_path / TOKEN_FILE_NAME).write_text("abc123\n", encoding="utf-8")
    assert read_token(tmp_path) == "abc123"
    assert load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path)}).token == "abc123"


def test_env_token_overrides_the_file(tmp_path):
    (tmp_path / TOKEN_FILE_NAME).write_text("from-file", encoding="utf-8")
    cfg = load_config({"SPIRE_COACH_MODS_DIR": str(tmp_path), "SPIRE_COACH_TOKEN": "from-env"})
    assert cfg.token == "from-env"


def test_blank_token_file_means_no_token(tmp_path):
    (tmp_path / TOKEN_FILE_NAME).write_text("   \n", encoding="utf-8")
    assert read_token(tmp_path) is None


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
