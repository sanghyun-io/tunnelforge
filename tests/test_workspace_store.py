import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core import workspace_store as ws
from src.core.platform_paths import workspaces_dir
from src.core.workspace_store import (
    CursorState, FileState, TabState, WorkspaceState, WorkspaceStore, WorkspaceStoreError,
)

PID = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return WorkspaceStore(tmp_path / "workspaces")


def sample_state(**overrides) -> WorkspaceState:
    state = WorkspaceState(
        profile_id=PID, target_database="app", target_schema="", active_tab=1, app_version="2.8.1",
        tabs=[
            TabState(id="t1", title_index=3, text="SELECT 1;", dirty=True, cursor=CursorState(9, 9, 0)),
            TabState(id="t2", title_index=1, file_path=os.path.abspath("report.sql"), dirty=False,
                     cursor=CursorState(120, 120, 14), file_state=FileState(5, 2048, "ab" * 32)),
        ],
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_workspaces_dir_is_under_app_support_dir():
    assert workspaces_dir(platform_name="Linux", home=Path("/h"), environ={}) == Path("/h/.config/tunnelforge/workspaces")
    assert workspaces_dir(platform_name="Darwin", home=Path("/h")).name == "workspaces"


def test_round_trip_preserves_tabs_order_cursor_and_target(store):
    result = store.save(sample_state(), now=NOW)
    assert result.omitted_tabs == [] and not result.wrote_sibling
    loaded = store.load(PID)
    assert loaded.status == ws.STATUS_OK and loaded.crashed is True  # session_state defaults to "open"
    state = loaded.state
    assert [t.id for t in state.tabs] == ["t1", "t2"]
    assert state.active_tab == 1
    assert (state.target_database, state.target_schema) == ("app", "")
    assert state.tabs[0].text == "SELECT 1;" and state.tabs[0].dirty is True
    assert state.tabs[0].cursor == CursorState(9, 9, 0)
    assert state.tabs[1].text is None and state.tabs[1].file_state == FileState(5, 2048, "ab" * 32)
    assert state.saved_at == "2026-10-01T09:00:00Z"


def test_clean_close_is_not_reported_as_a_crash(store):
    store.save(sample_state(session_state=ws.SESSION_CLOSED))
    assert store.load(PID).crashed is False


def test_missing_workspace(store):
    assert store.load(PID).status == ws.STATUS_MISSING


def test_saved_json_contains_only_whitelisted_fields(store):
    store.save(sample_state())
    data = json.loads(store.path_for(PID).read_text(encoding="utf-8"))
    assert set(data) == {"version", "profile_id", "saved_at", "session", "target", "active_tab", "tabs"}
    assert set(data["tabs"][0]) == {"id", "title_index", "file_path", "text", "dirty", "text_omitted", "cursor", "file_state", "target"}
    keys = set()

    def collect(node):
        if isinstance(node, dict):
            for key, value in node.items():
                keys.add(key.lower())
                collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(data)
    for forbidden in ("password", "secret", "token", "result", "host", "port", "user", "credential"):
        assert not any(forbidden in key for key in keys), forbidden


def test_invalid_profile_ids_are_rejected_before_any_path_is_built(store):
    for bad in ("", "../x", "a/b", "a\\b", "a b", "x" * 97, None, 5):
        with pytest.raises(WorkspaceStoreError):
            store.path_for(bad)
        with pytest.raises(WorkspaceStoreError):
            store.load(bad)


def test_atomic_write_keeps_previous_file_when_replace_fails(store, monkeypatch):
    store.save(sample_state(), now=NOW)
    before = store.path_for(PID).read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(ws.os, "replace", boom)
    with pytest.raises(WorkspaceStoreError):
        store.save(sample_state(active_tab=0), now=NOW + timedelta(seconds=5))
    monkeypatch.undo()
    assert store.path_for(PID).read_bytes() == before
    assert list(store.directory.glob("*.tmp.*")) == [], "failed write must not leave a temp file"


def test_stale_tmp_files_are_ignored_and_cleaned(store):
    store.save(sample_state())
    stale = store.directory / f"{PID}.json.tmp.1.2"
    stale.write_text("{partial", encoding="utf-8")
    assert store.load(PID).status == ws.STATUS_OK
    assert store.cleanup_stale_tmp() == 1 and not stale.exists()


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"version":1}', '{"version":1,"profile_id":"other","tabs":[]}'])
def test_corrupt_file_is_quarantined_and_blank_state_returned(store, content):
    store.directory.mkdir(parents=True)
    store.path_for(PID).write_text(content, encoding="utf-8")
    result = store.load(PID, now=NOW)
    assert result.status == ws.STATUS_CORRUPT and result.state is None
    assert result.backup_path.name == f"{PID}.corrupt-20261001090000"
    assert result.backup_path.read_text(encoding="utf-8") == content, "evidence is kept, not deleted"
    assert not store.path_for(PID).exists()
    assert store.load(PID).status == ws.STATUS_MISSING


