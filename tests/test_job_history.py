import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core import job_history as jh
from src.core.job_history import JobHistory
from src.core.platform_paths import job_history_file

NOW = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def history(tmp_path):
    return JobHistory(tmp_path / "jobs" / "job_history.json")


def begin(history, **overrides):
    fields = dict(profile_id="p1", profile_name="Prod DB", target="app", mode="parallel_strict",
                  details={"tables": 3}, now=NOW)
    fields.update(overrides)
    kind = fields.pop("kind", jh.KIND_EXPORT_FULL)
    return history.begin(kind, **fields)


def test_path_is_in_the_app_support_directory():
    assert job_history_file(platform_name="Linux", home=Path("/h"), environ={}) == Path("/h/.config/tunnelforge/job_history.json")


def test_begin_and_finish_round_trip(history):
    job = begin(history)
    running = history.list()[0]
    assert running.status == jh.STATUS_RUNNING and running.finished_at == "" and running.started_at == "2026-10-01T09:00:00Z"
    assert history.finish(job, jh.STATUS_COMPLETED, report_path="C:/out", details={"rows": 10}, now=NOW + timedelta(seconds=75))
    done = history.list()[0]
    assert (done.status, done.report_path, done.details) == (jh.STATUS_COMPLETED, "C:/out", {"tables": 3, "rows": 10})
    assert done.duration_seconds() == 75
    assert (done.profile_id, done.profile_name, done.target, done.mode) == ("p1", "Prod DB", "app", "parallel_strict")


def test_unknown_job_or_status_are_rejected(history):
    with pytest.raises(ValueError):
        history.begin("nonsense")
    job = begin(history)
    with pytest.raises(ValueError):
        history.finish(job, jh.STATUS_RUNNING)
    assert history.finish("missing-id", jh.STATUS_FAILED) is False


def test_list_is_newest_first_and_survives_a_new_instance(history):
    first = begin(history, now=NOW)
    second = begin(history, now=NOW + timedelta(minutes=1), kind=jh.KIND_IMPORT)
    history.finish(second, jh.STATUS_FAILED, error="boom")
    again = JobHistory(history.path)
    assert [r.id for r in again.list()] == [second, first]
    assert again.list()[0].kind == jh.KIND_IMPORT


@pytest.mark.parametrize("raw, expected_has", [
    ("connect failed mysql://admin:hunter2@db.internal:3306/app", "mysql://***@db.internal"),
    ("access denied password=hunter2 for user", "password=***"),
    ('config {"password": "hunter2"}', "password=***"),
    ("mysql -u root -phunter2 -h host", "-p***"),
    ("token: abc123", "token=***"),
])
def test_error_summary_hides_secret_patterns(history, raw, expected_has):
    job = begin(history)
    history.finish(job, jh.STATUS_FAILED, error=raw)
    summary = history.list()[0].error_summary
    assert expected_has in summary and "hunter2" not in summary and "abc123" not in summary


def test_error_summary_is_collapsed_and_length_limited(history):
    job = begin(history)
    history.finish(job, jh.STATUS_FAILED, error="line one\n\n  line   two " + "x" * 2000)
    summary = history.list()[0].error_summary
    assert "\n" not in summary and summary.startswith("line one line two") and len(summary) <= jh.MAX_ERROR_CHARS


def test_details_and_rerun_are_whitelisted_and_secret_free(history):
    job = history.begin(
        jh.KIND_EXPORT_TABLES, profile_id="p", target="app", now=NOW,
        details={"tables": 2, "password": "x", "db_user": "root", "note": "ok", "nested": {"a": 1}, "ratio": 0.5},
        rerun={"schema": "app", "tables": ["a", "b"], "password": "x", "host": "db", "compression": "zstd",
               "threads": 4, "connection_uri": "mysql://u:p@h/db", "include_fk_parents": True},
    )
    record = history.list()[0]
    assert record.details == {"tables": 2, "note": "ok"}
    assert record.rerun == {"schema": "app", "tables": ["a", "b"], "compression": "zstd", "threads": 4, "include_fk_parents": True}
    raw = history.path.read_text(encoding="utf-8").lower()
    assert "hunter" not in raw and '"password"' not in raw and "mysql://" not in raw
    assert job == record.id


def test_rerun_table_list_is_capped(history):
    history.begin(jh.KIND_EXPORT_TABLES, rerun={"tables": [f"t{i}" for i in range(500)]}, now=NOW)
    assert len(history.list()[0].rerun["tables"]) == jh.MAX_RERUN_TABLES


def test_record_count_is_capped_but_running_jobs_are_never_dropped(history, monkeypatch):
    monkeypatch.setattr(jh, "MAX_RECORDS", 5)
    running = begin(history, now=NOW)
    ids = []
    for index in range(8):
        job = begin(history, now=NOW + timedelta(minutes=index + 1))
        history.finish(job, jh.STATUS_COMPLETED)
        ids.append(job)
    records = history.list()
    assert len(records) == 5
    assert running in [r.id for r in records], "a running job must survive trimming"
    assert [r.id for r in records if r.id != running] == list(reversed(ids[-4:]))


