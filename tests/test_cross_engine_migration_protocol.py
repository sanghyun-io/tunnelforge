import json
import os
import sys
from pathlib import Path

import pytest

from src.core.cross_engine_migration import (
    DatabaseEngine,
    HelperProtocolError,
    MigrationDirection,
    _db_core_executable_names,
    _db_core_frozen_candidate_dirs,
    build_helper_request,
    db_core_executable,
    load_resume_state,
    next_workflow_command,
    parse_helper_event,
    render_result_report,
    save_resume_state,
    schema_from_inspect_result,
    state_key_from_payload,
)


def test_direction_from_engines_mysql_to_postgresql():
    direction = MigrationDirection.from_engines(DatabaseEngine.MYSQL, DatabaseEngine.POSTGRESQL)
    assert direction == MigrationDirection.MYSQL_TO_POSTGRESQL


def test_direction_from_engines_rejects_same_engine():
    with pytest.raises(ValueError):
        MigrationDirection.from_engines(DatabaseEngine.MYSQL, DatabaseEngine.MYSQL)


def test_build_helper_request_is_jsonl():
    line = build_helper_request("preflight", {"source_engine": "mysql"}, "req-1")
    assert line.endswith("\n")
    data = json.loads(line)
    assert data["command"] == "preflight"
    assert data["request_id"] == "req-1"
    assert data["payload"]["source_engine"] == "mysql"


def test_parse_issue_event():
    event = parse_helper_event(json.dumps({
        "event": "issue",
        "request_id": "req-1",
        "issue": {
            "severity": "error",
            "location": "direction",
            "message": "bad direction",
            "suggestion": "choose another target",
            "blocking": True,
        }
    }))

    assert event.event == "issue"
    assert event.issue is not None
    assert event.issue.blocking is True
    assert event.issue.location == "direction"


def test_parse_result_event_keeps_payload():
    event = parse_helper_event('{"event":"result","success":true,"plan":{"ddl":[]}}')
    assert event.event == "result"
    assert event.success is True
    assert event.payload["plan"]["ddl"] == []


def test_parse_helper_event_rejects_invalid_json():
    with pytest.raises(HelperProtocolError):
        parse_helper_event("not json")


def test_schema_from_inspect_result_extracts_schema():
    schema = {"tables": [{"name": "users", "columns": []}]}
    payload = {
        "event": "result",
        "command": "inspect",
        "success": True,
        "schema": schema,
    }

    assert schema_from_inspect_result(payload) == schema
    assert schema_from_inspect_result({"event": "result", "command": "plan"}) is None


def test_render_result_report_includes_counts_and_mismatches():
    report = render_result_report({
        "event": "result",
        "command": "verify",
        "success": False,
        "rows_copied": 2,
        "mismatches": [
            {"table": "users", "kind": "cell", "column": "name"},
        ],
    })

    assert "Command: verify" in report
    assert "Success: False" in report
    assert "Rows copied: 2" in report
    assert "Mismatches: 1" in report
    assert "users cell name" in report


def test_render_result_report_includes_direction_readiness():
    report = render_result_report({
        "event": "result",
        "command": "readiness",
        "success": False,
        "directions": [
            {
                "direction": "mysql_to_postgresql",
                "success": True,
                "table_count": 2,
                "issues": [],
            },
            {
                "direction": "postgresql_to_mysql",
                "success": False,
                "table_count": 1,
                "issues": [{"blocking": True}],
            },
        ],
    })

    assert "Direction Readiness:" in report
    assert "mysql_to_postgresql: ready" in report
    assert "postgresql_to_mysql: blocked" in report


def test_render_result_report_includes_detailed_guide_rows_and_sql():
    report = render_result_report({
        "event": "result",
        "command": "guide",
        "success": True,
        "directions": [{
            "direction": "mysql_to_postgresql",
            "success": True,
            "table_count": 1,
            "issues": [],
            "guide": {
                "create_table_sql": ["CREATE TABLE \"users\" (\"id\" INTEGER);"],
                "sequence_reset_sql": [],
                "post_data_sql": ["CREATE INDEX \"idx_users_id\" ON \"users\" (\"id\");"],
                "tables": [{
                    "table": "users",
                    "row_count": 1,
                    "columns": [{
                        "name": "id",
                        "source_type": "int(11)",
                        "target_type": "INTEGER",
                    }],
                    "row_samples": [{"id": "1", "name": "alpha"}],
                    "insert_example_sql": "INSERT INTO \"users\" (\"id\") VALUES ('1')",
                }],
            },
        }],
    })

    assert "Create table SQL:" in report
    assert "CREATE TABLE \"users\"" in report
    assert "Column id: int(11) -> INTEGER" in report
    assert 'Row sample 1: {"id": "1", "name": "alpha"}' in report
    assert "Insert example:" in report


def test_next_workflow_command_advances_only_on_success():
    assert next_workflow_command("inspect", True) == "preflight"
    assert next_workflow_command("preflight", True) == "plan"
    assert next_workflow_command("verify", True) is None
    assert next_workflow_command("migrate", False) is None
    assert next_workflow_command("unknown", True) is None


def test_resume_state_save_load_roundtrip(tmp_path):
    state = {
        "current_phase": "data",
        "tables": [{"table": "users", "completed": False, "rows_copied": 2}],
    }

    path = save_resume_state("test_state", state, base_dir=tmp_path)

    assert path.exists()
    assert load_resume_state("test_state", base_dir=tmp_path) == state
    assert load_resume_state("missing", base_dir=tmp_path) is None


def test_state_key_from_payload_is_filename_safe():
    key = state_key_from_payload({
        "source": {"engine": "mysql", "database": "app/db"},
        "target": {"engine": "postgresql", "database": "target db"},
        "schema": {"tables": [{"name": "users"}]},
    })

    assert "/" not in key
    assert " " not in key
    assert "mysql" in key
    assert "postgresql" in key


def test_db_core_executable_checks_frozen_app_directory(monkeypatch, tmp_path):
    exe_name = "tunnelforge-core.exe" if os.name == "nt" else "tunnelforge-core"
    app_dir = tmp_path / "TunnelForge"
    app_dir.mkdir()
    core = app_dir / exe_name
    core.write_text("", encoding="utf-8")
    app_exe = app_dir / ("TunnelForge.exe" if os.name == "nt" else "TunnelForge")
    app_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app_exe))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert db_core_executable() == str(core)


def test_db_core_executable_uses_unsuffixed_core_name_on_macos():
    assert _db_core_executable_names("posix") == ["tunnelforge-core"]


def test_db_core_frozen_candidate_dirs_include_macos_app_bundle_locations():
    executable = Path("/Applications/TunnelForge.app/Contents/MacOS/TunnelForge")

    candidates = _db_core_frozen_candidate_dirs(executable)

    assert executable.parent in candidates
    assert executable.parent.parent / "Frameworks" in candidates
    assert executable.parent.parent / "Resources" in candidates
