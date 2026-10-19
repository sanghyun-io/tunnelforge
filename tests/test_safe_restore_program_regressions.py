import json
from unittest.mock import MagicMock
from PyQt6.QtWidgets import QApplication
from src.exporters.rust_dump_exporter import describe_core_code, describe_program_warnings
from src.ui.dialogs.db_dialogs import RustDumpImportDialog

GENERIC = "테이블은 교체했지만 저장 프로그램이 빠져 있습니다. 보고서를 확인하고 복구하세요."
CAUSE = "ERROR 1227 (42000): Access denied; you need (at least one of) the SYSTEM_USER privilege(s) for this operation"

def _table(name):
    cols = [{"name": c, "data_type": "int", "column_type": "int", "nullable": False, "default": None,
             "extra": "", "collation": None, "comment": ""} for c in ("id", "a", "b")]
    return {"name": name, "ddl": f"CREATE TABLE `{name}` (`id` int NOT NULL, PRIMARY KEY (`id`))",
            "static_digest": "0" * 64, "table_id": 1, "columns": ["id", "a", "b"],
            "normalized": {"name": name, "columns": cols,
                           "indexes": [{"name": "PRIMARY", "columns": ["id"], "unique": True}], "foreign_keys": []},
            "content_digest": None}

def _rust_result(tables):
    plan = {"original_database": "shop", "original_tables": {t: _table(t) for t in tables},
            "candidate_tables": {t: _table(t) for t in tables}, "warnings": []}
    state = {"success": False, "status": "promoted_incomplete", "phase": "triggers_attached",
        "original_unchanged": False, "backup_namespace": "tf_backup_x", "report_path": "C:/r.json",
        "plan": plan, "warnings": [],
        "error": "저장 프로그램을 모두 복원하지 못했습니다. 저장된 보고서에서 빠진 객체를 확인하세요.",
        "message": GENERIC, "missing_programs": ["routine:f"],
        "pending_program": {"kind": "FUNCTION", "name": "f"},
        "programs_failed": [{"kind": "FUNCTION", "name": "f", "code": "program_restore_unavailable", "error": CAUSE, "target_unchanged": True}],
        "programs_imported": [{"kind": "PROCEDURE", "name": "p", "enabled": False, "status": None}],
        "program_warnings": [{"kind": "PROCEDURE", "name": "p", "code": "routine_grants_reset"}],
        "event": "result", "command": "dump.promote"}
    return json.loads(json.dumps(state, sort_keys=True, ensure_ascii=False))  # serde_json BTreeMap order

def _dialog(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.finish_promotion_job", lambda *a: None)
    d = RustDumpImportDialog(); d._test_app = app; d._promotion_action = "confirm"
    d._show_failure = MagicMock(); d._show_report_link = MagicMock()
    return d

def _logs(d):
    return "\n".join(d.txt_log.item(i).text() for i in range(d.txt_log.count()))

def test_window_names_cause_and_warnings(monkeypatch):
    d = _dialog(monkeypatch)
    try:
        r = _rust_result(["t1"])
        d._on_promotion_finished(False, r["message"], r)
        title, text, details = d._show_failure.call_args.args
        shown = text + "\n" + details + "\n" + _logs(d)
        assert describe_core_code("program_restore_unavailable", "f") in shown, "cause sentence missing"
        assert describe_program_warnings(r["program_warnings"])[0] in shown, "routine_grants_reset for p missing"
    finally:
        d.close()

def test_raw_log_and_audit_keep_programs_failed(monkeypatch):
    d = _dialog(monkeypatch)
    try:
        r = _rust_result([f"t{i}" for i in range(4)])
        d.on_raw_output(json.dumps(r, ensure_ascii=False))
        d._on_promotion_finished(False, r["message"], r)
        assert d.import_audit["promotion_result"]["programs_failed"] is not None
        assert "1227" in _logs(d)
    finally:
        d.close()