def test_delete_never_removes_running_jobs_and_clear_keeps_them(history):
    running = begin(history)
    done = begin(history)
    history.finish(done, jh.STATUS_FAILED)
    assert history.delete([running, done, "nope"]) == 1
    assert [r.id for r in history.list()] == [running]
    other = begin(history)
    history.finish(other, jh.STATUS_COMPLETED)
    assert history.clear() == 1 and [r.id for r in history.list()] == [running]


def test_stale_running_jobs_become_interrupted_on_startup(history):
    stale = begin(history)
    mine = begin(history)
    done = begin(history)
    history.finish(done, jh.STATUS_COMPLETED)
    assert history.mark_interrupted(alive_ids=[mine], now=NOW + timedelta(hours=1)) == 1
    by_id = {r.id: r for r in history.list()}
    assert by_id[stale].status == jh.STATUS_INTERRUPTED and "앱이 종료" in by_id[stale].error_summary
    assert by_id[stale].finished_at == "2026-10-01T10:00:00Z"
    assert by_id[mine].status == jh.STATUS_RUNNING and by_id[done].status == jh.STATUS_COMPLETED
    assert history.mark_interrupted(alive_ids=[mine]) == 0


@pytest.mark.parametrize("content", ["", "{bad", "[]", '{"records": 5}'])
def test_corrupt_file_is_set_aside_and_history_starts_empty(history, content):
    history.path.parent.mkdir(parents=True)
    history.path.write_text(content, encoding="utf-8")
    assert history.list() == []
    assert list(history.path.parent.glob("job_history.json.corrupt-*"))
    job = begin(history)
    assert [r.id for r in history.list()] == [job]


def test_bad_records_are_skipped_individually(history):
    history.path.parent.mkdir(parents=True)
    good = {"id": "ok", "kind": jh.KIND_IMPORT, "status": jh.STATUS_COMPLETED, "target": "t", "details": {"password": "x", "n": 1}}
    bad = [5, {"id": "x", "kind": "zzz", "status": "completed"}, {"id": "y", "kind": jh.KIND_IMPORT, "status": "weird"}, {"kind": jh.KIND_IMPORT}]
    history.path.write_text(json.dumps({"version": 1, "records": bad + [good]}), encoding="utf-8")
    records = history.list()
    assert [r.id for r in records] == ["ok"] and records[0].details == {"n": 1}


def test_atomic_write_keeps_the_previous_file_when_replace_fails(history, monkeypatch):
    begin(history)
    before = history.path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(jh.os, "replace", boom)
    with pytest.raises(OSError):
        begin(history)
    monkeypatch.undo()
    assert history.path.read_bytes() == before
    assert list(history.path.parent.glob("*.tmp.*")) == []


def test_ui_entry_points_never_raise_when_the_store_fails(monkeypatch):
    class Broken:
        def begin(self, *a, **k):
            raise OSError("nope")

        def finish(self, *a, **k):
            raise OSError("nope")

        def mark_interrupted(self, *a, **k):
            raise OSError("nope")

    monkeypatch.setattr(jh, "make_history", lambda: Broken())
    assert jh.job_begin(jh.KIND_IMPORT, target="x") is None
    jh.job_finish("id", jh.STATUS_FAILED)
    jh.job_finish(None, jh.STATUS_FAILED)
    assert jh.sweep_interrupted() == 0


def test_safe_entry_points_track_active_jobs_for_the_startup_sweep(tmp_path, monkeypatch):
    store = JobHistory(tmp_path / "h.json")
    monkeypatch.setattr(jh, "make_history", lambda: store)
    monkeypatch.setattr(jh, "_ACTIVE_IDS", set())
    mine = jh.job_begin(jh.KIND_IMPORT, target="x")
    foreign = store.begin(jh.KIND_EXPORT_FULL, target="y")
    assert jh.sweep_interrupted() == 1
    statuses = {r.id: r.status for r in store.list()}
    assert statuses == {mine: jh.STATUS_RUNNING, foreign: jh.STATUS_INTERRUPTED}
    jh.job_finish(mine, jh.STATUS_COMPLETED)
    assert {r.id: r.status for r in store.list()}[mine] == jh.STATUS_COMPLETED


def test_concurrent_writers_do_not_lose_records(history):
    ids = []
    lock = threading.Lock()

    def worker():
        for _ in range(10):
            job = begin(history)
            history.finish(job, jh.STATUS_COMPLETED)
            with lock:
                ids.append(job)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {r.id for r in history.list()} == set(ids) and len(ids) == 40


def test_module_has_no_qt_or_credential_dependencies():
    source = Path(jh.__file__).read_text(encoding="utf-8")
    assert "PyQt" not in source and "config_manager" not in source and "get_tunnel_credentials" not in source
