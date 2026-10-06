"""SQL editor workspace recovery (P1-4): save -> recreate -> restore, crash simulation, corrupt files,
production read-only state, disk-changed files, close choices, settings and the recovered-SQL list."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.core import workspace_store as ws
from src.ui.dialogs import sql_editor_workspace
from src.ui.dialogs.recovered_sql_dialog import RecoveredSqlDialog, find_orphans
from src.ui.dialogs.sql_editor_dialog import SQLEditorDialog
from src.ui.dialogs.workspace_settings_group import WorkspaceRecoverySettingsGroup

_app = QApplication.instance() or QApplication([])

PROFILE = "prof-1"


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "ws"
    monkeypatch.setattr(sql_editor_workspace, "make_workspace_store", lambda: ws.WorkspaceStore(directory))
    return ws.WorkspaceStore(directory)


@pytest.fixture(autouse=True)
def _no_modal_boxes(monkeypatch):
    for name in ("question", "warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Yes)


def make_editor(monkeypatch, environment="development", settings=None, databases=("app", "other"), engine="mysql"):
    def fake_refresh(self):
        self.db_combo.addItems(list(databases))

    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", fake_refresh)
    monkeypatch.setattr(SQLEditorDialog, "_load_metadata", lambda self, schema=None: None)
    values = {ws.SETTING_ENABLED: True}
    values.update(settings or {})
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("u", "p")
    config_manager.get_app_setting.side_effect = lambda key, default=None: values.get(key, default)
    dialog = SQLEditorDialog(
        None,
        {"id": PROFILE, "name": "p", "connection_mode": "direct", "environment": environment,
         "remote_host": "127.0.0.1", "remote_port": 3306, "db_engine": engine},
        config_manager, MagicMock(),
    )
    dialog._test_app = _app
    return dialog


def log_text(dialog) -> str:
    return dialog.message_text.toPlainText()


def save_now(dialog):
    dialog._ws_save_now()
    assert dialog._ws_writer.flush(5.0)


def close_dialog(dialog, decision="close"):
    dialog._ws_confirm_close = MagicMock(return_value=decision)
    event = QCloseEvent()
    dialog.closeEvent(event)
    return event.isAccepted()


def abandon(dialog):
    """Drop a dialog the way a crash would: no final save, just stop its timers/threads."""
    dialog._ws_debounce.stop()
    dialog._ws_periodic.stop()
    dialog._ws_writer.stop(2.0)
    dialog._ws_finalized = True


def titles(dialog):
    return [dialog.editor_tabs.tabText(i) for i in range(dialog.editor_tabs.count())]


def test_round_trip_restores_tabs_order_titles_cursor_active_tab_and_target(monkeypatch, store, tmp_path):
    sql_file = tmp_path / "report.sql"
    sql_file.write_text("SELECT 'from disk';", encoding="utf-8")
    first = make_editor(monkeypatch)
    try:
        first.editor_tabs.currentWidget().editor.setPlainText("SELECT 1;\nSELECT 2;")
        first._add_new_tab(str(sql_file))
        second = first._add_new_tab()
        second.editor.setPlainText("-- draft three")
        cursor = second.editor.textCursor()
        cursor.setPosition(5)
        second.editor.setTextCursor(cursor)
        first.db_combo.setCurrentText("other")
        first.editor_tabs.setCurrentIndex(1)
        save_now(first)
        saved = json.loads(store.path_for(PROFILE).read_text(encoding="utf-8"))
        assert saved["session"]["state"] == "open"
        assert saved["target"] == {"database": "other", "schema": ""}
        assert [t["file_path"] is not None for t in saved["tabs"]] == [False, True, False]
        assert saved["tabs"][1]["text"] is None, "an unmodified file tab keeps the file as the only source"
        assert close_dialog(first, "close")
    finally:
        pass
    assert store.load(PROFILE).crashed is False
    again = make_editor(monkeypatch)
    try:
        assert again.editor_tabs.count() == 3
        assert [again.editor_tabs.widget(i).editor.toPlainText() for i in (0, 2)] == ["SELECT 1;\nSELECT 2;", "-- draft three"]
        assert again.editor_tabs.widget(1).file_path == str(sql_file)
        assert again.editor_tabs.widget(1).editor.toPlainText() == "SELECT 'from disk';"
        assert again.editor_tabs.currentIndex() == 1
        assert again.editor_tabs.widget(2).editor.textCursor().position() == 5
        assert titles(again)[0].endswith("*") and titles(again)[2].endswith("*")
        assert again.db_combo.currentText() == "other"
        assert "이전 세션에서 3개 탭을 복구" in log_text(again)
    finally:
        close_dialog(again, "discard")


def test_crash_is_detected_from_the_open_session_marker(monkeypatch, store):
    crashed = make_editor(monkeypatch)
    crashed.editor_tabs.currentWidget().editor.setPlainText("SELECT unsaved;")
    save_now(crashed)
    abandon(crashed)  # no clean close
    again = make_editor(monkeypatch)
    try:
        assert again.editor_tabs.widget(0).editor.toPlainText() == "SELECT unsaved;"
        assert "비정상 종료 후 1개 탭을 복구" in log_text(again)
    finally:
        close_dialog(again, "discard")


def test_corrupt_workspace_is_quarantined_and_the_editor_opens_blank(monkeypatch, store):
    store.directory.mkdir(parents=True)
    store.path_for(PROFILE).write_text("{not json", encoding="utf-8")
    dialog = make_editor(monkeypatch)
    try:
        assert dialog.editor_tabs.count() == 1 and dialog.editor_tabs.widget(0).editor.toPlainText() == ""
        assert "읽을 수 없어 빈 상태로 시작" in log_text(dialog)
        assert list(store.directory.glob(f"{PROFILE}.corrupt-*"))
    finally:
        close_dialog(dialog)


def test_newer_version_workspace_is_not_restored_or_overwritten(monkeypatch, store):
    store.directory.mkdir(parents=True)
    future = json.dumps({"version": 99, "profile_id": PROFILE, "tabs": [{"id": "x", "text": "SELECT 'future'"}]})
    store.path_for(PROFILE).write_text(future, encoding="utf-8")
    dialog = make_editor(monkeypatch)
    try:
        assert dialog.editor_tabs.widget(0).editor.toPlainText() == ""
        assert "더 새 버전" in log_text(dialog)
        dialog.editor_tabs.currentWidget().editor.setPlainText("SELECT now;")
        save_now(dialog)
    finally:
        close_dialog(dialog)
    assert store.path_for(PROFILE).read_text(encoding="utf-8") == future


def test_production_editor_restores_read_only_and_never_stores_the_unlock(monkeypatch, store):
    first = make_editor(monkeypatch, environment="production")
    first._write_unlocked = True
    first._unlock_target = ("app", "")
    first.editor_tabs.currentWidget().editor.setPlainText("DELETE FROM t;")
    save_now(first)
    raw = store.path_for(PROFILE).read_text(encoding="utf-8").lower()
    assert "unlock" not in raw and "read_only" not in raw and "write" not in raw
    first._write_unlocked = False
    close_dialog(first, "close")
    again = make_editor(monkeypatch, environment="production")
    try:
        assert again.editor_tabs.widget(0).editor.toPlainText() == "DELETE FROM t;"
        assert again._write_unlocked is False and again._session_read_only() is True
    finally:
        close_dialog(again, "discard")


def test_restore_never_executes_sql_or_starts_a_worker(monkeypatch, store):
    first = make_editor(monkeypatch)
    first.editor_tabs.currentWidget().editor.setPlainText("DROP TABLE t;")
    save_now(first)
    close_dialog(first, "close")
    monkeypatch.setattr(SQLEditorDialog, "_execute_sql", lambda *a, **k: pytest.fail("restore executed SQL"))
    monkeypatch.setattr(SQLEditorDialog, "_ensure_connection", lambda *a, **k: pytest.fail("restore connected"))
    again = make_editor(monkeypatch)
    try:
        assert again.editor_tabs.widget(0).editor.toPlainText() == "DROP TABLE t;"
        assert again.worker is None and again.pending_queries == []
    finally:
        close_dialog(again, "discard")


@pytest.mark.parametrize("keep_draft", [True, False])
def test_modified_file_tab_whose_file_changed_on_disk_asks_and_never_drops_the_draft(monkeypatch, store, tmp_path, keep_draft):
    sql_file = tmp_path / "work.sql"
    sql_file.write_text("SELECT 'v1';", encoding="utf-8")
    first = make_editor(monkeypatch)
    tab = first._add_new_tab(str(sql_file))
    tab.editor.setPlainText("SELECT 'my draft';")
    save_now(first)
    close_dialog(first, "close")
    sql_file.write_text("SELECT 'edited elsewhere';", encoding="utf-8")
    monkeypatch.setattr(SQLEditorDialog, "_ws_ask_changed_file", lambda self, path: keep_draft)
    again = make_editor(monkeypatch)
    try:
        texts = [again.editor_tabs.widget(i).editor.toPlainText() for i in range(again.editor_tabs.count())]
        if keep_draft:
            assert "SELECT 'my draft';" in texts and "SELECT 'edited elsewhere';" not in texts
            draft_tab = next(again.editor_tabs.widget(i) for i in range(again.editor_tabs.count()) if again.editor_tabs.widget(i).editor.toPlainText() == "SELECT 'my draft';")
            assert draft_tab.file_path == str(sql_file) and draft_tab.is_modified
        else:
            assert "SELECT 'edited elsewhere';" in texts and "SELECT 'my draft';" in texts
            assert "내 초안은 새 탭에 보존했습니다" in log_text(again)
    finally:
        close_dialog(again, "discard")


def test_missing_saved_target_keeps_the_sql_and_explains(monkeypatch, store):
    first = make_editor(monkeypatch)
    first.db_combo.setCurrentText("other")
    first.editor_tabs.currentWidget().editor.setPlainText("SELECT 1;")
    save_now(first)
    close_dialog(first, "close")
    again = make_editor(monkeypatch, databases=("app",))
    try:
        assert again.editor_tabs.widget(0).editor.toPlainText() == "SELECT 1;"
        assert again.db_combo.currentText() == "app" and "'other'를 찾을 수 없어" in log_text(again)
    finally:
        close_dialog(again, "discard")


def test_postgresql_target_is_the_schema(monkeypatch, store):
    first = make_editor(monkeypatch, databases=("public", "audit"), engine="postgresql")
    first.db_combo.setCurrentText("audit")
    first.editor_tabs.currentWidget().editor.setPlainText("SELECT 1;")
    save_now(first)
    assert store.load(PROFILE).state.target_schema == "audit"
    close_dialog(first, "close")
    again = make_editor(monkeypatch, databases=("public", "audit"), engine="postgresql")
    try:
        assert again.db_combo.currentText() == "audit"
    finally:
        close_dialog(again, "discard")


def test_close_choices_keep_discard_or_cancel(monkeypatch, store):
    dialog = make_editor(monkeypatch)
    dialog.editor_tabs.currentWidget().editor.setPlainText("SELECT keep;")
    save_now(dialog)
    assert close_dialog(dialog, "cancel") is False, "cancel must keep the editor open"
    assert dialog._ws_finalized is False and store.path_for(PROFILE).exists()
    assert close_dialog(dialog, "close") is True
    assert store.load(PROFILE).crashed is False and store.path_for(PROFILE).exists()

    again = make_editor(monkeypatch)
    assert again.editor_tabs.widget(0).editor.toPlainText() == "SELECT keep;"
    assert close_dialog(again, "discard") is True
    assert not store.path_for(PROFILE).exists()


def test_close_dialog_text_says_the_sql_will_be_restored(monkeypatch, store):
    dialog = make_editor(monkeypatch)
    captured = {}

    class FakeBox:
        class _Btn:  # minimal QMessageBox stand-in
            pass

        def __init__(self, parent=None):
            self.buttons = []

        def setIcon(self, *_): pass
        def setWindowTitle(self, *_): pass
        def setText(self, text): captured["text"] = text
        def addButton(self, label, role):
            button = SimpleNamespace(label=label)
            self.buttons.append(button)
            return button
        def setDefaultButton(self, *_): pass
        def setEscapeButton(self, *_): pass
        def exec(self): pass
        def clickedButton(self): return self.buttons[1]

    monkeypatch.setattr(sql_editor_workspace, "QMessageBox", type("QM", (FakeBox,), {
        "Icon": QMessageBox.Icon, "ButtonRole": QMessageBox.ButtonRole}))
    decision = dialog._ws_confirm_close(["미커밋 변경사항 2건 (롤백됨)"], ["Query 1"])
    assert decision == "discard"
    assert "다음 실행 때 복원됩니다" in captured["text"] and "미커밋 변경사항 2건" in captured["text"]
    close_dialog(dialog, "discard")


def test_empty_workspace_removes_the_stored_file(monkeypatch, store):
    first = make_editor(monkeypatch)
    first.editor_tabs.currentWidget().editor.setPlainText("SELECT 1;")
    save_now(first)
    assert store.path_for(PROFILE).exists()
    first.editor_tabs.currentWidget().editor.setPlainText("")
    save_now(first)
    assert not store.path_for(PROFILE).exists()
    close_dialog(first, "discard")


def test_autosave_debounces_text_changes_and_the_periodic_tick_saves_dirty_state(monkeypatch, store):
    dialog = make_editor(monkeypatch)
    try:
        assert dialog._ws_debounce.interval() == 2000 and dialog._ws_periodic.interval() == 30_000
        dialog.editor_tabs.currentWidget().editor.setPlainText("SELECT 1;")
        assert dialog._ws_debounce.isActive() and dialog._ws_dirty is True
        assert dialog._ws_debounce.isSingleShot()
        dialog._ws_periodic_tick()
        assert dialog._ws_writer.flush(5.0)
        assert store.load(PROFILE).state.tabs[0].text == "SELECT 1;"
        assert dialog._ws_dirty is False
        dialog._ws_periodic_tick()  # nothing dirty: no write
    finally:
        close_dialog(dialog, "discard")


def test_interval_setting_is_clamped(monkeypatch, store):
    dialog = make_editor(monkeypatch, settings={ws.SETTING_AUTOSAVE_SECONDS: 1})
    try:
        assert dialog._ws_periodic.interval() == ws.MIN_AUTOSAVE_SECONDS * 1000
    finally:
        close_dialog(dialog, "discard")


def test_disabled_setting_writes_and_restores_nothing(monkeypatch, store):
    store.save(ws.WorkspaceState(profile_id=PROFILE, tabs=[ws.TabState(id="t", text="SELECT old;", dirty=True)]))
    before = store.path_for(PROFILE).read_bytes()
    dialog = make_editor(monkeypatch, settings={ws.SETTING_ENABLED: False})
    try:
        assert dialog.editor_tabs.widget(0).editor.toPlainText() == ""
        dialog.editor_tabs.currentWidget().editor.setPlainText("SELECT new;")
        assert dialog._ws_recovery_active() is False
        close_dialog(dialog)
    finally:
        pass
    assert store.path_for(PROFILE).read_bytes() == before


def test_write_failure_warns_once_and_editing_continues(monkeypatch, store):
    def failing(self, state, now=None):
        raise ws.WorkspaceStoreError("cannot write workspace file: OSError")

    dialog = make_editor(monkeypatch)
    try:
        monkeypatch.setattr(ws.WorkspaceStore, "save", failing)
        for text in ("SELECT 1;", "SELECT 2;", "SELECT 3;"):
            dialog.editor_tabs.currentWidget().editor.setPlainText(text)
            save_now(dialog)
        assert log_text(dialog).count("자동 저장에 실패했습니다") == 1
        assert dialog.editor_tabs.currentWidget().editor.toPlainText() == "SELECT 3;"
    finally:
        monkeypatch.undo()
        close_dialog(dialog, "discard")


def test_oversized_draft_is_reported_not_truncated(monkeypatch, store):
    monkeypatch.setattr(ws, "MAX_TAB_TEXT_BYTES", 10)
    dialog = make_editor(monkeypatch)
    try:
        dialog.editor_tabs.currentWidget().editor.setPlainText("SELECT 1, 2, 3, 4, 5;")
        save_now(dialog)
        dialog._ws_report_problems()
        assert "크기 제한으로 1개 탭의 초안" in log_text(dialog)
        assert store.load(PROFILE).state.tabs[0].text_omitted is True
    finally:
        close_dialog(dialog, "discard")


def test_esc_path_through_done_still_saves_the_final_state(monkeypatch, store):
    dialog = make_editor(monkeypatch)
    dialog.editor_tabs.currentWidget().editor.setPlainText("SELECT esc;")
    dialog.done(0)  # Esc / reject do not go through closeEvent
    assert store.load(PROFILE).state.tabs[0].text == "SELECT esc;"
    assert store.load(PROFILE).crashed is False


def test_about_to_quit_does_a_final_save(monkeypatch, store):
    dialog = make_editor(monkeypatch)
    dialog.editor_tabs.currentWidget().editor.setPlainText("SELECT quit;")
    dialog._ws_on_about_to_quit()
    assert store.load(PROFILE).state.tabs[0].text == "SELECT quit;"
    assert store.load(PROFILE).crashed is False


# ------------------------------------------------------------------ recovered SQL list and settings

def _orphan(store, profile_id, text, when):
    store.save(ws.WorkspaceState(profile_id=profile_id, tabs=[ws.TabState(id="t", title_index=1, text=text, dirty=True)]), now=when)


def test_recovered_sql_lists_only_orphans_and_marks_expired(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "ws")
    now = datetime.now(timezone.utc)
    _orphan(store, "alive", "SELECT 1;", now)
    _orphan(store, "gone-new", "SELECT 2;", now)
    _orphan(store, "gone-old", "SELECT 3;", now - timedelta(days=100))
    assert {o.profile_id for o in find_orphans(store, ["alive"])} == {"gone-new", "gone-old"}
    dialog = RecoveredSqlDialog(store, ["alive"])
    try:
        rows = [dialog.workspace_list.item(i).text() for i in range(dialog.workspace_list.count())]
        assert len(rows) == 2 and sum("[만료]" in r for r in rows) == 1
        assert dialog.viewer.isReadOnly()
    finally:
        dialog.close()


def test_recovered_sql_view_copy_save_and_delete(tmp_path, monkeypatch):
    store = ws.WorkspaceStore(tmp_path / "ws")
    _orphan(store, "gone", "SELECT 'secret-free';", datetime.now(timezone.utc))
    dialog = RecoveredSqlDialog(store, [])
    try:
        assert dialog.viewer.toPlainText() == "SELECT 'secret-free';"
        dialog.copy_current()
        assert QApplication.clipboard().text() == "SELECT 'secret-free';"
        target = tmp_path / "out.sql"
        monkeypatch.setattr(dialog, "_choose_save_path", lambda: str(target))
        assert dialog.save_current() is True and target.read_text(encoding="utf-8") == "SELECT 'secret-free';"
        monkeypatch.setattr(dialog, "_confirm_delete", lambda: False)
        assert dialog.delete_current() is False and store.path_for("gone").exists()
        monkeypatch.setattr(dialog, "_confirm_delete", lambda: True)
        assert dialog.delete_current() is True and not store.path_for("gone").exists()
        assert dialog.workspace_list.count() == 0
    finally:
        dialog.close()


def test_recovered_sql_never_deletes_without_confirmation_and_has_no_edit_path(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "ws")
    _orphan(store, "gone", "SELECT 1;", datetime.now(timezone.utc))
    dialog = RecoveredSqlDialog(store, [])
    try:
        assert dialog.viewer.isReadOnly()
        dialog.viewer.setPlainText("changed")  # programmatic only; the stored workspace must stay unchanged
        assert store.load("gone").state.tabs[0].text == "SELECT 1;"
    finally:
        dialog.close()


def test_settings_group_saves_values_and_deletes_only_after_confirmation(tmp_path, monkeypatch):
    stored = {}
    manager = MagicMock()
    manager.get_app_setting.side_effect = lambda key, default=None: stored.get(key, default)
    manager.set_app_settings.side_effect = lambda updates: stored.update(updates)
    group = WorkspaceRecoverySettingsGroup(manager)
    try:
        assert group.chk_enabled.isChecked() and group.spin_seconds.value() == ws.DEFAULT_AUTOSAVE_SECONDS
        group.chk_enabled.setChecked(False)
        group.spin_seconds.setValue(90)
        group.save()
        assert stored == {ws.SETTING_ENABLED: False, ws.SETTING_AUTOSAVE_SECONDS: 90}
        assert group.spin_seconds.isEnabled() is False

        directory = tmp_path / "isolated"
        monkeypatch.setattr(ws, "workspaces_dir", lambda *a, **k: directory)
        ws.WorkspaceStore(directory).save(ws.WorkspaceState(profile_id="p", tabs=[ws.TabState(id="t", text="x")]))
        monkeypatch.setattr(group, "_confirm_delete", lambda: False)
        assert group.delete_stored() == 0 and (directory / "p.json").exists()
        monkeypatch.setattr(group, "_confirm_delete", lambda: True)
        assert group.delete_stored() == 1 and not (directory / "p.json").exists()
    finally:
        group.close()


def test_main_window_button_shows_only_when_orphans_exist(tmp_path, monkeypatch):
    from src.ui.main_window import TunnelManagerUI

    directory = tmp_path / "ws"
    monkeypatch.setattr(ws, "workspaces_dir", lambda *a, **k: directory)
    store = ws.WorkspaceStore(directory)
    button = MagicMock()
    window = SimpleNamespace(act_recovered_sql=button, _known_profile_ids=lambda: ["alive"])
    TunnelManagerUI._refresh_recovered_sql_button(window)
    button.setVisible.assert_called_with(False)
    _orphan(store, "gone", "SELECT 1;", datetime.now(timezone.utc))
    TunnelManagerUI._refresh_recovered_sql_button(window)
    button.setVisible.assert_called_with(True)
