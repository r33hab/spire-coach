import errno
import json
import shutil
import subprocess
from pathlib import Path

import coach.cli as cli
from coach.bridge.client import BridgeAuthError, BridgeError, BridgeUnavailable
from coach.capture import CaptureStats
from coach.config import Config
from coach.modinstall import InstallError
from coach.saves import SaveError, backup

FIXTURES = Path(__file__).parent / "fixtures" / "mod"


def never_running():
    return False


def make_config(tmp_path, **overrides):
    fields = {
        "base_url": "http://127.0.0.1:15526",
        "token": "test-token",
        "data_dir": tmp_path / "data",
        "poll_interval_s": 0.2,
        "mods_dir": tmp_path / "mods",
    }
    fields.update(overrides)
    return Config(**fields)


# --- dispatch: every subcommand parses and calls the right function ----------


def test_doctor_dispatches_to_cmd_doctor(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "cmd_doctor", lambda cfg: calls.append(cfg) or 0)

    assert cli.main(["doctor"]) == 0
    assert calls == [config]


def test_capture_dispatches_to_cmd_capture(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "cmd_capture", lambda cfg: calls.append(cfg) or 0)

    assert cli.main(["capture"]) == 0
    assert calls == [config]


def test_saves_backup_dispatches_with_the_default_backup_root(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_config", lambda: make_config(tmp_path))
    calls = []
    monkeypatch.setattr(cli, "cmd_saves_backup", lambda backup_root: calls.append(backup_root) or 0)

    assert cli.main(["saves", "backup"]) == 0
    assert calls == [cli.DEFAULT_BACKUP_ROOT]


def test_saves_backup_dispatches_with_a_custom_backup_root(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_config", lambda: make_config(tmp_path))
    calls = []
    monkeypatch.setattr(cli, "cmd_saves_backup", lambda backup_root: calls.append(backup_root) or 0)
    custom = tmp_path / "my-backups"

    assert cli.main(["saves", "backup", "--backup-root", str(custom)]) == 0
    assert calls == [custom]


def test_saves_seed_modded_dispatches_with_from_and_to(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_config", lambda: make_config(tmp_path))
    calls = []
    monkeypatch.setattr(
        cli,
        "cmd_saves_seed_modded",
        lambda backup_root, source, target: calls.append((backup_root, source, target)) or 0,
    )

    assert cli.main(["saves", "seed-modded", "--from", "1", "--to", "3"]) == 0
    assert calls == [(cli.DEFAULT_BACKUP_ROOT, 1, 3)]


def test_saves_restore_dispatches_with_the_path(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_config", lambda: make_config(tmp_path))
    calls = []
    monkeypatch.setattr(cli, "cmd_saves_restore", lambda path: calls.append(path) or 0)
    backup_path = tmp_path / "some-backup"

    assert cli.main(["saves", "restore", str(backup_path)]) == 0
    assert calls == [backup_path]


def test_mod_install_dispatches_with_the_default_repo(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    calls = []
    monkeypatch.setattr(cli, "cmd_mod_install", lambda cfg, repo: calls.append((cfg, repo)) or 0)

    assert cli.main(["mod", "install"]) == 0
    assert calls == [(config, cli.DEFAULT_MOD_REPO)]


def test_mod_install_dispatches_with_a_custom_repo(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    calls = []
    monkeypatch.setattr(cli, "cmd_mod_install", lambda cfg, repo: calls.append((cfg, repo)) or 0)
    custom_repo = tmp_path / "STS2MCP-fork"

    assert cli.main(["mod", "install", "--repo", str(custom_repo)]) == 0
    assert calls == [(config, custom_repo)]


def test_mod_enable_dispatches_with_the_default_backup_root(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_config", lambda: make_config(tmp_path))
    calls = []
    monkeypatch.setattr(cli, "cmd_mod_enable", lambda backup_root: calls.append(backup_root) or 0)

    assert cli.main(["mod", "enable"]) == 0
    assert calls == [cli.DEFAULT_BACKUP_ROOT]


# --- saves/mod commands print what they did, including backup paths ----------


def make_save_tree(root):
    steam = root / "steam" / "76561198000000000"
    saves = steam / "profile1" / "saves"
    saves.mkdir(parents=True)
    (saves / "progress.save").write_text('{"unlocks": "full"}')
    (saves / "prefs.save").write_text('{"fast_mode": true}')
    return steam


def test_cmd_saves_backup_prints_the_backup_path(tmp_path, monkeypatch, capsys):
    steam = make_save_tree(tmp_path)
    monkeypatch.setattr(cli, "SAVE_ROOT", tmp_path / "steam")

    result = cli.cmd_saves_backup(tmp_path / "backups", is_running=never_running)

    assert result == 0
    out = capsys.readouterr().out
    assert str(steam) in out
    assert "backups" in out
    dest_candidates = list((tmp_path / "backups").iterdir())
    assert len(dest_candidates) == 1
    assert str(dest_candidates[0]) in out


def test_cmd_saves_seed_modded_prints_the_backup_and_written_paths(tmp_path, monkeypatch, capsys):
    make_save_tree(tmp_path)
    monkeypatch.setattr(cli, "SAVE_ROOT", tmp_path / "steam")

    result = cli.cmd_saves_seed_modded(
        tmp_path / "backups", 1, 1, is_running=never_running
    )

    assert result == 0
    out = capsys.readouterr().out
    assert "backups" in out
    assert "progress.save" in out


def test_cmd_saves_restore_prints_the_restore_and_replaced_paths(tmp_path, monkeypatch, capsys):
    steam = make_save_tree(tmp_path)
    monkeypatch.setattr(cli, "SAVE_ROOT", tmp_path / "steam")
    backup_path = backup(steam, tmp_path / "backups", is_running=never_running)

    result = cli.cmd_saves_restore(backup_path, is_running=never_running)

    assert result == 0
    out = capsys.readouterr().out
    assert str(backup_path) in out
    assert str(steam) in out


def test_cmd_mod_enable_prints_the_backup_path(tmp_path, monkeypatch, capsys):
    steam = make_save_tree(tmp_path)
    shutil.copy2(FIXTURES / "settings.save", steam / "settings.save")
    monkeypatch.setattr(cli, "SAVE_ROOT", tmp_path / "steam")

    result = cli.cmd_mod_enable(tmp_path / "backups", is_running=never_running)

    assert result == 0
    out = capsys.readouterr().out
    assert "STS2_MCP" in out
    assert "backups" in out


def test_cmd_mod_install_prints_what_it_did(tmp_path, capsys):
    repo = tmp_path / "STS2MCP"
    repo.mkdir()
    shutil.copy2(FIXTURES / "mod_manifest.json", repo / "mod_manifest.json")
    mods_dir = tmp_path / "mods"
    config = make_config(tmp_path, mods_dir=mods_dir)

    def fake_run(cmd, **kwargs):
        dll_dir = repo / "out" / "STS2_MCP"
        dll_dir.mkdir(parents=True, exist_ok=True)
        (dll_dir / "STS2_MCP.dll").write_bytes(b"dll-bytes")
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout="Build succeeded.\n", stderr=""
        )

    result = cli.cmd_mod_install(config, repo, run=fake_run, is_running=never_running)

    assert result == 0
    out = capsys.readouterr().out
    assert "installed" in out.lower()
    assert (mods_dir / "STS2_MCP.dll").is_file()


# --- doctor: ✗ lines when checks fail, ✓ lines when they pass -----------------


class FakeDoctorClient:
    def __init__(self, *, hello_error=None, get_state_error=None, payload=None):
        self._hello_error = hello_error
        self._get_state_error = get_state_error
        self._payload = payload or {}
        self.closed = False

    def hello(self):
        if self._hello_error:
            raise self._hello_error
        return {"status": "ok"}

    def get_state(self):
        if self._get_state_error:
            raise self._get_state_error
        return self._payload

    def close(self):
        self.closed = True


def matching_payload():
    return json.loads((FIXTURES / "state_monster_player_turn.json").read_text())


def test_doctor_reports_a_missing_game_dir(tmp_path):
    config = make_config(tmp_path)
    checks = cli._doctor_checks(
        config,
        game_dir=tmp_path / "no-such-game",
        release_info=tmp_path / "no-release-info.json",
        save_root=tmp_path / "no-saves",
        is_running=never_running,
        client_factory=lambda base_url, token: FakeDoctorClient(
            hello_error=BridgeUnavailable("could not reach the mod")
        ),
    )
    lines = [line for _, line in checks]
    expected = f"✗ game dir exists: {tmp_path / 'no-such-game'} not found"
    assert any(line == expected for line in lines)
    assert not all(ok for ok, _ in checks)


def test_doctor_reports_missing_mod_files_and_missing_token(tmp_path):
    config = make_config(tmp_path)  # mods_dir doesn't exist at all
    checks = cli._doctor_checks(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "no-saves",
        is_running=never_running,
        client_factory=lambda base_url, token: FakeDoctorClient(
            hello_error=BridgeUnavailable("gone")
        ),
    )
    lines = [line for _, line in checks]
    assert any(line.startswith("✗ mod files are installed") for line in lines)
    assert "✗ token: STS2_MCP.conf not found" in lines


def test_doctor_reports_a_corrupt_conf_distinctly_from_missing(tmp_path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    (mods_dir / "STS2_MCP.conf").write_text("{not valid json", encoding="utf-8")
    config = make_config(tmp_path, mods_dir=mods_dir)

    checks = cli._doctor_checks(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "no-saves",
        is_running=never_running,
        client_factory=lambda base_url, token: FakeDoctorClient(
            hello_error=BridgeUnavailable("gone")
        ),
    )
    lines = [line for _, line in checks]
    assert "✗ token: STS2_MCP.conf is not valid JSON" in lines


def test_doctor_reports_mod_not_enabled_in_settings(tmp_path):
    steam = tmp_path / "saves" / "76561198000000000"
    steam.mkdir(parents=True)
    (steam / "settings.save").write_text(
        json.dumps({"mod_settings": {"mods_enabled": False, "mod_list": []}})
    )
    config = make_config(tmp_path)

    checks = cli._doctor_checks(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "saves",
        is_running=never_running,
        client_factory=lambda base_url, token: FakeDoctorClient(
            hello_error=BridgeUnavailable("gone")
        ),
    )
    lines = [line for _, line in checks]
    assert any(line.startswith("✗ mod is enabled in settings.save") for line in lines)


def test_doctor_reports_the_game_not_running(tmp_path):
    config = make_config(tmp_path)
    checks = cli._doctor_checks(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "no-saves",
        is_running=lambda: False,
        client_factory=lambda base_url, token: FakeDoctorClient(
            hello_error=BridgeUnavailable("gone")
        ),
    )
    assert ("✗ game is running: Slay the Spire 2 is not running") in [line for _, line in checks]


def test_doctor_reports_hello_unreachable_and_skips_downstream_checks(tmp_path):
    config = make_config(tmp_path)
    checks = cli._doctor_checks(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "no-saves",
        is_running=never_running,
        client_factory=lambda base_url, token: FakeDoctorClient(
            hello_error=BridgeUnavailable("could not reach the mod")
        ),
    )
    lines = [line for _, line in checks]
    assert any(line.startswith("✗ hello reachable") for line in lines)
    assert any(line.startswith("✗ auth accepted") for line in lines)
    assert any(line.startswith("✗ versions ok") for line in lines)


def test_doctor_reports_auth_rejected(tmp_path):
    config = make_config(tmp_path)
    checks = cli._doctor_checks(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "no-saves",
        is_running=never_running,
        client_factory=lambda base_url, token: FakeDoctorClient(
            get_state_error=BridgeAuthError("token rejected: reinstall the mod")
        ),
    )
    lines = [line for _, line in checks]
    assert "✗ auth accepted: token rejected: reinstall the mod" in lines


def test_doctor_reports_a_version_mismatch(tmp_path):
    config = make_config(tmp_path)
    payload = matching_payload()
    payload["schema_version"] = 1
    checks = cli._doctor_checks(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "no-saves",
        is_running=never_running,
        client_factory=lambda base_url, token: FakeDoctorClient(payload=payload),
    )
    lines = [line for _, line in checks]
    assert any(line.startswith("✗ versions ok") and "schema_version" in line for line in lines)


def _healthy_doctor_setup(tmp_path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    (mods_dir / "STS2_MCP.dll").write_bytes(b"x")
    (mods_dir / "STS2_MCP.json").write_text("{}")
    (mods_dir / "STS2_MCP.conf").write_text(json.dumps({"port": 15526, "token": "abc123"}))
    steam = tmp_path / "saves" / "76561198000000000"
    steam.mkdir(parents=True)
    (steam / "settings.save").write_text(
        json.dumps(
            {
                "mod_settings": {
                    "mods_enabled": True,
                    "mod_list": [{"id": "STS2_MCP", "is_enabled": True, "source": "x"}],
                }
            }
        )
    )
    return make_config(tmp_path, mods_dir=mods_dir)


def test_doctor_all_checks_pass_and_cmd_doctor_exits_0(tmp_path):
    config = _healthy_doctor_setup(tmp_path)

    checks = cli._doctor_checks(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "saves",
        is_running=lambda: True,
        client_factory=lambda base_url, token: FakeDoctorClient(payload=matching_payload()),
    )
    assert all(ok for ok, _ in checks), [line for _, line in checks]


def test_cmd_doctor_prints_every_line_and_exits_0_when_all_checks_pass(tmp_path, capsys):
    config = _healthy_doctor_setup(tmp_path)

    result = cli.cmd_doctor(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "saves",
        is_running=lambda: True,
        client_factory=lambda base_url, token: FakeDoctorClient(payload=matching_payload()),
    )

    out_lines = capsys.readouterr().out.splitlines()
    assert result == 0
    assert len(out_lines) == 9
    assert all(line.startswith("✓") for line in out_lines)


def test_cmd_doctor_prints_x_lines_and_exits_1_when_a_check_fails(tmp_path, capsys):
    config = _healthy_doctor_setup(tmp_path)

    result = cli.cmd_doctor(
        config,
        game_dir=tmp_path / "does-not-exist",  # this one check now fails
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "saves",
        is_running=lambda: True,
        client_factory=lambda base_url, token: FakeDoctorClient(payload=matching_payload()),
    )

    out_lines = capsys.readouterr().out.splitlines()
    assert result == 1
    assert any(line.startswith("✗ game dir exists") for line in out_lines)
    # Every other check is unaffected and still passes.
    assert sum(line.startswith("✓") for line in out_lines) == 8


# --- addendum: one-line errors at the CLI boundary, never a traceback --------


def assert_one_clean_line(capsys_result, expected):
    assert capsys_result.err.strip() == expected
    assert "Traceback" not in capsys_result.err
    assert len(capsys_result.err.strip().splitlines()) == 1


def test_bridge_error_at_the_boundary_is_one_line_and_exit_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SPIRE_COACH_MODS_DIR", str(tmp_path / "mods"))
    monkeypatch.setenv("SPIRE_COACH_DATA_DIR", str(tmp_path / "data"))

    def raise_bridge_error(*a, **k):
        raise BridgeError("mod sent a non-JSON response (200 from /): check the mod is running")

    monkeypatch.setattr(cli, "capture_loop", raise_bridge_error)

    result = cli.main(["capture"])

    assert result == 1
    assert_one_clean_line(
        capsys.readouterr(),
        "mod sent a non-JSON response (200 from /): check the mod is running",
    )


def test_bridge_auth_error_at_the_boundary_is_one_line_and_exit_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SPIRE_COACH_MODS_DIR", str(tmp_path / "mods"))
    monkeypatch.setenv("SPIRE_COACH_DATA_DIR", str(tmp_path / "data"))

    def raise_auth_error(*a, **k):
        raise BridgeAuthError("token rejected: reinstall the mod or check STS2_MCP.conf")

    monkeypatch.setattr(cli, "capture_loop", raise_auth_error)

    result = cli.main(["capture"])

    assert result == 2
    assert_one_clean_line(
        capsys.readouterr(), "token rejected: reinstall the mod or check STS2_MCP.conf"
    )


def test_capture_auth_error_via_stats_exits_2_without_raising(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SPIRE_COACH_MODS_DIR", str(tmp_path / "mods"))
    monkeypatch.setenv("SPIRE_COACH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        cli, "capture_loop", lambda *a, **k: CaptureStats(records=3, actions=1, errors=1)
    )

    result = cli.main(["capture"])

    assert result == 2
    assert "Traceback" not in capsys.readouterr().err


def test_bridge_unavailable_at_the_boundary_is_one_line_and_exit_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SPIRE_COACH_MODS_DIR", str(tmp_path / "mods"))
    monkeypatch.setenv("SPIRE_COACH_DATA_DIR", str(tmp_path / "data"))

    def raise_unavailable(*a, **k):
        raise BridgeUnavailable("could not reach the mod: is the game running with the mod loaded?")

    monkeypatch.setattr(cli, "capture_loop", raise_unavailable)

    result = cli.main(["capture"])

    assert result == 1
    assert_one_clean_line(
        capsys.readouterr(),
        "could not reach the mod: is the game running with the mod loaded?",
    )


def test_keyboard_interrupt_during_capture_stops_cleanly(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SPIRE_COACH_MODS_DIR", str(tmp_path / "mods"))
    monkeypatch.setenv("SPIRE_COACH_DATA_DIR", str(tmp_path / "data"))

    def raise_keyboard_interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "capture_loop", raise_keyboard_interrupt)

    result = cli.main(["capture"])

    assert result == 0
    assert "Traceback" not in capsys.readouterr().err


def test_save_error_at_the_boundary_is_one_line_and_exit_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: make_config(tmp_path))
    monkeypatch.setattr(cli, "find_steam_dir", lambda root: tmp_path / "steam-dir")

    def raise_save_error(*a, **k):
        raise SaveError("Slay the Spire 2 is running; close the game first")

    monkeypatch.setattr(cli, "backup", raise_save_error)

    result = cli.main(["saves", "backup"])

    assert result == 1
    assert_one_clean_line(
        capsys.readouterr(), "Slay the Spire 2 is running; close the game first"
    )


def test_install_error_at_the_boundary_is_one_line_and_exit_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: make_config(tmp_path))
    repo = tmp_path / "STS2MCP"
    repo.mkdir()

    def raise_install_error(*a, **k):
        raise InstallError("Slay the Spire 2 is running; close the game first")

    monkeypatch.setattr(cli, "build_mod", raise_install_error)

    result = cli.main(["mod", "install", "--repo", str(repo)])

    assert result == 1
    assert_one_clean_line(capsys.readouterr(), "Slay the Spire 2 is running; close the game first")


def test_os_error_at_the_boundary_names_the_operation_strerror_and_path(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cli, "load_config", lambda: make_config(tmp_path))
    monkeypatch.setattr(cli, "find_steam_dir", lambda root: tmp_path / "steam-dir")

    def raise_os_error(*a, **k):
        raise OSError(errno.EACCES, "Permission denied", "/fake/steam-dir")

    monkeypatch.setattr(cli, "backup", raise_os_error)

    result = cli.main(["saves", "backup"])

    assert result == 1
    assert_one_clean_line(capsys.readouterr(), "backup failed: Permission denied (/fake/steam-dir)")


def test_config_value_error_at_the_boundary_is_one_line_and_exit_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SPIRE_COACH_MODS_DIR", str(tmp_path))
    monkeypatch.setenv("SPIRE_COACH_POLL_S", "abc")

    result = cli.main(["doctor"])

    assert result == 1
    assert_one_clean_line(
        capsys.readouterr(), "invalid setting SPIRE_COACH_POLL_S=abc: must be a number"
    )
