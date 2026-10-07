"""Opt-in real Python exporter/importer contract against the disposable Docker DB."""
import os
import json
from pathlib import Path
import subprocess
import uuid

import pytest

from src.core.db_core_service import DbCoreFacade, DbCoreServiceClient, DbEndpoint
from src.core.db_core_service import create_rust_db_connector
from src.exporters.rust_dump_exporter import RustDumpExporter, RustDumpImporter, build_rust_dump_config


@pytest.mark.skipif(not (os.getenv("TF_LIVE_DOCKER_RUNNER") or os.getenv("TF_LIVE_CORE")),
                    reason="requires disposable Docker database")
@pytest.mark.parametrize("engine", ["postgresql", "mysql"])
@pytest.mark.parametrize("restore_mode", ["replace", "safe", "safe_new", "safe_promote"])
def test_python_export_import_named_schema_roundtrip(engine, restore_mode):
    root = Path(__file__).resolve().parents[1]
    token = uuid.uuid4().hex[:12]
    source_schema, target_schema = f"tf_ui_src_{token}", f"tf_ui_dst_{token}"
    output = root / "build" / "ui-contract-test" / token

    def start_core(_command, **kwargs):
        return subprocess.Popen([
            "docker", "exec", "-i", os.environ["TF_LIVE_DOCKER_RUNNER"],
            "/workspace/migration_core/target/debug/tunnelforge-core",
        ], **kwargs)

    class DockerFacade(DbCoreFacade):
        def run_dump(self, payload, on_event=None):
            payload = dict(payload)
            payload["output_dir"] = "/workspace/" + Path(payload["output_dir"]).relative_to(root).as_posix()
            return super().run_dump(payload, on_event=on_event)

        def import_dump(self, payload, on_event=None):
            payload = dict(payload)
            payload["input_dir"] = "/workspace/" + Path(payload["input_dir"]).relative_to(root).as_posix()
            return super().import_dump(payload, on_event=on_event)

    if os.getenv("TF_LIVE_CORE"):
        # The core runs on this host (CI): no container path remapping.
        facade = DbCoreFacade(DbCoreServiceClient(executable=os.environ["TF_LIVE_CORE"]))
    else:
        facade = DockerFacade(DbCoreServiceClient(executable="docker-core", popen_factory=start_core))
    if engine == "postgresql":
        connector = create_rust_db_connector("postgresql", os.environ["TF_LIVE_PG_HOST"], 5432, "postgres",
                                             os.environ["TF_LIVE_PG_PASSWORD"], "tf_test", facade=facade)
    else:
        connector = create_rust_db_connector("mysql", os.environ["TF_LIVE_MYSQL_HOST"], 3306, "root",
                                             os.environ["TF_LIVE_MYSQL_PASSWORD"], "tf_test", facade=facade)
    quote = '"' if engine == "postgresql" else '`'
    def table(namespace):
        return f'{quote}{namespace}{quote}.samples'
    endpoint = DbEndpoint(engine=engine, host=connector.host, port=connector.port,
                          user=connector.user, password=connector.password, database="tf_test")
    candidate_namespace = None
    extra_namespaces = []
    try:
        for namespace in [source_schema] + ([target_schema] if restore_mode != "safe_new" else []):
            facade.execute_query(endpoint, f'CREATE SCHEMA {quote}{namespace}{quote}')
        temporal_type = "timestamp" if engine == "postgresql" else "datetime(6)"
        facade.execute_query(endpoint, f'CREATE TABLE {table(source_schema)} (id integer PRIMARY KEY, note text, amount numeric(20,4), stamp {temporal_type})')
        facade.execute_query(endpoint, f'CREATE VIEW {quote}{source_schema}{quote}.v_samples AS SELECT id, note FROM {table(source_schema)}')
        if restore_mode != "safe_new":
            facade.execute_query(endpoint, f'CREATE TABLE {table(target_schema)} (id integer PRIMARY KEY, note text, amount numeric(20,4), stamp {temporal_type})')
            facade.execute_query(endpoint, f"INSERT INTO {table(target_schema)} VALUES (999, 'original sentinel', 1.0000, NULL)")
            facade.execute_query(endpoint, f'CREATE VIEW {quote}{target_schema}{quote}.v_samples AS SELECT id, note FROM {table(target_schema)}')
        for row in [(1, "한글🙂\tline\nend", "12345678901234.5678", "2026-09-28 12:34:56.123456"),
                    (2, "", "-0.0001", None), (3, None, None, None)]:
            facade.execute_query(endpoint, f'INSERT INTO {table(source_schema)} VALUES (%s, %s, %s, %s)', row)
        assert connector.connect()[0]
        assert source_schema in connector.get_schemas(use_cache=False)
        assert connector.get_tables(source_schema, use_cache=False) == ["samples"]
        config = build_rust_dump_config(connector)
        def rows(namespace):
            amount = "amount::text" if engine == "postgresql" else "CAST(amount AS CHAR)"
            stamp = "stamp::text" if engine == "postgresql" else "CAST(stamp AS CHAR)"
            return facade.execute_query(endpoint, f'SELECT id, note, {amount} AS amount, {stamp} AS stamp FROM {table(namespace)} ORDER BY id')
        original_rows = rows(target_schema) if restore_mode != "safe_new" else None
        def view_rows(namespace):
            return facade.execute_query(endpoint, f'SELECT * FROM {quote}{namespace}{quote}.v_samples ORDER BY id')
        original_view_rows = view_rows(target_schema) if original_rows is not None else None
        ok, message = RustDumpExporter(config, facade).export_full_schema(source_schema, str(output))
        assert ok, message
        raw_events = []
        ok, message, result = RustDumpImporter(config, facade).import_dump(
            str(output), target_schema=target_schema, import_mode="safe" if restore_mode.startswith("safe") else restore_mode,
            raw_output_callback=lambda line: raw_events.append(json.loads(line)),
        )
        if restore_mode == "replace" and engine == "postgresql":
            assert not ok and "target_dependency_preflight_failed" in message, message
            assert rows(target_schema) == original_rows
            assert view_rows(target_schema) == original_view_rows
            assert not any(event.get("event") == "target_change" for event in raw_events)
            return
        assert ok, message
        assert result["samples"]["status"] == "done"
        if restore_mode.startswith("safe"):
            final = next(event for event in reversed(raw_events) if event.get("event") == "safe_restore_ready")
            candidate = final["candidate_target"]
            candidate_namespace = candidate["schema" if engine == "postgresql" else "database"]
            if restore_mode == "safe_new":
                assert candidate_namespace == target_schema
                assert final["status"] == "completed_new_target" and final["cutover_pending"] is False
            else:
                assert candidate_namespace.startswith("tf_restore_")
                assert candidate_namespace not in (source_schema, target_schema)
                assert rows(target_schema) == original_rows
                assert view_rows(target_schema) == original_view_rows
            assert rows(source_schema) == rows(candidate_namespace)
            assert view_rows(source_schema) == view_rows(candidate_namespace)
            if restore_mode == "safe_promote":
                promotion = {"action": "plan", "restore_id": final["restore_id"], "report_path": final["report_path"],
                    "target": dict(final["original_target"], user=connector.user, password=connector.password)}
                plan = facade.promote_dump(promotion)
                assert plan["can_promote"] is True, plan
                assert rows(target_schema) == original_rows
                result = facade.promote_dump(dict(promotion, action="confirm", plan_digest=plan["plan_digest"], overwrite_confirmed=True))
                assert result["success"] is True and result["status"] == "promoted", result
                backup = result["backup_namespace"]
                assert backup.startswith("tf_backup_") and backup not in (source_schema, target_schema)
                extra_namespaces.append(backup)
                clone = result.get("clone_database")
                if clone:
                    assert clone.startswith("tf_promote_") and clone not in (source_schema, target_schema)
                    extra_namespaces.append(clone)
                assert rows(target_schema) == rows(source_schema)
                assert rows(backup) == original_rows
                assert view_rows(target_schema) == view_rows(source_schema)
        else:
            assert rows(source_schema) == rows(target_schema)
            assert view_rows(source_schema) == view_rows(target_schema)
    finally:
        connector.disconnect()
        for namespace in [source_schema, target_schema] + ([candidate_namespace] if candidate_namespace and candidate_namespace.startswith("tf_restore_") else []) + extra_namespaces:
            cascade = " CASCADE" if engine == "postgresql" else ""
            facade.execute_query(endpoint, f'DROP SCHEMA IF EXISTS {quote}{namespace}{quote}{cascade}')
        facade.client.shutdown()