def test_only_the_newest_corrupt_backups_are_kept(store):
    store.directory.mkdir(parents=True)
    for index in range(5):
        store.path_for(PID).write_text("{bad", encoding="utf-8")
        store.load(PID, now=NOW + timedelta(seconds=index))
    kept = sorted(p.name for p in store.directory.glob("*.corrupt-*"))
    assert len(kept) == ws.KEEP_CORRUPT_BACKUPS
    assert kept[-1].endswith("20261001090004")


def test_newer_version_is_preserved_untouched_and_new_state_goes_to_a_sibling(store):
    store.directory.mkdir(parents=True)
    future = json.dumps({"version": 99, "profile_id": PID, "tabs": [], "future_field": True})
    store.path_for(PID).write_text(future, encoding="utf-8")
    assert store.load(PID).status == ws.STATUS_NEWER_VERSION
    result = store.save(sample_state())
    assert result.wrote_sibling and result.path.name == f"{PID}.compat-v1.json"
    assert store.path_for(PID).read_text(encoding="utf-8") == future
    loaded = store.load(PID)
    assert loaded.status == ws.STATUS_OK and loaded.state.tabs[0].text == "SELECT 1;"


def test_bad_tab_entries_are_skipped_individually_and_cursor_is_clamped(store):
    store.save(sample_state())
    data = json.loads(store.path_for(PID).read_text(encoding="utf-8"))
    data["tabs"] = [
        "not a tab",
        {"id": 5},
        {"id": "dup", "text": "a", "cursor": {"position": 999, "anchor": -3}},
        {"id": "dup", "text": "b"},
        {"id": "rel", "file_path": "relative/path.sql", "text": None},
        {"id": "typed", "text": 12},
        {"id": "ok", "text": "SELECT 2", "cursor": {"position": True, "first_visible_line": "x"}, "unknown": 1},
    ]
    data["active_tab"] = 99
    store.path_for(PID).write_text(json.dumps(data), encoding="utf-8")
    state = store.load(PID).state
    assert [t.id for t in state.tabs] == ["dup", "rel", "ok"]
    assert state.tabs[0].cursor.position == 1 and state.tabs[0].cursor.anchor == 0
    assert state.tabs[1].file_path is None, "relative paths are never used"
    assert state.tabs[2].cursor == CursorState(0, 0, 0)
    assert state.active_tab == 2


def test_tab_count_is_limited_and_reported(store):
    tabs = [TabState(id=f"t{i}", text="x") for i in range(ws.MAX_TABS + 3)]
    result = store.save(sample_state(tabs=tabs, active_tab=0))
    assert result.omitted_tabs == ["t50", "t51", "t52"]
    assert len(store.load(PID).state.tabs) == ws.MAX_TABS


def test_oversized_draft_is_omitted_and_flagged_never_truncated(store):
    huge = "x" * (ws.MAX_TAB_TEXT_BYTES + 1)
    state = sample_state(tabs=[TabState(id="big", text=huge, dirty=True), TabState(id="small", text="SELECT 1", dirty=True)])
    result = store.save(state)
    assert result.omitted_tabs == ["big"]
    loaded = store.load(PID).state
    assert loaded.tabs[0].text is None and loaded.tabs[0].text_omitted is True
    assert loaded.tabs[1].text == "SELECT 1"
    assert state.tabs[0].text == huge, "the caller's snapshot must not be modified"


def test_total_file_size_limit_drops_the_largest_drafts_first(store, monkeypatch):
    monkeypatch.setattr(ws, "MAX_FILE_BYTES", 3000)
    tabs = [TabState(id="a", text="a" * 2000), TabState(id="b", text="b" * 500), TabState(id="c", text="c" * 100)]
    result = store.save(sample_state(tabs=tabs, active_tab=0))
    assert result.omitted_tabs == ["a"]
    loaded = store.load(PID).state
    assert loaded.tabs[0].text_omitted and loaded.tabs[1].text == "b" * 500


