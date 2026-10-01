import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from src.core import scheduled_backup_store as store

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_backup(output, name, schedule_id="s1", state=store.STATE_COMPLETED, age_days=0, files=True):
    path = output / name
    path.mkdir(parents=True)
    if files:
        (path / "_tunnelforge_dump.json").write_text("{}", encoding="utf-8")
    store.write_marker(str(path), schedule_id, "nightly", state, created_at=NOW - timedelta(days=age_days),
                       finished_at=NOW - timedelta(days=age_days) if state != store.STATE_RUNNING else None)
    return path


def names(items):
    return [os.path.basename(b.path) for b in items]


def test_marker_round_trip_and_state_update_keep_the_creation_time(tmp_path):
    path = make_backup(tmp_path, "b1", state=store.STATE_RUNNING, age_days=2)
    first = store.read_marker(str(path))
    assert first["state"] == store.STATE_RUNNING and first["schedule_id"] == "s1" and first["dir_name"] == "b1"
    store.write_marker(str(path), "s1", "nightly", store.STATE_COMPLETED, finished_at=NOW, message="ok")
    updated = store.read_marker(str(path))
    assert updated["state"] == store.STATE_COMPLETED and updated["created_at"] == first["created_at"]
    assert not list(path.glob("*.tmp.*"))


@pytest.mark.parametrize("content", ["", "{bad", "[]", json.dumps({"version": 2}),
                                     json.dumps({"version": 1, "schedule_id": "", "state": "completed", "created_at": "2026-01-01T00:00:00Z", "dir_name": "b"}),
                                     json.dumps({"version": 1, "schedule_id": "s", "state": "weird", "created_at": "2026-01-01T00:00:00Z", "dir_name": "b"}),
                                     json.dumps({"version": 1, "schedule_id": "s", "state": "completed", "created_at": "yesterday", "dir_name": "b"})])
def test_invalid_markers_do_not_prove_ownership(tmp_path, content):
    path = tmp_path / "b"
    path.mkdir()
    (path / store.MARKER_NAME).write_text(content, encoding="utf-8")
    assert store.read_marker(str(path)) is None
    assert store.list_owned(str(tmp_path), "s") == []


def test_a_marker_copied_into_another_folder_name_is_not_ownership(tmp_path):
    original = make_backup(tmp_path, "b1")
    copy = tmp_path / "b1_copy"
    copy.mkdir()
    (copy / store.MARKER_NAME).write_text((original / store.MARKER_NAME).read_text(encoding="utf-8"), encoding="utf-8")
    assert names(store.list_owned(str(tmp_path), "s1")) == ["b1"]


def test_only_this_schedules_marked_folders_are_owned(tmp_path):
    make_backup(tmp_path, "mine")
    make_backup(tmp_path, "other_schedule", schedule_id="s2")
    (tmp_path / "legacy_20250101_030000").mkdir()  # old version: no marker
    (tmp_path / "user_stuff").mkdir()
    (tmp_path / "loose_file.txt").write_text("x", encoding="utf-8")
    assert names(store.list_owned(str(tmp_path), "s1")) == ["mine"]
    assert store.list_owned(str(tmp_path / "missing"), "s1") == []


def test_retention_by_count_keeps_the_newest_completed_backups(tmp_path):
    for index in range(6):
        make_backup(tmp_path, f"b{index}", age_days=6 - index)
    victims = store.select_for_deletion(store.list_owned(str(tmp_path), "s1"), retention_count=3, retention_days=365, now=NOW)
    assert names(victims) == ["b0", "b1", "b2"]


def test_retention_by_age_never_deletes_the_newest_completed_backup(tmp_path):
    make_backup(tmp_path, "old1", age_days=100)
    make_backup(tmp_path, "old2", age_days=90)
    victims = store.select_for_deletion(store.list_owned(str(tmp_path), "s1"), retention_count=10, retention_days=30, now=NOW)
    assert names(victims) == ["old1"], "the newest completed backup survives even when it is stale"


