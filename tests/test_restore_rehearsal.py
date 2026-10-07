"""Restore rehearsal: non-production targets only, candidate-only restore, verification, owned cleanup, recording."""
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.core import job_history as jh
from src.core import scheduled_backup_store as store
from src.core.restore_rehearsal import REPORT_NAME, RestoreRehearsal, rehearsal_target_error
from src.core.schedule_config import ScheduleConfig


def schedule(**overrides):
    values = dict(id="s1", name="nightly", tunnel_id="src", schema="app", output_dir="C:/out",
                  rehearsal_tunnel_id="tgt", rehearsal_schema="rehearsal")
    values.update(overrides)
    return ScheduleConfig(**values)


def conn(host="127.0.0.1", port=3306, engine="mysql"):
    return SimpleNamespace(host=host, port=port, user="u", password="pw", engine=engine, release=None)


class FakeFacade:
    def __init__(self, can_cleanup=True, cleanup_error=None):
        self.calls, self.can_cleanup, self.cleanup_error = [], can_cleanup, cleanup_error
        self.client = MagicMock()
        self.candidate_exists = True

    def restore_backups(self, payload):
        self.calls.append(payload["action"])
        assert payload["endpoint"]["database"] == "rehearsal"
        if payload["action"] == "list":
            return {"backups": [{"restore_id": "r1", "candidate": {"exists": self.candidate_exists}}]}
        if payload["action"] == "cleanup_plan":
            assert payload["target"] == "candidate"
            return {"can_cleanup": self.can_cleanup, "plan_digest": "d", "blockers": ["still promoted"]}
        if self.cleanup_error:
            raise RuntimeError(self.cleanup_error)
        self.candidate_exists = False
        return {"message": "dropped"}


def make_importer(tmp_path, restored_rows=None, verified=True, unchanged=True, ok=True):
    class FakeImporter:
        seen = {}

        def __init__(self, config, facade=None):
            FakeImporter.seen["config"] = config

        def import_dump(self, input_dir, target_schema, threads, import_mode, raw_output_callback):
            FakeImporter.seen.update(mode=import_mode, target=target_schema)
            report = tmp_path / "import_report.json"
            rows = restored_rows if restored_rows is not None else {"parent": 2, "child": 3}
            report.write_text(json.dumps({"verification": {"actual_row_counts": rows,
                                                           "content_digests": {k: {} for k in rows}}}))
            raw_output_callback(json.dumps({
                "event": "safe_restore_ready", "restore_id": "r1", "status": "ready_for_switch",
                "verified": verified, "original_unchanged": unchanged, "report_path": str(report),
                "candidate_target": {"database": "tf_restore_r1"}, "blockers": []}))
            return ok, "msg", {}

    return FakeImporter


@pytest.fixture
def env(tmp_path, monkeypatch):
    jh.make_history = lambda h=jh.JobHistory(tmp_path / "jobs.json"): h
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "_tunnelforge_dump.json").write_text(json.dumps({"tables": [
        {"name": "parent", "rows": 2}, {"name": "child", "rows": 3}]}))
    store.write_marker(str(backup), "s1", "nightly", store.STATE_COMPLETED)
    tunnels = {"tgt": {"id": "tgt", "environment": "development"}}
    connector = MagicMock()
    connector.connect.return_value = (True, "")
    connector.schema_exists.return_value = True
    facade = FakeFacade()

    holder = SimpleNamespace(importer=None)

    def build(**kwargs):
        holder.importer = make_importer(tmp_path, **kwargs)
        monkeypatch.setattr("src.exporters.rust_dump_exporter.RustDumpImporter", holder.importer)
        return RestoreRehearsal(lambda s: (conn(), ""), tunnels.get, lambda *a, **k: connector, lambda: facade)

    return SimpleNamespace(backup=str(backup), tunnels=tunnels, connector=connector,
                           facade=facade, build=build, holder=holder)


def records():
    return jh.make_history().list()


# ------------------------------------------------------------------ target rules

def test_production_unset_and_missing_targets_are_rejected_and_dev_staging_allowed():
    sch = schedule()
    assert "운영" in rehearsal_target_error({"environment": "production"}, sch)
    assert "개발/스테이징" in rehearsal_target_error({"id": "t"}, sch)  # unset environment fails closed
    assert "찾을 수 없습니다" in rehearsal_target_error(None, sch)
    assert rehearsal_target_error({"environment": "development"}, sch) is None
    assert rehearsal_target_error({"environment": "staging"}, sch) is None
    assert rehearsal_target_error(None, schedule(rehearsal_tunnel_id="")) is None  # option off
    assert "스키마" in rehearsal_target_error({"environment": "staging"}, schedule(rehearsal_schema=""))


