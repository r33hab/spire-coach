import contextlib
import copy
import errno
import functools
import io
import json
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import httpx
import pytest
from websockets.sync.client import connect

import coach.cli as cli
from coach.bridge.client import BridgeAuthError, BridgeError, BridgeUnavailable
from coach.bridge.versions import VersionStatus
from coach.capture import CaptureStats
from coach.config import Config
from coach.engine import EngineClient
from coach.live import CoachSettings, engine_health_problems
from coach.modinstall import InstallError
from coach.saves import SaveError, backup
from coach.view import CoachState

FIXTURES = Path(__file__).parent / "fixtures" / "mod"
FAKE_ENGINE = Path(__file__).parent / "fakes" / "fake_engine_service.py"
# How long a test waits on the running coach before it gives up. Generous on purpose:
# it only ever runs out on a broken test, while a loaded machine can be slow.
UNDER_LOAD_S = 60.0


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


def test_autopilot_dispatches_with_the_defaults(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "cmd_autopilot", lambda cfg, **kwargs: calls.append(kwargs) or 0)

    assert cli.main(["autopilot"]) == 0
    assert calls[0]["restart_on_jam"] is False


def test_autopilot_dispatches_with_restart_on_jam(monkeypatch, tmp_path):
    # Bug 3/7: the opt-in flag must actually reach cmd_autopilot/autopilot.run.
    config = make_config(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "cmd_autopilot", lambda cfg, **kwargs: calls.append(kwargs) or 0)

    assert cli.main(["autopilot", "--restart-on-jam"]) == 0
    assert calls[0]["restart_on_jam"] is True


def test_autopilot_dispatches_with_auto_kill_defaults(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "cmd_autopilot", lambda cfg, **kwargs: calls.append(kwargs) or 0)

    assert cli.main(["autopilot"]) == 0
    assert calls[0]["auto_kill"] is True
    assert calls[0]["auto_kill_rounds"] == cli.autoplay.DEFAULT_AUTO_KILL_NO_PROGRESS_ROUNDS


def test_autopilot_dispatches_with_auto_kill_overridden(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "cmd_autopilot", lambda cfg, **kwargs: calls.append(kwargs) or 0)

    assert cli.main(["autopilot", "--no-auto-kill", "--auto-kill-rounds", "3"]) == 0
    assert calls[0]["auto_kill"] is False
    assert calls[0]["auto_kill_rounds"] == 3


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


