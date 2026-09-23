import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from coach.modinstall import (
    InstallError,
    build_mod,
    enable_mod_in_settings,
    install_mod,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mod"
NOW = datetime(2026, 9, 24, 12, 0, 0)
STAMP = "20260924-120000"


def never_running():
    return False


def always_running():
    return True


def make_dll(tmp_path, content=b"new-dll-bytes"):
    dll = tmp_path / "build" / "STS2_MCP.dll"
    dll.parent.mkdir(parents=True, exist_ok=True)
    dll.write_bytes(content)
    return dll


def make_manifest(tmp_path):
    manifest = tmp_path / "mod_manifest.json"
    shutil.copy2(FIXTURES / "mod_manifest.json", manifest)
    return manifest


# --- build_mod -------------------------------------------------------------


def test_build_mod_returns_the_dll_path_on_success(tmp_path):
    repo = tmp_path / "STS2MCP"
    repo.mkdir()
    game_dir = tmp_path / "game"
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout="Build succeeded.\n", stderr=""
        )

    result = build_mod(repo, game_dir=game_dir, run=fake_run)

    assert result == repo / "out" / "STS2_MCP" / "STS2_MCP.dll"
    assert captured["cmd"] == [
        "dotnet", "build", "STS2_MCP.csproj",
        "-c", "Release",
        "-o", "out/STS2_MCP",
        f"-p:STS2GameDir={game_dir}",
    ]
    assert captured["kwargs"]["cwd"] == repo


def test_build_mod_raises_install_error_with_output_tail_on_failure(tmp_path):
    repo = tmp_path / "STS2MCP"
    repo.mkdir()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            returncode=1,
            stdout="Build FAILED.\n",
            stderr="McpMod.cs(42,5): error CS1002: ; expected\n",
        )

    with pytest.raises(InstallError, match="CS1002"):
        build_mod(repo, game_dir=tmp_path / "game", run=fake_run)


# --- install_mod -------------------------------------------------------------


def test_fresh_install_creates_the_conf_with_a_64_hex_token(tmp_path):
    mods_dir = tmp_path / "mods"
    dll = make_dll(tmp_path)
    manifest = make_manifest(tmp_path)

    result = install_mod(dll, manifest, mods_dir, now=NOW, is_running=never_running)

    assert result.previous_backup is None
    assert result.token_created is True
    assert (mods_dir / "STS2_MCP.dll").read_bytes() == b"new-dll-bytes"
    assert json.loads((mods_dir / "STS2_MCP.json").read_text()) == json.loads(
        (FIXTURES / "mod_manifest.json").read_text()
    )
    conf = json.loads((mods_dir / "STS2_MCP.conf").read_text())
    assert conf["port"] == 15526
    assert len(conf["token"]) == 64
    assert all(c in "0123456789abcdef" for c in conf["token"])
    mode = (mods_dir / "STS2_MCP.conf").stat().st_mode & 0o777
    assert mode == 0o600


def test_fresh_install_uses_the_given_token_factory(tmp_path):
    mods_dir = tmp_path / "mods"
    dll = make_dll(tmp_path)
    manifest = make_manifest(tmp_path)

    result = install_mod(
        dll, manifest, mods_dir, now=NOW, is_running=never_running,
        token_factory=lambda: "fixed-token",
    )

    assert result.token_created is True
    conf = json.loads((mods_dir / "STS2_MCP.conf").read_text())
    assert conf["token"] == "fixed-token"


