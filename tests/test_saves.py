from datetime import datetime

import pytest

from coach.saves import SaveError, backup, find_steam_dir, restore, seed_modded

NOW = datetime(2026, 9, 24, 12, 0, 0)


def never_running():
    return False


def always_running():
    return True


def make_save_tree(root):
    steam = root / "steam" / "76561198000000000"
    saves = steam / "profile1" / "saves"
    (saves / "history").mkdir(parents=True)
    (saves / "progress.save").write_text('{"unlocks": "full"}')
    (saves / "progress.save.backup").write_text('{"unlocks": "full"}')
    (saves / "prefs.save").write_text('{"fast_mode": true}')
    (saves / "current_run.save").write_text('{"run": "normal"}')
    (saves / "history" / "1.run").write_text("{}")
    modded = steam / "modded" / "profile1" / "saves"
    modded.mkdir(parents=True)
    (modded / "progress.save").write_text('{"unlocks": "none"}')
    (modded / "current_run.save").write_text('{"run": "modded-in-progress"}')
    (steam / "profile.save").write_text('{"last_profile_id": 1}')
    return steam


def test_find_steam_dir_requires_exactly_one_account(tmp_path):
    steam = make_save_tree(tmp_path)
    assert find_steam_dir(tmp_path / "steam") == steam
    (tmp_path / "steam" / "76561198000000001").mkdir()
    with pytest.raises(SaveError, match="exactly one"):
        find_steam_dir(tmp_path / "steam")


def test_backup_copies_the_whole_account_folder(tmp_path):
    steam = make_save_tree(tmp_path)
    dest = backup(steam, tmp_path / "backups", now=NOW, is_running=never_running)
    assert dest.name == "76561198000000000-20260924-120000"
    assert (dest / "profile1" / "saves" / "history" / "1.run").exists()
    assert (dest / "modded" / "profile1" / "saves" / "current_run.save").exists()


def test_backup_refuses_while_the_game_runs(tmp_path):
    steam = make_save_tree(tmp_path)
    with pytest.raises(SaveError, match="running"):
        backup(steam, tmp_path / "backups", now=NOW, is_running=always_running)
    assert not (tmp_path / "backups").exists()


def test_seed_copies_progress_and_prefs_only_after_a_backup(tmp_path):
    steam = make_save_tree(tmp_path)
    result = seed_modded(
        steam, tmp_path / "backups", source_profile=1, target_slot=1, now=NOW,
        is_running=never_running,
    )
    modded = steam / "modded" / "profile1" / "saves"
    assert (modded / "progress.save").read_text() == '{"unlocks": "full"}'
    assert (modded / "prefs.save").read_text() == '{"fast_mode": true}'
    # The modded run in progress is untouched, and nothing else is copied over.
    assert (modded / "current_run.save").read_text() == '{"run": "modded-in-progress"}'
    assert not (modded / "history").exists()
    # The backup holds the pre-seed modded state.
    saved = result.backup_path / "modded" / "profile1" / "saves" / "progress.save"
    assert saved.read_text() == '{"unlocks": "none"}'
    assert sorted(p.name for p in result.written) == [
        "prefs.save", "progress.save", "progress.save.backup",
    ]


def test_seed_creates_an_empty_target_slot(tmp_path):
    steam = make_save_tree(tmp_path)
    seed_modded(
        steam, tmp_path / "backups", source_profile=1, target_slot=3, now=NOW,
        is_running=never_running,
    )
    assert (steam / "modded" / "profile3" / "saves" / "progress.save").exists()


def test_seed_refuses_while_the_game_runs_and_changes_nothing(tmp_path):
    steam = make_save_tree(tmp_path)
    with pytest.raises(SaveError, match="running"):
        seed_modded(
            steam, tmp_path / "backups", source_profile=1, target_slot=1, now=NOW,
            is_running=always_running,
        )
    modded = steam / "modded" / "profile1" / "saves" / "progress.save"
    assert modded.read_text() == '{"unlocks": "none"}'
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("source,target", [(0, 1), (4, 1), (1, 0), (1, 4)])
def test_seed_rejects_profile_numbers_outside_1_to_3(tmp_path, source, target):
    steam = make_save_tree(tmp_path)
    with pytest.raises(SaveError, match="1, 2 or 3"):
        seed_modded(
            steam, tmp_path / "backups", source_profile=source, target_slot=target, now=NOW,
            is_running=never_running,
        )


def test_seed_fails_cleanly_when_the_source_has_no_progress(tmp_path):
    steam = make_save_tree(tmp_path)
    with pytest.raises(SaveError, match="progress.save"):
        seed_modded(
            steam, tmp_path / "backups", source_profile=2, target_slot=1, now=NOW,
            is_running=never_running,
        )
    assert not (tmp_path / "backups").exists()


def test_restore_round_trips_and_keeps_the_replaced_folder(tmp_path):
    steam = make_save_tree(tmp_path)
    dest = backup(steam, tmp_path / "backups", now=NOW, is_running=never_running)
    (steam / "profile1" / "saves" / "progress.save").write_text('{"unlocks": "corrupted"}')
    aside = restore(dest, steam, now=NOW, is_running=never_running)
    progress_restored = (steam / "profile1" / "saves" / "progress.save").read_text()
    progress_aside = (aside / "profile1" / "saves" / "progress.save").read_text()
    assert progress_restored == '{"unlocks": "full"}'
    assert progress_aside == '{"unlocks": "corrupted"}'


def test_restore_rejects_a_folder_that_is_not_a_backup(tmp_path):
    steam = make_save_tree(tmp_path)
    not_backup = tmp_path / "random"
    not_backup.mkdir()
    with pytest.raises(SaveError, match="not a save backup"):
        restore(not_backup, steam, now=NOW, is_running=never_running)