def test_running_backups_are_never_selected_and_failed_ones_expire_by_age(tmp_path):
    make_backup(tmp_path, "running_old", state=store.STATE_RUNNING, age_days=400)
    make_backup(tmp_path, "failed_old", state=store.STATE_FAILED, age_days=60)
    make_backup(tmp_path, "failed_new", state=store.STATE_FAILED, age_days=1)
    make_backup(tmp_path, "interrupted_old", state=store.STATE_INTERRUPTED, age_days=45)
    make_backup(tmp_path, "good", age_days=0)
    victims = store.select_for_deletion(store.list_owned(str(tmp_path), "s1"), retention_count=5, retention_days=30, now=NOW)
    assert names(victims) == ["failed_old", "interrupted_old"]


def test_failed_backups_do_not_count_against_the_completed_quota(tmp_path):
    make_backup(tmp_path, "c1", age_days=3)
    make_backup(tmp_path, "c2", age_days=2)
    make_backup(tmp_path, "f1", state=store.STATE_FAILED, age_days=1)
    assert store.select_for_deletion(store.list_owned(str(tmp_path), "s1"), 2, 365, NOW) == []


def test_apply_retention_deletes_only_proven_folders_and_leaves_everything_else(tmp_path):
    for index in range(4):
        make_backup(tmp_path, f"b{index}", age_days=4 - index)
    make_backup(tmp_path, "running", state=store.STATE_RUNNING, age_days=50)
    foreign = tmp_path / "nightly_20240101_030000"  # looks like a backup, has no marker
    foreign.mkdir()
    (foreign / "keep.txt").write_text("x", encoding="utf-8")
    other = make_backup(tmp_path, "other", schedule_id="s2", age_days=999)
    removed = store.apply_retention(str(tmp_path), "s1", 2, 365, NOW)
    assert sorted(os.path.basename(p) for p in removed) == ["b0", "b1"]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["b2", "b3", "running", "nightly_20240101_030000", "other"])
    assert (foreign / "keep.txt").exists() and other.exists()


def test_delete_revalidates_ownership_and_refuses_running_or_foreign_paths(tmp_path):
    path = make_backup(tmp_path, "b1", age_days=10)
    candidate = store.list_owned(str(tmp_path), "s1")[0]
    store.write_marker(str(path), "s1", "nightly", store.STATE_RUNNING)  # became active after selection
    assert store.delete_owned(str(tmp_path), candidate) is False and path.exists()
    store.write_marker(str(path), "other-schedule", "x", store.STATE_COMPLETED)
    assert store.delete_owned(str(tmp_path), candidate) is False and path.exists()
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    stranger = store.OwnedBackup(str(outside), "s1", store.STATE_COMPLETED, NOW)
    assert store.delete_owned(str(tmp_path), stranger) is False and outside.exists()


def test_symlinked_folders_are_never_followed_or_deleted(tmp_path):
    real = tmp_path / "real_data"
    real.mkdir()
    (real / "precious.txt").write_text("x", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    link = output / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    store.write_marker(str(real), "s1", "n", store.STATE_COMPLETED)  # marker dir_name is 'real_data', not 'link'
    assert store.list_owned(str(output), "s1") == []
    assert store.delete_owned(str(output), store.OwnedBackup(str(link), "s1", store.STATE_COMPLETED, NOW)) is False
    assert (real / "precious.txt").exists()


def test_stale_running_markers_become_interrupted_unless_the_schedule_is_active(tmp_path):
    path = make_backup(tmp_path, "b1", state=store.STATE_RUNNING)
    assert store.sweep_interrupted(str(tmp_path), "s1", schedule_active=True) == 0
    assert store.read_marker(str(path))["state"] == store.STATE_RUNNING
    assert store.sweep_interrupted(str(tmp_path), "s1", schedule_active=False) == 1
    assert store.read_marker(str(path))["state"] == store.STATE_INTERRUPTED
    assert store.sweep_interrupted(str(tmp_path), "s1", schedule_active=False) == 0


def test_unknown_state_is_rejected():
    with pytest.raises(ValueError):
        store.write_marker("x", "s", "n", "weird")
