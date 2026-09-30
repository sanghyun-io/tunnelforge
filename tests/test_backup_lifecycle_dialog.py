from unittest.mock import MagicMock

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.core.db_core_facade import DbCoreFacade
from src.ui.dialogs.backup_lifecycle_dialog import BackupLifecycleDialog

ENTRY = {
    "restore_id": "r1", "journal_status": "promoted",
    "backup": {"namespace": "tf_backup_1", "exists": True, "ownership": "proven", "verdict": "promoted",
               "tables": [{"name": "a", "rows": 2}, {"name": "b", "rows": 1}],
               "saved_view_alias_note": "not views over backup data"},
    "candidate": {"namespace": "tf_restore_r1", "exists": False},
}


def _dialog():
    app = QApplication.instance() or QApplication([])
    dialog = BackupLifecycleDialog({"engine": "mysql"}, ["C:/dump"])
    dialog._test_app = app  # keep QApplication alive
    return dialog


def test_facade_uses_restore_backups_command():
    facade = DbCoreFacade.__new__(DbCoreFacade)
    facade.client = MagicMock()
    facade.restore_backups({"action": "list"})
    facade.client.request.assert_called_once_with("restore.backups", {"action": "list"}, on_event=None)


def test_list_result_fills_table_and_states_alias_note():
    dialog = _dialog()
    try:
        dialog._on_list(True, "", {"backups": [ENTRY], "unproven_namespaces": [{"namespace": "tf_backup_9"}]})
        assert dialog.table.rowCount() == 1
        assert dialog.table.item(0, 2).text() == "tf_backup_1"
        assert dialog.table.item(0, 4).text() == "2개 (3)"
        assert dialog.table.item(0, 6).text() == "tf_restore_r1 (없음)"
        notes = dialog.notes.toPlainText()
        assert "not views over backup data" in notes and "tf_backup_9" in notes
    finally:
        dialog.close()


def test_blocked_plan_never_reaches_apply(monkeypatch):
    dialog = _dialog()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    dialog._request = MagicMock()
    dialog._confirm_cleanup = MagicMock(return_value=True)
    try:
        dialog._on_plan("backup", "r1", True, "", {"can_cleanup": False, "blockers": ["changed"]})
        dialog._confirm_cleanup.assert_not_called()
        dialog._request.assert_not_called()
    finally:
        dialog.close()


def test_apply_requires_confirmation_and_carries_reviewed_digest():
    dialog = _dialog()
    dialog._request = MagicMock()
    plan = {"can_cleanup": True, "plan_digest": "abc", "will_delete": ["tf_backup_1"], "tables": []}
    try:
        dialog._confirm_cleanup = MagicMock(return_value=False)
        dialog._on_plan("backup", "r1", True, "", plan)
        dialog._request.assert_not_called()

        dialog._confirm_cleanup = MagicMock(return_value=True)
        dialog._on_plan("backup", "r1", True, "", plan)
        payload = dialog._request.call_args[0][0]
        assert payload["action"] == "cleanup_apply"
        assert payload["confirmed"] is True and payload["plan_digest"] == "abc"
        assert payload["restore_id"] == "r1" and payload["target"] == "backup"
    finally:
        dialog.close()


def test_import_dialog_backup_management_carries_registered_tls(monkeypatch):
    from types import SimpleNamespace

    from src.core import connection_trust as ct
    from src.ui.dialogs import db_import_dialog
    from src.ui.dialogs.db_import_dialog import RustDumpImportDialog

    ct.clear_registered_tls()
    ct.register_endpoint_tls("127.0.0.1", 13306, ct.TlsPolicy("verify_full", "ca.pem", "db.internal"))
    captured = {}

    class FakeDialog:
        def __init__(self, endpoint, input_dirs, parent=None):
            captured["endpoint"] = endpoint

        def refresh(self):
            pass

        def exec(self):
            pass

    monkeypatch.setattr(db_import_dialog, "BackupLifecycleDialog", FakeDialog)
    dummy = SimpleNamespace(
        import_audit={"original_target": {"engine": "mysql", "host": "127.0.0.1", "port": 13306, "database": "d"},
                      "report_path": "C:/dump/_tunnelforge_import_report.json"},
        restore_config=SimpleNamespace(user="u", password="p"),
    )
    try:
        RustDumpImportDialog.open_backup_lifecycle(dummy)
    finally:
        ct.clear_registered_tls()
    assert captured["endpoint"]["tls"]["mode"] == "verify_full"
    assert captured["endpoint"]["user"] == "u"
