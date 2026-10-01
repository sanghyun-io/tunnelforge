"""Scheduled backup reliability: validation, missed runs, overlap, unattended credentials, ownership and
retention, job-history records."""
import os
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.core import job_history as jh
from src.core import scheduled_backup_store as backup_store
from src.core.backup_task_executor import BackupTaskExecutor, safe_folder_name
from src.core.scheduler import (
    BackupScheduler, SQL_TASK_UNSUPPORTED_MESSAGE, ScheduleConfig, describe_unattended_tunnel_failure,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = {"schedules": []}
    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: settings.get(key, default)
    config_manager.set_app_setting.side_effect = lambda key, value: settings.__setitem__(key, value)
    config_manager.load_config.return_value = {"tunnels": [{"id": "t1", "db_engine": "mysql", "name": "Prod"}]}
    config_manager.get_tunnel_credentials.return_value = ("backup_user", "pw")
    engine = MagicMock()
    engine.tunnel_configs = {}
    engine.is_running.return_value = True
    engine.get_connection_info.return_value = ("127.0.0.1", 13306)
    monkeypatch.setattr("src.core.execution_log_writer.platform_log_dir", lambda: tmp_path / "logs")
    scheduler = BackupScheduler(config_manager, engine)
    yield SimpleEnv(scheduler, settings, engine, config_manager, tmp_path)
    scheduler.stop()


class SimpleEnv:
    def __init__(self, scheduler, settings, engine, config_manager, tmp_path):
        self.scheduler, self.settings, self.engine, self.config_manager, self.tmp = scheduler, settings, engine, config_manager, tmp_path

    def schedule(self, **overrides):
        values = dict(id="s1", name="nightly", tunnel_id="t1", schema="app", output_dir=str(self.tmp / "out"),
                      cron_expression="0 3 * * *", enabled=True)
        values.update(overrides)
        return ScheduleConfig(**values)

    def reload(self):
        return BackupScheduler(self.config_manager, self.engine)


def history_records():
    return jh.make_history().list()


# ------------------------------------------------------------------ validation

def test_sql_schedules_are_rejected_and_never_executed(env):
    sql = env.schedule(task_type="sql_query", sql_query="DELETE FROM t")
    with pytest.raises(ValueError, match="예약 SQL 실행은 지원되지 않습니다"):
        env.scheduler.add_schedule(sql)
    # a legacy SQL schedule that is already in the stored settings stays inert
    env.settings["schedules"] = [sql.to_dict() | {"next_run": (datetime.now() - timedelta(minutes=1)).isoformat()}]
    legacy = env.reload()
    assert legacy._snapshot_due_jobs(datetime.now()) == []
    executed = MagicMock()
    legacy._sql_executor.execute = executed
    ok, message = legacy._execute_task(legacy.get_schedules()[0])
    assert (ok, message) == (False, SQL_TASK_UNSUPPORTED_MESSAGE) and not executed.called


@pytest.mark.parametrize("cron, fragment", [
    ("*/5 * * * *", "너무 짧습니다"),
    ("not a cron", "잘못된 cron"),
    ("0 0 31 2 *", "1년 안에"),
])
def test_invalid_or_too_frequent_expressions_are_refused(env, cron, fragment):
    with pytest.raises(ValueError, match=fragment):
        env.scheduler.add_schedule(env.schedule(cron_expression=cron))
    assert env.scheduler.get_schedules() == []


def test_minimum_interval_is_a_setting_and_updates_are_validated_too(env):
    env.scheduler.add_schedule(env.schedule())
    env.settings["scheduled_backup_min_interval_minutes"] = 60
    assert env.scheduler.min_interval_minutes() == 60
    with pytest.raises(ValueError, match="너무 짧습니다"):
        env.scheduler.update_schedule(env.schedule(cron_expression="*/30 * * * *"))
    env.settings["scheduled_backup_min_interval_minutes"] = "garbage"
    assert env.scheduler.min_interval_minutes() == 15


def test_backup_needs_an_output_folder(env):
    with pytest.raises(ValueError, match="출력 폴더"):
        env.scheduler.add_schedule(env.schedule(output_dir=""))


# ------------------------------------------------------------------ missed runs / overlap

def stored(env, **fields):
    env.settings["schedules"] = [env.schedule(**fields).to_dict()]
    return env.reload()


def test_stored_next_run_survives_a_restart_so_missed_runs_can_be_detected(env):
    past = (datetime.now() - timedelta(hours=5)).isoformat()
    scheduler = stored(env, next_run=past)
    assert scheduler.get_schedules()[0].next_run == past
    scheduler = stored(env, next_run="garbage")
    assert BackupScheduler._valid_iso(scheduler.get_schedules()[0].next_run)
    scheduler = stored(env, next_run=None, enabled=False)
    assert scheduler.get_schedules()[0].next_run is None


def test_on_time_run_is_a_normal_scheduled_job(env):
    now = datetime.now()
    scheduler = stored(env, next_run=(now - timedelta(seconds=20)).isoformat())
    jobs = scheduler._snapshot_due_jobs(now)
    assert [j.trigger for j in jobs] == ["scheduled"]
    assert history_records() == []


def test_missed_runs_are_caught_up_at_most_once(env):
    now = datetime.now()
    scheduler = stored(env, cron_expression="0 * * * *", next_run=(now - timedelta(hours=9)).isoformat())
    jobs = scheduler._snapshot_due_jobs(now)
    assert [j.trigger for j in jobs] == ["catch_up"], "nine missed hourly runs must produce one catch-up"
    assert scheduler._snapshot_due_jobs(now) == [], "the schedule is active now: no second job"
    scheduler._run_execution_job(jobs[0])
    live = scheduler.get_schedules()[0]
    assert datetime.fromisoformat(live.next_run) > now, "after the single catch-up the next run is in the future"
    assert scheduler._snapshot_due_jobs(now) == []


def test_missed_runs_are_skipped_and_recorded_when_catch_up_is_off(env):
    now = datetime.now()
    scheduler = stored(env, catch_up_missed=False, next_run=(now - timedelta(hours=3)).isoformat())
    assert scheduler._snapshot_due_jobs(now) == []
    record = history_records()[0]
    assert record.kind == jh.KIND_SCHEDULED_BACKUP and record.status == jh.STATUS_SKIPPED
    assert "놓친 실행" in record.error_summary and record.target == "app"
    assert datetime.fromisoformat(scheduler.get_schedules()[0].next_run) > now
    assert scheduler._snapshot_due_jobs(now) == [] and len(history_records()) == 1, "a skip is recorded once"
    assert env.settings["schedules"][0]["next_run"] == scheduler.get_schedules()[0].next_run


def test_a_due_schedule_that_is_still_running_is_skipped_and_recorded_not_queued_twice(env):
    now = datetime.now()
    scheduler = stored(env, next_run=(now - timedelta(seconds=10)).isoformat())
    first = scheduler._snapshot_due_jobs(now)
    assert len(first) == 1
    scheduler.get_schedules()[0].next_run = (now - timedelta(seconds=5)).isoformat()  # due again while still running
    assert scheduler._snapshot_due_jobs(now) == []
    record = history_records()[0]
    assert record.status == jh.STATUS_SKIPPED and "아직 진행 중" in record.error_summary
    assert datetime.fromisoformat(scheduler.get_schedules()[0].next_run) > now


def test_run_now_twice_runs_once_and_records_the_refused_request(env):
    env.scheduler.add_schedule(env.schedule())
    release = threading.Event()
    started = threading.Event()

    def slow(schedule, trigger="scheduled"):
        started.set()
        release.wait(5)
        return True, "ok"

    env.scheduler._execute_task = slow
    try:
        assert env.scheduler.run_now("s1")[0] is True
        assert started.wait(2)
        ok, message = env.scheduler.run_now("s1")
        assert ok is False and "이미 실행 중" in message
    finally:
        release.set()
    records = [r for r in history_records() if r.status == jh.STATUS_SKIPPED]
    assert len(records) == 1 and "지금 실행" in records[0].error_summary


def test_re_enabling_starts_the_clock_from_now(env):
    env.scheduler.add_schedule(env.schedule(enabled=False))
    env.scheduler.get_schedule("s1").next_run = (datetime.now() - timedelta(days=2)).isoformat()
    env.scheduler.set_enabled("s1", True)
    assert datetime.fromisoformat(env.scheduler.get_schedule("s1").next_run) > datetime.now()


# ------------------------------------------------------------------ unattended credentials

@pytest.mark.parametrize("message, expected", [
    ("처음 접속하는 SSH 서버입니다 (error_code=ssh_host_key_unknown)", "호스트 키를 자동으로 수락하지 않습니다"),
    ("SSH 서버의 호스트 키가 저장된 값과 다릅니다 (error_code=ssh_host_key_changed)", "중간자 공격"),
    ("개인키가 비밀번호(Passphrase)로 보호되어 있습니다: /k", "예약 실행에서 사용할 수 없습니다"),
    ("❌ 터널 연결 실패\n에러 타입: X\n세부\n", "터널 연결 실패: ❌ 터널 연결 실패"),
])
def test_unattended_tunnel_failures_have_clear_reasons(message, expected):
    assert expected in describe_unattended_tunnel_failure(message)


def test_unattended_start_failure_is_reported_and_never_prompts(env):
    env.engine.is_running.return_value = False
    env.engine.start_tunnel_unattended.return_value = (False, "x (error_code=ssh_host_key_unknown)")
    env.config_manager.load_config.return_value = {"tunnels": [{"id": "t1", "db_engine": "mysql"}]}
    resolved, error = env.scheduler._resolve_connection(env.schedule())
    assert resolved is None and "자동으로 수락하지 않습니다" in error
    env.engine.start_tunnel.assert_not_called()
    env.engine.stop_tunnel.assert_not_called()


def test_a_tunnel_opened_by_the_schedule_is_closed_afterwards_but_a_users_tunnel_is_not(env, monkeypatch):
    class Exporter:
        def __init__(self, config): pass
        def export_full_schema(self, schema, output_dir, threads): return True, "ok"

    monkeypatch.setattr("src.exporters.rust_dump_exporter.RustDumpExporter", Exporter)
    env.engine.is_running.return_value = False
    env.engine.start_tunnel_unattended.return_value = (True, "ok")
    assert env.scheduler._execute_backup(env.schedule())[0] is True
    env.engine.stop_tunnel.assert_called_once_with("t1")

    env.engine.reset_mock()
    env.engine.is_running.return_value = True
    env.engine.get_connection_info.return_value = ("127.0.0.1", 13306)
    assert env.scheduler._execute_backup(env.schedule())[0] is True
    env.engine.stop_tunnel.assert_not_called()
    env.engine.start_tunnel_unattended.assert_not_called()


def test_the_tunnel_is_closed_even_when_the_export_fails(env, monkeypatch):
    class Exporter:
        def __init__(self, config): pass
        def export_full_schema(self, schema, output_dir, threads): raise RuntimeError("boom")

    monkeypatch.setattr("src.exporters.rust_dump_exporter.RustDumpExporter", Exporter)
    env.engine.is_running.return_value = False
    env.engine.start_tunnel_unattended.return_value = (True, "ok")
    ok, message = env.scheduler._execute_backup(env.schedule())
    assert ok is False and "boom" in message
    env.engine.stop_tunnel.assert_called_once_with("t1")


# ------------------------------------------------------------------ ownership, retention, job history

class FakeExporter:
    outcome = (True, "ok")
    seen_state = None

    def __init__(self, config):
        pass

    def export_full_schema(self, schema, output_dir, threads):
        FakeExporter.seen_state = backup_store.read_marker(output_dir)  # Rust dump refuses a non-empty folder
        if FakeExporter.outcome[0]:
            with open(os.path.join(output_dir, "_tunnelforge_dump.json"), "w", encoding="utf-8") as handle:
                handle.write("{}")
        return FakeExporter.outcome


@pytest.fixture
def exporter(monkeypatch):
    FakeExporter.outcome, FakeExporter.seen_state = (True, "ok"), None
    monkeypatch.setattr("src.exporters.rust_dump_exporter.RustDumpExporter", FakeExporter)
    return FakeExporter


def test_successful_backup_is_marked_owned_and_recorded(env, exporter):
    schedule = env.schedule()
    ok, message = env.scheduler._execute_backup(schedule, trigger="catch_up")
    assert ok is True
    assert exporter.seen_state is None, "no marker while the export runs (the dump needs an empty folder)"
    owned = backup_store.list_owned(schedule.output_dir, "s1")
    assert len(owned) == 1 and owned[0].state == backup_store.STATE_COMPLETED
    record = history_records()[0]
    assert record.kind == jh.KIND_SCHEDULED_BACKUP and record.status == jh.STATUS_COMPLETED
    assert "놓친 실행 따라잡기" in record.mode and record.target == "app" and record.profile_id == "t1"
    assert record.report_path.endswith("_tunnelforge_dump.json") and os.path.dirname(record.report_path) == owned[0].path
    assert schedule.last_run


def test_failed_backup_is_recorded_with_its_reason_and_an_empty_folder_is_removed(env, exporter):
    exporter.outcome = (False, "dump.run refused: the selected scope contains objects the dump cannot preserve: trigger:t:trg")
    schedule = env.schedule()
    ok, message = env.scheduler._execute_backup(schedule)
    assert ok is False and "cannot preserve" in message
    assert os.listdir(schedule.output_dir) == [], "a folder that only holds the marker is not left behind"
    record = history_records()[0]
    assert record.status == jh.STATUS_FAILED and "cannot preserve" in record.error_summary


def test_failed_backup_with_partial_files_is_kept_marked_failed(env, monkeypatch):
    class Partial:
        def __init__(self, config): pass
        def export_full_schema(self, schema, output_dir, threads):
            with open(os.path.join(output_dir, "partial.chunk"), "w") as handle:
                handle.write("x")
            return False, "connection lost"

    monkeypatch.setattr("src.exporters.rust_dump_exporter.RustDumpExporter", Partial)
    schedule = env.schedule()
    assert env.scheduler._execute_backup(schedule)[0] is False
    owned = backup_store.list_owned(schedule.output_dir, "s1")
    assert len(owned) == 1 and owned[0].state == backup_store.STATE_FAILED


def test_retention_runs_after_success_and_only_touches_owned_folders(env, exporter):
    schedule = env.schedule(retention_count=2, retention_days=365)
    out = env.tmp / "out"
    out.mkdir()
    legacy = out / "nightly_20240101_030000"  # a backup from an older version: no marker
    legacy.mkdir()
    (legacy / "keep.txt").write_text("x", encoding="utf-8")
    user = out / "my_manual_backup"
    user.mkdir()
    for index in range(3):
        old = out / f"old{index}"
        old.mkdir()
        backup_store.write_marker(str(old), "s1", "nightly", backup_store.STATE_COMPLETED,
                                  created_at=datetime.now().astimezone() - timedelta(days=10 - index))
    ok, message = env.scheduler._execute_backup(schedule)
    assert ok is True and "오래된 백업 2개 정리" in message
    survivors = sorted(p.name for p in out.iterdir() if p.name not in ("nightly_20240101_030000", "my_manual_backup"))
    assert len(survivors) == 2 and "old2" in survivors and "old0" not in survivors and "old1" not in survivors
    assert (legacy / "keep.txt").exists() and user.exists()
    assert history_records()[0].details["retention_removed"] == 2


def test_two_schedules_never_clean_each_others_backups(env, exporter):
    out = env.tmp / "out"
    out.mkdir()
    other = out / "other_20250101_000000"
    other.mkdir()
    backup_store.write_marker(str(other), "s2", "other", backup_store.STATE_COMPLETED,
                              created_at=datetime.now().astimezone() - timedelta(days=500))
    assert env.scheduler._execute_backup(env.schedule(retention_count=1, retention_days=1))[0] is True
    assert other.exists()


def test_stale_running_markers_are_marked_interrupted_when_the_app_starts(env):
    out = env.tmp / "out"
    stale = out / "nightly_x"
    stale.mkdir(parents=True)
    backup_store.write_marker(str(stale), "s1", "nightly", backup_store.STATE_RUNNING)
    env.settings["schedules"] = [env.schedule().to_dict()]
    env.reload()
    assert backup_store.read_marker(str(stale))["state"] == backup_store.STATE_INTERRUPTED


def test_folder_names_are_sanitized_and_unique(env, exporter):
    assert safe_folder_name("../../etc/passwd") == "etc_passwd"
    assert safe_folder_name("야간 백업/운영") == "야간 백업_운영"
    assert safe_folder_name("???") == "backup"
    schedule = env.schedule(name="a/b")
    executor = BackupTaskExecutor(lambda s: (MagicMock(host="h", port=1, user="u", password="p", engine="mysql", release=None), ""), MagicMock())
    for _ in range(2):
        assert executor.execute(schedule)[0] is True
    names = sorted(os.listdir(schedule.output_dir))
    assert len(names) == 2 and all(n.startswith("a_b_") for n in names)
    assert all(os.sep not in n for n in names)


def test_resolution_errors_are_recorded_as_failed_jobs(env):
    env.engine.tunnel_configs = {}
    env.config_manager.load_config.return_value = {"tunnels": []}
    ok, message = env.scheduler._execute_backup(env.schedule())
    assert ok is False and "터널 설정을 찾을 수 없습니다" in message
    assert history_records()[0].status == jh.STATUS_FAILED


def test_postgresql_backup_connects_to_the_schedule_database_and_records_it(env, monkeypatch):
    seen = {}

    class Capture(FakeExporter):
        def __init__(self, config):
            seen["database"], seen["engine"] = config.database, config.engine

    FakeExporter.outcome = (True, "ok")
    monkeypatch.setattr("src.exporters.rust_dump_exporter.RustDumpExporter", Capture)
    env.config_manager.load_config.return_value = {"tunnels": [{"id": "t1", "db_engine": "postgresql", "name": "PG"}]}
    schedule = env.schedule(database="sales")
    assert env.scheduler._execute_backup(schedule)[0] is True
    assert seen == {"database": "sales", "engine": "postgresql"}
    assert history_records()[0].target == "sales.app"
