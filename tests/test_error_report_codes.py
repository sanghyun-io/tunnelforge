import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.core.db_core_client import DbCoreServiceError
from src.core.error_report_codes import classify_error_code, is_classified_error_code
from src.exporters.rust_dump_exporter import _is_operation_scoped_import_error


@pytest.mark.parametrize(
    ("error_code", "message", "expected"),
    [
        (None, "incompatible_surviving_fk: target-only foreign keys are incompatible: shop.fk_x", "INCOMPATIBLE_SURVIVING_FK"),
        (None, "load_failed: secret_table: create_table failed: mysql create table error: "
               "MySqlError { ERROR 1822 (HY000): Failed to add the foreign key constraint }", "LOAD_FAILED:MYSQL-1822"),
        (None, "post_load_validation_failed: postgresql SQL execution error: db error; code=23503; message=x", "POST_LOAD_VALIDATION_FAILED:PG-23503"),
        # `code=` / `ERROR n` inside a user path or identifier is not a server code.
        (None, r"[Errno 2] No such file: 'D:\exports\code=KR001\_export_metadata.json'", None),
        (None, "load_failed: code=AB123: create_table failed: postgresql create table error: db error; code=23505; message=x",
         "LOAD_FAILED:PG-23505"),
        (None, "export failed for table `ERROR 12345 (ABCDE): x`", None),
        (None, "mysql connection error: TLS required (error_code=tls_unavailable)", "TLS_UNAVAILABLE"),
        ("unsupported_objects", "dump.run refused: objects secret_view", "UNSUPPORTED_OBJECTS"),
        (None, "MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED:RELOAD:denied", "MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED"),
        (None, "mysql connection error: MySqlError { ERROR 1045 (28000): Access denied for user 'secret'@'host' }", "MYSQL-1045"),
        # A leading identifier that is not an allowlisted code (a table name) is never reported.
        (None, "secret_table: something failed", None),
        ("secret_code_from_somewhere", "plain failure", None),
        (None, "", None),
    ],
)
def test_classify_error_code_reports_only_allowlisted_tokens(error_code, message, expected):
    assert classify_error_code(error_code, message) == expected
    if expected:
        assert is_classified_error_code(expected)


def test_is_classified_error_code_rejects_free_text_and_identifiers():
    for value in ("SECRET_TABLE", "LOAD_FAILED:SECRET", "load_failed", "PG-2350", "MYSQL-12", None, 7, "X" * 65):
        assert not is_classified_error_code(value)


def test_builder_uses_classified_code_and_splits_fingerprints():
    from src.core.error_report_builder import build_error_report

    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: (
        "550e8400-e29b-41d4-a716-446655440000" if key == "error_reporting_installation_id" else default
    )

    def build(code):
        return build_error_report(
            config_manager, operation_kind="import", db_engine="mysql", phase="dump.import",
            error_message="Rust DB Core import operation failed.", error_code=code,
        )

    surviving = build("INCOMPATIBLE_SURVIVING_FK")
    load = build("LOAD_FAILED:MYSQL-1822")
    unknown = build("SECRET_TABLE")
    assert surviving["error"]["error_code"] == "INCOMPATIBLE_SURVIVING_FK"
    assert load["error"]["error_code"] == "LOAD_FAILED:MYSQL-1822"
    assert "error_code" not in unknown["error"]
    assert "SECRET_TABLE" not in json.dumps(unknown)
    fingerprints = {r["report"]["error_fingerprint"] for r in (surviving, load, unknown)}
    assert len(fingerprints) == 3


def test_exporter_remembers_only_the_classified_code():
    from src.exporters.rust_dump_exporter import RustDumpImporter

    importer = RustDumpImporter.__new__(RustDumpImporter)
    importer.last_error_code = None
    importer._remember_error_code(DbCoreServiceError(
        "load_failed: customer_secret: create_table failed: MySqlError { ERROR 1822 (HY000): x }", error_code=None,
    ))
    assert importer.last_error_code == "LOAD_FAILED:MYSQL-1822"


def test_preflight_refusals_are_operation_scoped():
    for message in (
        "incompatible_surviving_fk: target-only foreign keys are incompatible; target tables were not changed: a.fk",
        "target_dependency_preflight_failed: PostgreSQL target dependencies prevent replacement",
        "ddl_preflight_failed: t: server rejected the table definition",
    ):
        assert _is_operation_scoped_import_error(message)
    assert not _is_operation_scoped_import_error("load_failed: t: create_table failed")


@pytest.mark.parametrize(
    ("module", "cls", "kind", "phase"),
    [
        ("src.ui.dialogs.db_import_dialog", "RustDumpImportDialog", "import", "dump.import"),
        ("src.ui.dialogs.db_export_dialog", "RustDumpExportDialog", "export", "dump.run"),
    ],
)
def test_dialog_report_passes_worker_error_code(module, cls, kind, phase):
    import importlib

    dialog_class = getattr(importlib.import_module(module), cls)
    calls = []
    dialog = SimpleNamespace(
        config_manager=object(),
        connector=SimpleNamespace(engine="mysql"),
        worker=SimpleNamespace(error_code="LOAD_FAILED:MYSQL-1822"),
        _start_error_report_worker=lambda **kwargs: calls.append(kwargs),
    )
    dialog_class._report_error_anonymously(dialog)
    dialog.worker = SimpleNamespace(error_code=None)
    dialog_class._report_error_anonymously(dialog)
    base = {"operation_kind": kind, "db_engine": "mysql", "phase": phase}
    assert calls == [{**base, "error_code": "LOAD_FAILED:MYSQL-1822"}, base]