def test_scheduler_refuses_to_save_a_production_rehearsal_target():
    from src.core.scheduler import BackupScheduler

    cm = MagicMock()
    cm.get_app_setting.side_effect = lambda key, default=None: [] if key == "schedules" else default
    cm.load_config.return_value = {"tunnels": [{"id": "tgt", "environment": "production"}]}
    scheduler = BackupScheduler(cm, MagicMock(tunnel_configs={}))
    assert "운영" in scheduler.validate_schedule(schedule())


# ------------------------------------------------------------------ flow

def test_passing_rehearsal_restores_to_a_candidate_verifies_and_cleans_only_the_owned_candidate(env):
    outcome = env.build().run(schedule(), env.backup, conn(port=3307))
    assert outcome.ok and not outcome.candidate_retained
    assert env.facade.calls == ["list", "cleanup_plan", "cleanup_apply"]
    report = json.loads(open(os.path.join(env.backup, REPORT_NAME), encoding="utf-8").read())
    assert report["result"] == "passed" and report["candidate_namespace"] == "tf_restore_r1"
    assert report["target_original_unchanged"] is True and report["candidate_cleanup"] == "cleaned"
    assert [t["restored_rows"] for t in report["tables"]] == [2, 3]
    assert "pw" not in json.dumps(report)
    record = records()[0]
    assert record.kind == jh.KIND_RESTORE_REHEARSAL and record.status == jh.STATUS_COMPLETED
    assert record.target == "rehearsal" and record.report_path.endswith(REPORT_NAME)
    assert store.read_marker(env.backup).get("hold") == ""


def test_the_restore_runs_in_safe_mode_against_the_existing_target_namespace(env):
    env.build().run(schedule(), env.backup, conn(port=3307))
    seen = env.holder.importer.seen
    assert seen["mode"] == "safe" and seen["target"] == "rehearsal"


def test_row_count_mismatch_fails_but_the_candidate_is_still_cleaned(env):
    outcome = env.build(restored_rows={"parent": 2, "child": 1}).run(schedule(), env.backup, conn(port=3307))
    assert not outcome.ok and "child" in outcome.message
    assert env.facade.calls[-1] == "cleanup_apply"
    record = records()[0]
    assert record.status == jh.STATUS_FAILED and "child" in record.error_summary


def test_an_unverified_restore_is_a_failure(env):
    outcome = env.build(verified=False).run(schedule(), env.backup, conn(port=3307))
    assert not outcome.ok and "검증되지" in outcome.message


def test_a_blocked_cleanup_retains_the_candidate_and_holds_the_backup_from_retention(env):
    env.facade.can_cleanup = False
    outcome = env.build().run(schedule(), env.backup, conn(port=3307))
    assert not outcome.ok and outcome.candidate_retained and "정리할 수 없어" in outcome.message
    assert "cleanup_apply" not in env.facade.calls
    assert store.read_marker(env.backup)["hold"] == "rehearsal_candidate_retained"
    owned = store.list_owned(os.path.dirname(env.backup), "s1")
    old = datetime.now(timezone.utc) - timedelta(days=900)
    owned[0].created_at = old
    assert store.select_for_deletion(owned * 1, 1, 1) == [], "a held folder is never deleted"


def test_a_missing_target_namespace_is_refused_and_nothing_is_created(env):
    env.connector.schema_exists.return_value = False
    outcome = env.build().run(schedule(), env.backup, conn(port=3307))
    assert not outcome.ok and "없습니다" in outcome.message
    env.connector.schema_exists.assert_called_once_with(schedule().rehearsal_schema)
    assert env.facade.calls == []


def test_the_backup_source_namespace_can_not_be_the_rehearsal_target(env):
    outcome = env.build().run(schedule(rehearsal_schema="app"), env.backup, conn())
    assert not outcome.ok and "같은 네임스페이스" in outcome.message
    assert env.facade.calls == []


def test_engine_mismatch_is_refused(env):
    outcome = env.build().run(schedule(), env.backup, conn(engine="postgresql"))
    assert not outcome.ok and "같은 DB 엔진" in outcome.message


def test_production_target_is_refused_at_run_time_too(env):
    env.tunnels["tgt"]["environment"] = "production"
    outcome = env.build().run(schedule(), env.backup, conn(port=3307))
    assert not outcome.ok and "운영" in outcome.message and env.facade.calls == []