def test_reinstall_keeps_the_token_and_backs_up_the_old_files(tmp_path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    (mods_dir / "STS2_MCP.dll").write_bytes(b"old-dll-bytes")
    (mods_dir / "STS2_MCP.json").write_text(json.dumps({"version": "0.3.0"}))
    shutil.copy2(FIXTURES / "STS2_MCP.conf", mods_dir / "STS2_MCP.conf")
    old_conf = json.loads((FIXTURES / "STS2_MCP.conf").read_text())

    dll = make_dll(tmp_path)
    manifest = make_manifest(tmp_path)

    result = install_mod(dll, manifest, mods_dir, now=NOW, is_running=never_running)

    assert result.token_created is False
    assert result.previous_backup == mods_dir / ".previous" / STAMP
    assert (result.previous_backup / "STS2_MCP.dll").read_bytes() == b"old-dll-bytes"
    backed_up_json = json.loads((result.previous_backup / "STS2_MCP.json").read_text())
    assert backed_up_json == {"version": "0.3.0"}
    assert json.loads((result.previous_backup / "STS2_MCP.conf").read_text()) == old_conf

    assert (mods_dir / "STS2_MCP.dll").read_bytes() == b"new-dll-bytes"
    assert json.loads((mods_dir / "STS2_MCP.json").read_text()) == json.loads(
        (FIXTURES / "mod_manifest.json").read_text()
    )
    new_conf = json.loads((mods_dir / "STS2_MCP.conf").read_text())
    assert new_conf["token"] == old_conf["token"]
    assert new_conf["port"] == 15526
    # Keys we don't manage survive the rewrite.
    assert new_conf["allow_hidden_state"] == old_conf["allow_hidden_state"]
    assert new_conf["allow_debug_actions"] == old_conf["allow_debug_actions"]
    mode = (mods_dir / "STS2_MCP.conf").stat().st_mode & 0o777
    assert mode == 0o600


def test_reinstall_normalizes_a_non_default_port(tmp_path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    (mods_dir / "STS2_MCP.conf").write_text(json.dumps({"port": 9999, "token": "existing-token"}))
    dll = make_dll(tmp_path)
    manifest = make_manifest(tmp_path)

    install_mod(dll, manifest, mods_dir, now=NOW, is_running=never_running)

    new_conf = json.loads((mods_dir / "STS2_MCP.conf").read_text())
    assert new_conf["port"] == 15526
    assert new_conf["token"] == "existing-token"


def test_reinstall_replaces_a_blank_token(tmp_path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    (mods_dir / "STS2_MCP.conf").write_text(json.dumps({"port": 15526, "token": "   "}))
    dll = make_dll(tmp_path)
    manifest = make_manifest(tmp_path)

    result = install_mod(
        dll, manifest, mods_dir, now=NOW, is_running=never_running,
        token_factory=lambda: "brand-new-token",
    )

    assert result.token_created is True
    new_conf = json.loads((mods_dir / "STS2_MCP.conf").read_text())
    assert new_conf["token"] == "brand-new-token"


def test_install_mod_refuses_to_clobber_an_existing_backup_stamp(tmp_path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    (mods_dir / "STS2_MCP.dll").write_bytes(b"old-dll-bytes")
    (mods_dir / ".previous" / STAMP).mkdir(parents=True)
    dll = make_dll(tmp_path)
    manifest = make_manifest(tmp_path)

    with pytest.raises(InstallError, match="already exists"):
        install_mod(dll, manifest, mods_dir, now=NOW, is_running=never_running)

    assert (mods_dir / "STS2_MCP.dll").read_bytes() == b"old-dll-bytes"


def test_install_mod_refuses_while_the_game_runs_and_changes_nothing(tmp_path):
    mods_dir = tmp_path / "mods"
    dll = make_dll(tmp_path)
    manifest = make_manifest(tmp_path)

    with pytest.raises(InstallError, match="running"):
        install_mod(dll, manifest, mods_dir, now=NOW, is_running=always_running)

    assert not mods_dir.exists()


def test_install_mod_refuses_while_the_game_runs_and_keeps_the_existing_install(tmp_path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    (mods_dir / "STS2_MCP.dll").write_bytes(b"old-dll-bytes")
    dll = make_dll(tmp_path)
    manifest = make_manifest(tmp_path)

    with pytest.raises(InstallError, match="running"):
        install_mod(dll, manifest, mods_dir, now=NOW, is_running=always_running)

    assert (mods_dir / "STS2_MCP.dll").read_bytes() == b"old-dll-bytes"
    assert not (mods_dir / ".previous").exists()


# --- enable_mod_in_settings --------------------------------------------------


def test_enable_mod_in_settings_flips_the_existing_entry_and_preserves_other_keys(tmp_path):
    settings_path = tmp_path / "settings.save"
    shutil.copy2(FIXTURES / "settings.save", settings_path)
    backup_root = tmp_path / "backups"

    backup_path = enable_mod_in_settings(
        settings_path, backup_root, now=NOW, is_running=never_running
    )

    assert backup_path == backup_root / f"settings.save.{STAMP}"
    assert backup_path.read_text() == (FIXTURES / "settings.save").read_text()

    updated = json.loads(settings_path.read_text())
    assert updated["mod_settings"]["mods_enabled"] is True
    entries = updated["mod_settings"]["mod_list"]
    assert len(entries) == 1
    assert entries[0] == {"id": "STS2_MCP", "is_enabled": True, "source": "mods_directory"}
    # Untouched keys, including ones before and after mod_settings, survive.
    assert updated["aspect_ratio"] == "SIXTEEN_NINE"
    assert updated["schema_version"] == 3
    assert updated["vsync"] is True


def test_enable_mod_in_settings_appends_the_entry_when_absent(tmp_path):
    settings_path = tmp_path / "settings.save"
    initial = {
        "aspect_ratio": "SIXTEEN_NINE",
        "mod_settings": {"mod_list": [], "mods_enabled": False},
    }
    settings_path.write_text(json.dumps(initial, indent=2))
    backup_root = tmp_path / "backups"

    enable_mod_in_settings(settings_path, backup_root, now=NOW, is_running=never_running)

    updated = json.loads(settings_path.read_text())
    assert updated["mod_settings"]["mod_list"] == [
        {"id": "STS2_MCP", "is_enabled": True, "source": "mods_directory"}
    ]
    assert updated["mod_settings"]["mods_enabled"] is True


def test_enable_mod_in_settings_writes_two_space_indented_json(tmp_path):
    settings_path = tmp_path / "settings.save"
    shutil.copy2(FIXTURES / "settings.save", settings_path)

    enable_mod_in_settings(settings_path, tmp_path / "backups", now=NOW, is_running=never_running)

    text = settings_path.read_text()
    assert '{\n  "aspect_ratio"' in text


def test_enable_mod_in_settings_refuses_to_clobber_an_existing_backup(tmp_path):
    settings_path = tmp_path / "settings.save"
    shutil.copy2(FIXTURES / "settings.save", settings_path)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    stale_backup = backup_root / f"settings.save.{STAMP}"
    stale_backup.write_text("stale-backup")

    with pytest.raises(InstallError, match="already exists"):
        enable_mod_in_settings(settings_path, backup_root, now=NOW, is_running=never_running)

    assert stale_backup.read_text() == "stale-backup"
    assert settings_path.read_text() == (FIXTURES / "settings.save").read_text()


def test_enable_mod_in_settings_refuses_while_the_game_runs_and_changes_nothing(tmp_path):
    settings_path = tmp_path / "settings.save"
    shutil.copy2(FIXTURES / "settings.save", settings_path)
    original = settings_path.read_text()
    backup_root = tmp_path / "backups"

    with pytest.raises(InstallError, match="running"):
        enable_mod_in_settings(settings_path, backup_root, now=NOW, is_running=always_running)

    assert settings_path.read_text() == original
    assert not backup_root.exists()