def test_unicode_and_line_endings_round_trip(store):
    text = "-- 한글 주석\r\nSELECT '😀', E'\\n';\n"
    store.save(sample_state(tabs=[TabState(id="u", text=text, dirty=True)], active_tab=0))
    assert store.load(PID).state.tabs[0].text == text


def test_file_state_detects_unchanged_changed_and_missing(tmp_path):
    path = tmp_path / "q.sql"
    path.write_text("SELECT 1;", encoding="utf-8")
    saved = ws.compute_file_state(str(path))
    assert ws.compare_file_state(str(path), saved) == "unchanged"
    assert ws.compare_file_state(str(path), None) == "unknown"
    path.write_text("SELECT 2;", encoding="utf-8")
    assert ws.compare_file_state(str(path), saved) == "changed"
    path.unlink()
    assert ws.compare_file_state(str(path), saved) == "missing"
    assert ws.compute_file_state(str(path)) is None


def test_delete_removes_workspace_and_sibling_but_keeps_other_profiles(store):
    other = "other-profile"
    store.save(sample_state())
    store.save(sample_state(profile_id=other))
    store.delete(PID)
    store.delete(PID)  # idempotent
    assert store.load(PID).status == ws.STATUS_MISSING
    assert store.load(other).status == ws.STATUS_OK


def test_delete_all_clears_workspaces_backups_and_tmp(store):
    store.save(sample_state())
    store.save(sample_state(profile_id="p2"))
    store.path_for("p3").write_text("{bad", encoding="utf-8")
    store.load("p3", now=NOW)
    (store.directory / "p2.json.tmp.1.1").write_text("x", encoding="utf-8")
    assert store.delete_all() == 4
    assert list(store.directory.iterdir()) == []
    assert WorkspaceStore(store.directory / "missing").delete_all() == 0


def test_orphans_are_listed_and_expire_after_90_days_without_being_deleted(store):
    store.save(sample_state(profile_id="live"), now=NOW)
    store.save(sample_state(profile_id="gone-new"), now=NOW)
    store.save(sample_state(profile_id="gone-old"), now=NOW - timedelta(days=91))
    store.path_for("junk").write_text("{bad", encoding="utf-8")
    summaries = {s.profile_id: s for s in store.list_workspaces(["live"], now=NOW)}
    assert set(summaries) == {"live", "gone-new", "gone-old"}
    assert not summaries["live"].orphan and not summaries["live"].expired
    assert summaries["gone-new"].orphan and not summaries["gone-new"].expired
    assert summaries["gone-old"].orphan and summaries["gone-old"].expired
    assert summaries["live"].tab_count == 2
    assert store.path_for("gone-old").exists(), "expiry is a marker for the UI, never an automatic delete"


def test_orphan_content_can_be_read_for_the_read_only_recovered_sql_list(store):
    store.save(sample_state(profile_id="gone"))
    assert store.load("gone").state.tabs[0].text == "SELECT 1;"


def test_autosave_setting_is_clamped():
    assert ws.autosave_seconds(30) == 30
    assert ws.autosave_seconds(1) == ws.MIN_AUTOSAVE_SECONDS
    assert ws.autosave_seconds(10_000) == ws.MAX_AUTOSAVE_SECONDS
    for bad in (None, "x", [], "3.5"):
        assert ws.autosave_seconds(bad) == ws.DEFAULT_AUTOSAVE_SECONDS


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_workspace_file_is_owner_only_on_posix(store):
    store.save(sample_state())
    assert (store.path_for(PID).stat().st_mode & 0o777) == 0o600


def test_module_imports_no_qt_and_no_credential_apis():
    source = Path(ws.__file__).read_text(encoding="utf-8")
    assert "PyQt" not in source and "config_manager" not in source and "get_tunnel_credentials" not in source
    for line in source.splitlines():
        if "logger." in line:
            assert "text" not in line.lower().replace("context", ""), line


def test_concurrent_saves_leave_a_valid_file(store):
    import threading

    errors = []

    def worker(index):
        try:
            for step in range(10):
                store.save(sample_state(tabs=[TabState(id="t", text=f"SELECT {index}-{step}", dirty=True)], active_tab=0))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert store.load(PID).state.tabs[0].text.startswith("SELECT ")
    assert list(store.directory.glob("*.tmp.*")) == []
