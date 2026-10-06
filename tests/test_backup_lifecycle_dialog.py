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


def test_blocked_rollback_plan_never_reaches_apply(monkeypatch):
    dialog = _dialog()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    dialog._request = MagicMock()
    dialog._confirm_rollback = MagicMock(return_value=True)
    try:
        dialog._on_rollback_plan("r1", True, "", {"can_rollback": False, "blockers": ["views"]})
        dialog._confirm_rollback.assert_not_called()
        dialog._request.assert_not_called()
    finally:
        dialog.close()


def test_rollback_apply_needs_confirmation_and_reviewed_digest():
    dialog = _dialog()
    dialog._request = MagicMock()
    plan = {"can_rollback": True, "plan_digest": "d1", "displaced_backup": "tf_backup_rb_x", "displace": []}
    try:
        dialog._confirm_rollback = MagicMock(return_value=False)
        dialog._on_rollback_plan("r1", True, "", plan)
        dialog._request.assert_not_called()

        dialog._confirm_rollback = MagicMock(return_value=True)
        dialog._on_rollback_plan("r1", True, "", plan)
        payload = dialog._request.call_args[0][0]
        assert payload == {"action": "rollback_apply", "endpoint": {"engine": "mysql"}, "input_dirs": ["C:/dump"],
                           "restore_id": "r1", "plan_digest": "d1", "confirmed": True}
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


def test_list_shows_korean_status_labels_with_raw_code_tooltips_and_empty_state():
    dialog = _dialog()
    try:
        dialog._on_list(True, "", {"backups": [ENTRY]})
        assert dialog.table.item(0, 1).text() == "전환 완료" and dialog.table.item(0, 1).toolTip() == "promoted"
        assert dialog.table.item(0, 3).text() == "TunnelForge 소유 확인"
        assert dialog.table.item(0, 5).text() == "전환됨"
        assert dialog.label_state.text() == "보존된 백업 1건"
        unknown = dict(ENTRY, journal_status="future_code")
        dialog._on_list(True, "", {"backups": [unknown]})
        assert dialog.table.item(0, 1).text() == "future_code", "unknown codes stay visible"
        dialog._on_list(True, "", {"backups": []})
        assert dialog.table.rowCount() == 0 and dialog.label_state.text() == "보존된 백업이 없습니다."
    finally:
        dialog.close()


def test_destructive_actions_are_separated_and_need_a_selection():
    from src.ui.styles import ButtonStyles

    dialog = _dialog()
    try:
        for button in dialog._danger_buttons:
            assert button.styleSheet() == ButtonStyles.DELETE
            assert button.parent() is not dialog.btn_refresh.parent(), "own group, apart from read-only actions"
        assert dialog.btn_refresh.isEnabled()
        assert not dialog.btn_reconcile.isEnabled() and not any(b.isEnabled() for b in dialog._danger_buttons)
        dialog._on_list(True, "", {"backups": [ENTRY]})
        dialog.table.selectRow(0)
        assert dialog.btn_reconcile.isEnabled() and all(b.isEnabled() for b in dialog._danger_buttons)
    finally:
        dialog.close()


def test_request_shows_loading_disables_actions_and_delivers_after_the_thread_ends(monkeypatch):
    from src.ui.dialogs import backup_lifecycle_dialog

    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in self.slots:
                slot(*args)

    workers = []

    class FakeWorker:
        def __init__(self, payload):
            self.payload = payload
            self.finished_with_result = Signal()
            self.finished = Signal()
            workers.append(self)

        def start(self):
            pass

        def wait(self):
            return True

    monkeypatch.setattr(backup_lifecycle_dialog, "BackupLifecycleWorker", FakeWorker)
    dialog = _dialog()
    try:
        dialog.refresh()
        assert dialog.label_state.text() == "조회 중…" and not dialog.btn_refresh.isEnabled()
        dialog.refresh()
        assert len(workers) == 1, "a second click while busy is ignored"
        workers[0].finished_with_result.emit(True, "", {"backups": []})
        assert dialog.label_state.text() == "조회 중…", "result is delivered only when the thread has ended"
        workers[0].finished.emit()
        assert dialog.btn_refresh.isEnabled() and dialog.label_state.text() == "보존된 백업이 없습니다."
        dialog.refresh()
        assert len(workers) == 2, "the next request (e.g. preview -> apply) is accepted right away"
    finally:
        dialog.close()


def test_status_cells_are_translated_and_list_summary_survives_follow_up_requests():
    from src.core import i18n

    dialog = _dialog()
    i18n.set_language("en")
    try:
        dialog._on_list(True, "", {"backups": [ENTRY]})
        assert dialog.table.item(0, 1).text() == "Promoted"
        assert dialog.table.item(0, 5).text() == "Promoted"
        assert dialog.table.item(0, 2).text() == "tf_backup_1"  # names are never translated
        summary = dialog.label_state.text()
        assert summary
        dialog._busy = True
        dialog._on_worker_done(MagicMock(), [True, "", {"conclusion": "promoted"}], lambda *a: None)
        assert dialog.label_state.text() == summary, "reconcile/preview must not wipe the backup count line"
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
        dialog.close()
