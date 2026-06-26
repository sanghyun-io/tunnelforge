import importlib.util
import json
from pathlib import Path


def _load_capture():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "capture-live-ui-migration-evidence.py"
    )
    spec = importlib.util.spec_from_file_location("capture_live_ui_migration_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_validator():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "validate-live-ui-migration-evidence.py"
    )
    spec = importlib.util.spec_from_file_location("validate_live_ui_migration_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_direction_evidence_uses_worker_results_and_heartbeat():
    capture = _load_capture()

    evidence = capture.direction_evidence_from_worker_results(
        migrate_payload={"success": True, "rows_copied": "1000000"},
        verify_payload={"success": True, "mismatches": []},
        worker_progress_events=12,
        heartbeat={"samples": 50, "max_gap_ms": 240, "max_allowed_gap_ms": 1000},
        notes="local container run",
    )

    assert evidence["rows_migrated"] == 1_000_000
    assert evidence["migration_success"] is True
    assert evidence["verify_success"] is True
    assert evidence["mismatches"] == 0
    assert evidence["worker_progress_events"] == 12
    assert evidence["ui_heartbeat"]["max_gap_ms"] == 240
    assert evidence["notes"] == "local container run"


def test_build_evidence_report_matches_validator_shape(tmp_path):
    capture = _load_capture()
    validator = _load_validator()
    direction = {
        "rows_migrated": 1_000_000,
        "migration_success": True,
        "verify_success": True,
        "mismatches": 0,
        "worker_progress_events": 8,
        "ui_heartbeat": {
            "samples": 60,
            "max_gap_ms": 200,
            "max_allowed_gap_ms": 1000,
        },
        "notes": "captured through CrossEngineMigrationWorker",
    }

    report = capture.build_evidence_report(
        git_sha="807fcdb",
        source_type="local_containers",
        directions={
            "mysql_to_postgresql": dict(direction),
            "postgresql_to_mysql": dict(direction),
        },
        stress_10m={
            "source_type": "synthetic_adapter",
            "rows": 10_000_000,
            "resume_success": True,
            "verify_success": True,
            "mismatches": 0,
            "peak_rss_mb": 512,
            "rss_limit_mb": 2048,
            "notes": "validated archived Rust Core stress artifact",
        },
    )

    report_path = tmp_path / "live-ui-migration-evidence.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    summary = validator.validate_report(report_path)
    assert report["issue"] == 136
    assert report["directions"]["mysql_to_postgresql"]["notes"].startswith("captured")
    assert summary == {"directions_checked": 2, "rows_checked": 12_000_000}


def test_migration_payload_uses_create_only_mode_and_chunk_size():
    capture = _load_capture()
    source = capture.endpoint("mysql", "127.0.0.1", 3406, "root", "test", "source_db")
    target = capture.endpoint("postgresql", "127.0.0.1", 55432, "postgres", "test", "target_db")
    schema = capture.mysql_schema("tf_live_mysql_pg_1m")

    payload = capture.migration_payload(
        source_engine="mysql",
        target_engine="postgresql",
        source=source,
        target=target,
        schema=schema,
        chunk_size=2500,
    )

    assert payload["source_engine"] == "mysql"
    assert payload["target_engine"] == "postgresql"
    assert payload["source"]["port"] == 3406
    assert payload["target"]["port"] == 55432
    assert payload["schema"]["tables"][0]["name"] == "tf_live_mysql_pg_1m"
    assert payload["execution_options"] == {"mode": "create_only", "chunk_size": 2500}


def test_seed_local_containers_refuses_non_tf_live_tables():
    capture = _load_capture()

    try:
        capture.seed_local_containers(
            mysql_container="tf-live-mysql",
            postgres_container="tf-live-postgres",
            mysql_user="root",
            mysql_password="test",
            mysql_database="tunnelforge_live",
            postgres_user="postgres",
            postgres_database="tunnelforge_live",
            rows=1000,
            mysql_to_postgres_table="production_table",
            postgres_to_mysql_table="tf_live_pg_mysql_1m",
        )
    except ValueError as exc:
        assert "tf_live_" in str(exc)
    else:
        raise AssertionError("seed_local_containers accepted a non tf_live_ table")
