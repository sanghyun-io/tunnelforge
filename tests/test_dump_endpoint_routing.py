from types import SimpleNamespace
from unittest.mock import MagicMock
import json
import pytest

from src.exporters.rust_dump_exporter import build_rust_dump_config, RustDumpExporter, RustDumpImporter
from src.core.postgres_connector import PostgresConnector


def test_postgres_dump_keeps_database_separate_from_namespace(tmp_path):
    connector = SimpleNamespace(host="127.0.0.1", port=15433, user="u", password="p", engine="postgresql", database="analytics")
    facade = MagicMock()
    facade.run_dump.return_value = {"success": True}
    exporter = RustDumpExporter(build_rust_dump_config(connector), facade)
    assert exporter.export_full_schema("reporting", str(tmp_path))[0]
    endpoint = facade.run_dump.call_args.args[0]["source"]
    assert endpoint["database"] == "analytics"
    assert endpoint["schema"] == "reporting"
    assert endpoint["port"] == 15433


def test_postgres_import_uses_target_connection_database_and_manifest_namespace(tmp_path):
    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({
        "database": "source_db", "source_engine": "postgresql", "source_schema": "reporting", "tables": [],
    }), encoding="utf-8")
    connector = SimpleNamespace(host="localhost", port=5432, user="u", password="p", engine="postgresql", database="target_db")
    facade = MagicMock()
    facade.import_dump.return_value = {"success": True}
    importer = RustDumpImporter(build_rust_dump_config(connector), facade)
    assert importer.import_dump(str(tmp_path), import_mode="replace")[0]
    endpoint = facade.import_dump.call_args.args[0]["target"]
    assert endpoint["database"] == "target_db"
    assert endpoint["schema"] == "reporting"


def test_postgres_connector_exposes_dump_metadata_methods():
    facade = MagicMock()
    facade.catalog.return_value = ["reporting"]
    connector = PostgresConnector("localhost", 5432, "u", "p", "app", facade)
    connector.connection = MagicMock()
    assert connector.get_schemas() == ["reporting"]
    assert facade.catalog.call_args.args[1] == "schemas"
    assert callable(connector.get_tables)


def test_import_server_timezone_explicitly_disables_manifest_override(tmp_path):
    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({"database": "app", "tables": []}), encoding="utf-8")
    connector = SimpleNamespace(host="localhost", port=3306, user="u", password="p", engine="mysql")
    facade = MagicMock()
    facade.import_dump.return_value = {"success": True}
    importer = RustDumpImporter(build_rust_dump_config(connector), facade)
    assert importer.import_dump(str(tmp_path), import_mode="replace", use_source_timezone=False)[0]
    assert facade.import_dump.call_args.args[0]["use_source_timezone"] is False


@pytest.mark.parametrize("version", [1, 2, 3])
def test_python_metadata_preserves_dump_format_version(tmp_path, version):
    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({
        "format": "tunnelforge-dump", "format_version": version,
        "database": "app", "tables": [],
    }), encoding="utf-8")
    connector = SimpleNamespace(host="localhost", port=3306, user="u", password="p", engine="mysql")
    facade = MagicMock()
    facade.import_dump.return_value = {"success": True}
    importer = RustDumpImporter(build_rust_dump_config(connector), facade)
    metadata = []
    assert importer.import_dump(str(tmp_path), import_mode="replace", metadata_callback=metadata.append)[0]
    assert metadata[0]["format_version"] == version
    assert json.loads((tmp_path / "_tunnelforge_dump.json").read_text(encoding="utf-8"))["format_version"] == version


@pytest.mark.parametrize("source_engine,target_engine,source_schema", [
    ("postgresql", "postgresql", None),
    ("mysql", "postgresql", None),
    ("postgresql", "mysql", "reporting"),
])
def test_import_requires_explicit_destination_when_original_is_unknown_or_cross_engine(tmp_path, source_engine, target_engine, source_schema):
    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({
        "source_engine": source_engine, "source_schema": source_schema,
        "database": "source_db", "tables": [],
    }), encoding="utf-8")
    connector = SimpleNamespace(host="localhost", port=5432 if target_engine == "postgresql" else 3306,
                                user="u", password="p", engine=target_engine, database="target_db")
    facade = MagicMock()
    facade.import_dump.return_value = {"success": True}
    importer = RustDumpImporter(build_rust_dump_config(connector), facade)
    assert not importer.import_dump(str(tmp_path), import_mode="replace")[0]
    facade.import_dump.assert_not_called()
    assert importer.import_dump(str(tmp_path), import_mode="replace", target_schema="chosen")[0]
    endpoint = facade.import_dump.call_args.args[0]["target"]
    assert endpoint["schema" if target_engine == "postgresql" else "database"] == "chosen"


def test_import_completion_surfaces_verification_warnings(tmp_path):
    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({"database": "app", "tables": []}), encoding="utf-8")
    connector = SimpleNamespace(host="localhost", port=3306, user="u", password="p", engine="mysql")
    facade = MagicMock()
    warnings = ["Legacy export used independent snapshots", "Triggers were not exported"]
    facade.import_dump.return_value = {"success": True, "verification": {"warnings": warnings}}
    progress = []
    success, message, _ = RustDumpImporter(build_rust_dump_config(connector), facade).import_dump(str(tmp_path), import_mode="replace", progress_callback=progress.append)
    assert success
    for warning in warnings:
        assert warning in message
        assert any(warning in line for line in progress)


@pytest.mark.parametrize("ready", [True, False])
def test_safe_import_requires_verified_candidate_and_reports_pending_switch(tmp_path, ready):
    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({"database": "app", "tables": []}), encoding="utf-8")
    connector = SimpleNamespace(host="localhost", port=3306, user="u", password="p", engine="mysql")
    facade = MagicMock()
    facade.import_dump.return_value = {
        "success": True, "status": "ready_for_switch", "verified": ready,
        "original_unchanged": True, "cutover_pending": True,
        "candidate_target": {"engine": "mysql", "host": "localhost", "port": 3306, "database": "restore_candidate", "password": "never-copy"},
    }
    raw = []
    success, message, _ = RustDumpImporter(build_rust_dump_config(connector), facade).import_dump(str(tmp_path), raw_output_callback=raw.append)
    assert success is ready
    assert facade.import_dump.call_args.args[0]["mode"] == "safe"
    if ready:
        assert "restore_candidate" in message
        assert "전환 대기" in message
        assert raw and "never-copy" not in raw[-1]


def test_safe_import_accepts_verified_requested_new_namespace(tmp_path):
    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({"database": "requested", "tables": []}), encoding="utf-8")
    connector = SimpleNamespace(host="localhost", port=3306, user="u", password="p", engine="mysql")
    facade = MagicMock()
    facade.import_dump.return_value = {"success": True, "status": "completed_new_target", "verified": True,
        "original_unchanged": True, "cutover_pending": False, "namespace_existed": False, "namespace_created": True,
        "candidate_target": {"engine": "mysql", "database": "requested"}}
    success, message, _ = RustDumpImporter(build_rust_dump_config(connector), facade).import_dump(str(tmp_path), import_mode="safe")
    assert success
    assert "requested" in message and "전환 대기" not in message