def test_fidelity_scoreboard_dispatches_with_the_defaults(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    calls = []
    monkeypatch.setattr(
        cli, "cmd_fidelity_scoreboard", lambda cfg, **kw: calls.append((cfg, kw)) or 0
    )

    assert cli.main(["fidelity-scoreboard"]) == 0
    assert calls == [
        (
            config,
            {
                "sts2_repo": cli.fidelity_scoreboard.DEFAULT_STS2_REPO,
                "runs_dir": None,
                "out_dir": None,
                "resamples": None,
            },
        )
    ]


def test_fidelity_scoreboard_dispatches_with_every_option_given(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    calls = []
    monkeypatch.setattr(
        cli, "cmd_fidelity_scoreboard", lambda cfg, **kw: calls.append((cfg, kw)) or 0
    )
    sts2_repo = tmp_path / "sts2-fork"
    runs_dir = tmp_path / "runs"
    out_dir = tmp_path / "out"

    assert (
        cli.main(
            [
                "fidelity-scoreboard",
                "--sts2-repo",
                str(sts2_repo),
                "--runs-dir",
                str(runs_dir),
                "--out-dir",
                str(out_dir),
                "--resamples",
                "32",
            ]
        )
        == 0
    )
    assert calls == [
        (
            config,
            {"sts2_repo": sts2_repo, "runs_dir": runs_dir, "out_dir": out_dir, "resamples": 32},
        )
    ]


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

    result = cli.cmd_saves_seed_modded(tmp_path / "backups", 1, 1, is_running=never_running)

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


def test_doctor_survives_client_factory_itself_raising(tmp_path):
    # A bad SPIRE_COACH_BASE_URL makes the real ModClient's constructor raise
    # ValueError. That must not abort the whole doctor run: the first 6 checks
    # (computed before any client is built) still run and print, and the last
    # 3 become clear ✗ lines instead of an uncaught exception. Uses the real
    # default client_factory (ModClient) on purpose, not a fake.
    config = make_config(tmp_path, base_url="http://example.com:15526")

    checks = cli._doctor_checks(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "no-saves",
        is_running=never_running,
    )

    assert len(checks) == 9
    lines = [line for _, line in checks]
    assert lines[0] == "✓ game dir exists"  # earlier checks still ran
    assert any(line.startswith("✗ hello reachable: could not create a client") for line in lines)
    assert lines[-2] == "✗ auth accepted: not checked (hello unreachable)"
    assert lines[-1] == "✗ versions ok: not checked (hello unreachable)"
    assert not all(ok for ok, _ in checks)


def test_cmd_doctor_survives_client_factory_itself_raising_and_exits_1(tmp_path, capsys):
    config = make_config(tmp_path, base_url="http://example.com:15526")

    result = cli.cmd_doctor(
        config,
        game_dir=tmp_path,
        release_info=FIXTURES / "release_info.json",
        save_root=tmp_path / "no-saves",
        is_running=never_running,
    )

    out_lines = capsys.readouterr().out.splitlines()
    assert result == 1
    assert len(out_lines) == 9
    assert "Traceback" not in "\n".join(out_lines)


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
        cli,
        "capture_loop",
        lambda *a, **k: CaptureStats(
            records=3, actions=1, errors=1, error_message="token rejected: x"
        ),
    )

    result = cli.main(["capture"])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.err.strip() == "token rejected: x"
    assert "token rejected: x" not in captured.out
    assert "Traceback" not in captured.err


class FakeAuthFailingClient:
    """A minimal ModClient stand-in whose first get_state() rejects the token."""

    def __init__(self):
        self.closed = False

    def get_state(self):
        raise BridgeAuthError("token rejected: reinstall the mod or check STS2_MCP.conf")

    def actions_since(self, since):
        return {"status": "ok", "latest_seq": since, "entries": []}

    def close(self):
        self.closed = True


def test_capture_auth_error_through_the_real_capture_loop_goes_to_stderr_not_stdout(
    monkeypatch, tmp_path, capsys
):
    # Exercises the REAL capture_loop (not monkeypatched) through cmd_capture/main,
    # only faking the ModClient it talks to -- proves the addendum's stderr rule
    # holds on the realistic path, not just the defensive uncaught-exception path
    # covered by test_bridge_auth_error_at_the_boundary_is_one_line_and_exit_2 above.
    monkeypatch.setenv("SPIRE_COACH_MODS_DIR", str(tmp_path / "mods"))
    monkeypatch.setenv("SPIRE_COACH_DATA_DIR", str(tmp_path / "data"))
    fake_client = FakeAuthFailingClient()
    monkeypatch.setattr(cli, "ModClient", lambda base_url, token: fake_client)

    result = cli.main(["capture"])

    captured = capsys.readouterr()
    assert result == 2
    assert "token rejected: reinstall the mod or check STS2_MCP.conf" in captured.err
    assert "token rejected" not in captured.out
    assert "Traceback" not in captured.err
    assert fake_client.closed is True


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
    assert_one_clean_line(capsys.readouterr(), "Slay the Spire 2 is running; close the game first")


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


# --- coach: argument parsing ------------------------------------------------------------

COACH_DEFAULTS = {
    "workers": 4,
    "budget_s": 1.5,
    "port": 15530,
    "window": True,
    "mode": "show",
    "prior": None,
}


def record_coach_calls(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    calls = []
    monkeypatch.setattr(cli, "cmd_coach", lambda cfg, **options: calls.append((cfg, options)) or 0)
    return config, calls


def test_coach_dispatches_with_the_defaults(monkeypatch, tmp_path):
    config, calls = record_coach_calls(monkeypatch, tmp_path)

    assert cli.main(["coach"]) == 0
    assert calls == [(config, COACH_DEFAULTS)]


def test_coach_dispatches_with_every_option_given(monkeypatch, tmp_path):
    config, calls = record_coach_calls(monkeypatch, tmp_path)
    argv = ["coach", "--workers", "6", "--budget", "2.5", "--port", "15600"]

    assert cli.main([*argv, "--no-window", "--mode", "quiz", "--no-prior"]) == 0
    assert calls == [
        (
            config,
            {
                "workers": 6,
                "budget_s": 2.5,
                "port": 15600,
                "window": False,
                "mode": "quiz",
                "prior": False,
            },
        )
    ]


def test_coach_passes_prior_through(monkeypatch, tmp_path):
    config, calls = record_coach_calls(monkeypatch, tmp_path)

    assert cli.main(["coach", "--prior"]) == 0
    assert calls == [(config, {**COACH_DEFAULTS, "prior": True})]


def export_prior(config):
    folder = cli.combat_prior_dir(config)
    folder.mkdir(parents=True)
    (folder / "model.json").write_text("{}", encoding="utf-8")
    (folder / "combat_policy.pt").write_bytes(b"weights")
    return folder


def test_the_prior_is_off_by_default_and_on_with_prior(tmp_path):
    config = make_config(tmp_path)
    assert cli.combat_prior_dir(config) == tmp_path / "data" / "models" / "combat_prior"
    assert cli.DEFAULT_PRIOR is False  # it did not help at the coach's budget (E2)
    assert cli._prior_dir(config, None) is None
    folder = export_prior(config)
    assert cli._prior_dir(config, None) is None  # exported, but off by default
    assert cli._prior_dir(config, True) == folder  # --prior
    assert cli._prior_dir(config, False) is None  # --no-prior


def test_an_automatic_default_follows_the_export(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_PRIOR", None)
    assert cli._prior_dir(config, None) is None  # nothing exported: the plain search
    folder = export_prior(config)
    assert cli._prior_dir(config, None) == folder
    assert cli._prior_dir(config, False) is None


def test_prior_without_an_exported_prior_is_refused(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(cli.CoachError, match="no combat prior is exported to"):
        cli._prior_dir(config, True)


def test_the_coach_starts_the_engine_with_the_exported_prior(monkeypatch, tmp_path, capsys):
    config = make_config(tmp_path)
    folder = export_prior(config)
    made = []

    class Stop(Exception):
        pass

    def engine(cmd=None, **kwargs):
        made.append(kwargs)
        raise Stop

    monkeypatch.setattr(cli, "EngineClient", engine)
    for prior in (True, None, False):
        with pytest.raises(Stop):
            cli.cmd_coach(config, prior=prior, client_factory=lambda url, token: FakeMod())
    assert [kwargs["prior_dir"] for kwargs in made] == [folder, None, None]


@pytest.mark.parametrize(
    "option, value, problem",
    [
        ("--workers", "0", "must be at least 1, got 0"),
        ("--workers", "two", "'two' is not a whole number"),
        ("--budget", "0", "must be a positive number of seconds, got 0"),
        ("--budget", "-1.5", "must be a positive number of seconds, got -1.5"),
        ("--budget", "nan", "must be a positive number of seconds, got nan"),
        ("--budget", "soon", "'soon' is not a number"),
        ("--port", "0", "must be a port from 1 to 65535, got 0"),
        ("--port", "65536", "must be a port from 1 to 65535, got 65536"),
        ("--mode", "hard", "invalid choice: 'hard'"),
    ],
)
def test_coach_refuses_a_bad_option_before_starting_anything(
    monkeypatch, tmp_path, capsys, option, value, problem
):
    _, calls = record_coach_calls(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exited:
        cli.main(["coach", option, value])

    assert exited.value.code == 2
    error = capsys.readouterr().err.strip().splitlines()[-1]
    assert error.startswith(f"spire-coach coach: error: argument {option}: ")
    assert problem in error
    assert calls == []


def test_a_coach_that_cannot_start_says_why_in_one_line_and_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: make_config(tmp_path))

    problem = "The coach panel could not listen on 127.0.0.1:15530."

    def cannot_start(config, **options):
        raise cli.CoachError(problem)

    monkeypatch.setattr(cli, "cmd_coach", cannot_start)

    assert cli.main(["coach"]) == 1
    assert_one_clean_line(capsys.readouterr(), problem)


# --- coach: the engine service's health check ---------------------------------------------


def fake_engine_cmd(tmp_path, control=None):
    control_path = tmp_path / "engine-control.json"
    control_path.write_text(json.dumps(control or {}), encoding="utf-8")
    return [sys.executable, str(FAKE_ENGINE), str(control_path)]


def engine_problems(cmd):
    engine = EngineClient(cmd, request_timeout_s=5.0)
    try:
        return engine_health_problems(engine)
    finally:
        engine.close()


def test_a_healthy_engine_that_reports_its_versions_has_no_problem(tmp_path):
    assert engine_problems(fake_engine_cmd(tmp_path)) == []


def test_an_engine_that_reports_no_native_api_version_is_a_problem(tmp_path):
    health = {"service_version": "old", "game_version_target": "v0.107.1"}

    problems = engine_problems(fake_engine_cmd(tmp_path, {"health": health}))

    assert problems == ["the engine service did not report its native API version"]


def test_an_engine_built_for_another_game_version_is_a_problem(tmp_path):
    health = {"native_api_version": 1, "game_version_target": "v0.111.0"}

    problems = engine_problems(fake_engine_cmd(tmp_path, {"health": health}))

    assert problems == ["the engine service targets game version v0.111.0, not v0.107.1"]


@pytest.mark.parametrize(
    "control, reason",
    [
        (
            {"ok_false": {"type": "internal", "message": "no native\nlibrary"}},
            "engine returned internal: no native library",
        ),
        (
            {"crash_always": True},  # it dies at every request, a restarted one too
            "engine service unavailable after a restart: the engine service closed its output",
        ),
    ],
)
def test_an_engine_that_fails_its_health_check_is_a_problem_named_in_one_line(
    tmp_path, control, reason
):
    cmd = fake_engine_cmd(tmp_path, control)

    assert engine_problems(cmd) == [f"the engine service failed its health check ({reason})"]


def test_an_engine_service_that_cannot_start_is_a_problem(tmp_path):
    [problem] = engine_problems([str(tmp_path / "no-such-engine-service")])

    assert problem.startswith(
        "the engine service failed its health check (could not start the engine service:"
    )
    assert "\n" not in problem


# --- coach: the panel's state, kept before it is pushed -------------------------------------


def coach_state(status: str) -> CoachState:
    return CoachState(
        status=status,
        status_text=f"{status}.",
        recommendation=None,
        last_grade=None,
        mode="show",
        versions=VersionStatus(ok=True, problems=[]),
        run={"act": 1, "floor": 6},
    )


def test_the_panel_feed_keeps_each_state_before_the_broadcaster_pushes_it():
    # coach/server.py's contract: a panel that connects while a state is published
    # then reads it as its first state, or has it pushed; never neither.
    first, second = coach_state("thinking"), coach_state("advice")
    pushed = []

    class Broadcaster:
        def publish(self, state):
            pushed.append((state, feed.state()))  # what a panel connecting now would read

    feed = cli._PanelFeed(first, Broadcaster())
    assert feed.state() is first

    feed.publish(second)

    assert pushed == [(second, second)]
    assert feed.state() is second


# --- coach: the whole command, with a fake mod and a fake engine service -------------------


class FakeMod:
    """The mod as the coach sees it: a monster fight on screen and no moves logged."""

    def __init__(self):
        self.state = json.loads((FIXTURES / "combat_full.json").read_text(encoding="utf-8"))
        self.closed = False

    def get_state(self):
        return copy.deepcopy(self.state)

    def actions_since(self, since):
        return {"status": "ok", "latest_seq": self.state["action_log_seq"], "entries": []}

    def close(self):
        self.closed = True


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def until(condition, timeout=UNDER_LOAD_S) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        with contextlib.suppress(httpx.HTTPError):
            if condition():
                return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)


def never_ctrl_c():
    pytest.fail("the coach waited for Ctrl-C")


class CoachRun:
    """One `spire-coach coach` against fakes: the mod, the window and the user's Ctrl-C
    are stand-ins, while the engine service is a real child process running the fake
    service script and the panel's server really listens on 127.0.0.1."""

    def __init__(self, tmp_path, engine_control=None, **config):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self.mod = FakeMod()
        self.config = make_config(tmp_path, poll_interval_s=0.02, **config)
        self.requests = tmp_path / "engine-requests.jsonl"
        self.engine_cmd = fake_engine_cmd(tmp_path)
        self.control = self.engine_cmd[-1]  # in the command line of every service it runs
        self.set_engine_control(**(engine_control or {}))
        self.steps: list[str] = []  # what the stand-ins for the window and Ctrl-C saw happen
        self.stops: list[str] = []  # the session, the engine and the server, as they stopped
        self._threads_before = set(threading.enumerate())

    def set_engine_control(self, **control):
        """What the fake engine service does from its next request on."""
        control = {"log_requests_to": str(self.requests), **control}
        Path(self.control).write_text(json.dumps(control), encoding="utf-8")

    def __call__(self, *, retry_port_race=True, **options):
        try:
            return self._run(**options)
        except cli.CoachError:
            if not retry_port_race:
                raise
            # The free port found for this run was taken meanwhile (by another test run,
            # say): once more, on a new one.
            self.port = free_port()
            self.url = f"http://127.0.0.1:{self.port}/"
            self.mod = FakeMod()
            self.stops.clear()
            self.requests.unlink(missing_ok=True)
            return self._run(**options)

    def _run(self, **options):
        options = {"engine_cmd": self.engine_cmd, "port": self.port, **options}
        with self.recording_the_stops():
            return cli.cmd_coach(self.config, client_factory=lambda url, token: self.mod, **options)

    @contextlib.contextmanager
    def recording_the_stops(self):
        """Thin wrappers note when the session, the engine and the server stop."""
        stops = self.stops
        make_engine, make_server = cli.EngineClient, cli.create_server

        class Session(cli.CoachSession):
            def close(self):
                ended = super().close()
                stops.append("session closed")
                return ended

        def engine(cmd, *, workers, **kwargs):
            client = make_engine(cmd, workers=workers, **kwargs)
            close = client.close

            def closed():
                close()
                stops.append("engine closed")

            client.close = closed
            return client

        def server(app, *, port):
            made = make_server(app, port=port)
            run = made.run

            def ran(*args, **kwargs):
                try:
                    return run(*args, **kwargs)
                finally:
                    stops.append("server stopped")

            made.run = ran
            return made

        with (
            mock.patch.object(cli, "CoachSession", Session),
            mock.patch.object(cli, "EngineClient", engine),
            mock.patch.object(cli, "create_server", server),
        ):
            yield

    def state(self) -> dict:
        return httpx.get(self.url + "state", timeout=5.0).json()

    def shows(self, status: str) -> bool:
        return until(lambda: self.state()["status"] == status)

    def engine_requests(self) -> list[dict]:
        lines = self.requests.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def engine_services(self) -> list[str]:
        """The engine services of this run still running, as `ps` lists them."""
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,command="], capture_output=True, text=True, check=True
        ).stdout
        return [line for line in listing.splitlines() if self.control in line]

    def new_threads(self) -> list[str]:
        """The threads started since this run began that are still alive."""
        return sorted(
            thread.name for thread in threading.enumerate() if thread not in self._threads_before
        )

    def assert_all_stopped(self, order=("session closed", "engine closed", "server stopped")):
        assert self.engine_services() == [], "an engine service is still running"
        assert self.new_threads() == [], "a thread the coach started is still running"
        with pytest.raises(httpx.ConnectError):
            self.state()  # the panel's server no longer listens
        assert self.mod.closed
        assert self.stops == list(order)


@pytest.fixture(autouse=True)
def matching_game_version(monkeypatch):
    # The live session reads the installed game's version on every settled state; no
    # test ever reads the real install.
    monkeypatch.setattr("coach.live.read_game_version", lambda: "v0.107.1")


def test_the_coach_serves_advice_until_ctrl_c_then_stops_all_it_started(tmp_path, capsys):
    run = CoachRun(tmp_path)

    def wait_for_ctrl_c():
        run.steps.append("waited for Ctrl-C")
        assert run.shows("advice")
        with connect(f"ws://127.0.0.1:{run.port}/ws") as panel:  # a panel connecting late...
            assert json.loads(panel.recv(timeout=10))["status"] == "advice"  # ...is up to date
        assert len(run.engine_services()) == 1  # the engine service is a child process
        assert {"coach-engine", "coach-server", "coach-session"} <= set(run.new_threads())
        raise KeyboardInterrupt  # the user presses Ctrl-C

    assert run(window=False, wait_for_ctrl_c=wait_for_ctrl_c) == 0

    assert run.steps == ["waited for Ctrl-C"]
    run.assert_all_stopped()
    methods = [request["method"] for request in run.engine_requests()]
    assert methods[0] == "health"  # checked at startup, before any advice
    assert "analyze" in methods
    output = capsys.readouterr()
    assert output.out.splitlines() == [
        "Starting the engine service…",
        f"The coach panel is at {run.url}",
        "Press Ctrl-C to stop the coach.",
        "Stopping the coach…",
    ]
    assert output.err == ""


def test_closing_the_window_stops_the_coach_and_all_it_started(tmp_path):
    run = CoachRun(tmp_path)

    def show_window(url):  # pywebview's window, on the main thread
        run.steps.append(f"opened the window on {url}")
        assert run.shows("advice")
        return True  # the user closed it

    assert run(window=True, show_window=show_window, wait_for_ctrl_c=never_ctrl_c) == 0

    assert run.steps == [f"opened the window on {run.url}"]
    run.assert_all_stopped()


def test_when_the_window_cannot_open_the_coach_serves_the_browser_until_ctrl_c(tmp_path):
    run = CoachRun(tmp_path)

    def show_window(url):
        run.steps.append(f"tried the window on {url}")
        return False  # it could not open: the URL was printed for the browser instead

    def wait_for_ctrl_c():
        run.steps.append("waited for Ctrl-C")
        assert run.shows("advice")  # still served after the fallback
        raise KeyboardInterrupt

    assert run(window=True, show_window=show_window, wait_for_ctrl_c=wait_for_ctrl_c) == 0

    assert run.steps == [f"tried the window on {run.url}", "waited for Ctrl-C"]
    run.assert_all_stopped()


def test_the_options_reach_the_engine_service_and_the_session(tmp_path, monkeypatch):
    run = CoachRun(tmp_path)
    launched = []

    def engine_client(cmd, *, workers, prior_dir=None):
        launched.append((cmd, workers))
        return EngineClient(run.engine_cmd, workers=workers)  # the fake, not the simulator

    monkeypatch.setattr(cli, "EngineClient", engine_client)

    def wait_for_ctrl_c():
        run.steps.append("waited for Ctrl-C")
        assert run.shows("advice")
        assert run.state()["mode"] == "quiz"
        assert run.state()["recommendation"]["best"] is None  # hidden until you move
        raise KeyboardInterrupt

    options = dict(engine_cmd=None, workers=6, budget_s=0.25, mode="quiz", window=False)
    assert run(**options, wait_for_ctrl_c=wait_for_ctrl_c) == 0

    assert run.steps == ["waited for Ctrl-C"]
    assert launched == [(None, 6)]  # the default launch, with 6 search workers
    [analyze] = [r for r in run.engine_requests() if r["method"] == "analyze"]
    assert analyze["params"]["time_budget_s"] == 0.25
    run.assert_all_stopped()


def test_an_unhealthy_engine_keeps_advice_off_until_the_panel_overrides_it(tmp_path, capsys):
    old_service = {"service_version": "old", "game_version_target": "v0.107.1"}
    run = CoachRun(tmp_path, engine_control={"health": old_service})
    reason = "the engine service did not report its native API version"

    def wait_for_ctrl_c():
        run.steps.append("waited for Ctrl-C")
        assert run.shows("advice_off")
        assert reason in run.state()["status_text"]
        override = httpx.post(run.url + "settings", json={"override_versions": True}, timeout=5)
        assert override.status_code == 200
        assert run.shows("advice")
        warnings = run.state()["recommendation"]["warnings"]
        assert f"The version check is overridden: {reason}." in warnings
        raise KeyboardInterrupt

    assert run(window=False, wait_for_ctrl_c=wait_for_ctrl_c) == 0

    assert run.steps == ["waited for Ctrl-C"]
    run.assert_all_stopped()
    assert capsys.readouterr().err.splitlines() == [
        f"Advice is off: {reason}. Turn on the version override in the panel to see advice anyway."
    ]


def test_a_port_already_taken_is_one_line_on_stderr_and_leaves_nothing_running(
    tmp_path, monkeypatch, capsys
):
    run = CoachRun(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: run.config)
    monkeypatch.setattr(cli, "ModClient", lambda base_url, token: run.mod)
    monkeypatch.setattr(
        cli,
        "EngineClient",
        lambda cmd, *, workers, prior_dir=None: EngineClient(run.engine_cmd, workers=workers),
    )
    # Noticed when the server gives up, not when the deadline for it to listen runs out.
    monkeypatch.setattr(cli, "_SERVER_START_S", UNDER_LOAD_S)

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", run.port))
        taken.listen()
        started = time.monotonic()
        with run.recording_the_stops():
            result = cli.main(["coach", "--port", str(run.port), "--no-window"])
        took = time.monotonic() - started

    assert result == 1
    assert capsys.readouterr().err.splitlines() == [  # uvicorn's own error log stays quiet
        f"The coach panel could not listen on 127.0.0.1:{run.port}: is another coach "
        "running? Choose another port with --port."
    ]
    assert took < UNDER_LOAD_S / 2
    # The server gave up at startup; the session's thread never started.
    run.assert_all_stopped(order=("server stopped", "session closed", "engine closed"))


def test_the_first_health_check_waits_for_a_slow_engine_start(tmp_path, monkeypatch):
    # A cold start of the real service (uv, imports, the search workers) can outlast an
    # ordinary request: the startup check waits up to the configured startup timeout.
    run = CoachRun(tmp_path, engine_control={"health_delay_s": 1.0}, engine_start_timeout_s=30.0)
    monkeypatch.setattr(
        cli,
        "EngineClient",
        lambda cmd, *, workers, prior_dir=None: EngineClient(
            cmd, workers=workers, request_timeout_s=0.3
        ),
    )
    waited = []

    def health_check(engine, *, timeout_s=None):
        waited.append(timeout_s)
        return engine_health_problems(engine, timeout_s=timeout_s)

    monkeypatch.setattr(cli, "engine_health_problems", health_check)

    def wait_for_ctrl_c():
        run.steps.append("waited for Ctrl-C")
        # The first verdict, before any later check of the engine could change it.
        assert until(lambda: run.state()["status"] in ("advice", "advice_off"))
        assert run.state()["status"] == "advice"  # not "... did not respond within 0.3s"
        raise KeyboardInterrupt

    assert run(window=False, wait_for_ctrl_c=wait_for_ctrl_c) == 0

    assert run.steps == ["waited for Ctrl-C"]
    assert waited == [30.0]  # the configured startup timeout
    assert [r["method"] for r in run.engine_requests()].count("health") == 1  # answered
    run.assert_all_stopped()


@pytest.mark.parametrize(
    "broken, reason",
    [
        (
            {"health": {"service_version": "old", "game_version_target": "v0.107.1"}},
            "did not report its native API version",
        ),
        ({"crash_always": True}, "failed its health check"),  # the next check starts it anew
    ],
)
def test_a_failing_engine_is_checked_again_and_advice_comes_on_without_a_restart(
    tmp_path, monkeypatch, broken, reason
):
    run = CoachRun(tmp_path, engine_control=broken)
    checked_often = functools.partial(CoachSettings, engine_recheck_s=0.2)
    monkeypatch.setattr(cli, "CoachSettings", checked_often)

    def wait_for_ctrl_c():
        run.steps.append("waited for Ctrl-C")
        assert run.shows("advice_off")
        assert reason in run.state()["status_text"]
        run.set_engine_control()  # the engine is fixed while the coach runs
        assert run.shows("advice")
        raise KeyboardInterrupt

    assert run(window=False, wait_for_ctrl_c=wait_for_ctrl_c) == 0

    assert run.steps == ["waited for Ctrl-C"]
    run.assert_all_stopped()


class TerminalGoneWhileStopping(io.StringIO):
    """A terminal that goes away just as the coach says it is stopping."""

    def write(self, text):
        if "Stopping" in text:
            raise BrokenPipeError(32, "Broken pipe")
        return super().write(text)


def test_a_terminal_gone_away_does_not_keep_the_coach_from_stopping_all(tmp_path, monkeypatch):
    run = CoachRun(tmp_path)
    monkeypatch.setattr(sys, "stdout", TerminalGoneWhileStopping())

    def wait_for_ctrl_c():
        run.steps.append("waited for Ctrl-C")
        assert run.shows("advice")
        raise KeyboardInterrupt

    assert run(window=False, wait_for_ctrl_c=wait_for_ctrl_c) == 0

    assert run.steps == ["waited for Ctrl-C"]
    run.assert_all_stopped()


def test_a_step_of_stopping_that_fails_does_not_skip_the_steps_after_it(tmp_path, monkeypatch):
    run = CoachRun(tmp_path)

    def engine_client(cmd, *, workers, prior_dir=None):
        engine = EngineClient(cmd, workers=workers, prior_dir=prior_dir)
        close = engine.close

        def failing_close():
            close()
            raise OSError("the engine log could not be closed")

        engine.close = failing_close
        return engine

    monkeypatch.setattr(cli, "EngineClient", engine_client)

    def wait_for_ctrl_c():
        run.steps.append("waited for Ctrl-C")
        assert run.shows("advice")
        raise KeyboardInterrupt

    with pytest.raises(OSError, match="engine log could not be closed"):
        run(window=False, wait_for_ctrl_c=wait_for_ctrl_c)

    assert run.steps == ["waited for Ctrl-C"]
    run.assert_all_stopped(order=("session closed", "server stopped"))  # the rest still ran


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_sigterm_and_sighup_stop_the_coach_in_order_as_ctrl_c_does(tmp_path, signum):
    # A plain `kill`, or the terminal closing, must not end the coach halfway.
    run = CoachRun(tmp_path)

    def not_for_the_test(signum, frame):
        raise AssertionError(f"signal {signum} reached the test instead of the coach")

    previous = signal.signal(signum, not_for_the_test)  # else it would end pytest itself
    try:

        def wait_for_ctrl_c():
            run.steps.append("waited")
            assert run.shows("advice")
            signal.raise_signal(signum)
            pytest.fail("the signal did not stop the coach")

        assert run(window=False, wait_for_ctrl_c=wait_for_ctrl_c) == 0
    finally:
        signal.signal(signum, previous)

    assert run.steps == ["waited"]
    run.assert_all_stopped()


def test_deck_rollouts_reach_the_engine_environment():
    from pathlib import Path

    from coach import cli

    env = cli.autopilot_engine_env(deck_rollouts=True, prior_dir=Path("/prior"))
    assert env["STS2_DECK_ROLLOUTS"] == "1"
    assert env["STS2_DECK_ROLLOUT_PRIOR"] == "/prior"
    assert "STS2_STRATEGY_MODEL_DIR" in env
    plain = cli.autopilot_engine_env(deck_rollouts=False, prior_dir=None)
    assert "STS2_DECK_ROLLOUTS" not in plain


def test_the_autopilot_parser_has_deck_rollouts():
    from coach import cli

    args = cli._build_parser().parse_args(["autopilot", "--deck-rollouts"])
    assert args.deck_rollouts is True
    assert cli._build_parser().parse_args(["autopilot"]).deck_rollouts is False
