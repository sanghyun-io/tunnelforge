"""SQL editor: workspace recovery (P1-4).

Saves unsaved SQL drafts, open tabs (order/title/file path), the selected database/schema and cursor
positions of one profile's editor, and restores them the next time that profile's editor opens.
Never stored: passwords, results, pending changes, or the production write-unlock state (a restored
production editor opens read-only like any new one). Restored text is never executed.
Design: docs/superpowers/specs/2026-10-01-workspace-recovery-design.md
"""
import logging
import os
import uuid
from typing import List, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.core.workspace_store import (
    SESSION_CLOSED, SESSION_OPEN, SETTING_AUTOSAVE_SECONDS, SETTING_ENABLED, STATUS_CORRUPT,
    STATUS_MISSING, STATUS_NEWER_VERSION, STATUS_OK, CursorState, TabState, WorkspaceState,
    WorkspaceStore, autosave_seconds, compare_file_state, validate_profile_id, WorkspaceStoreError,
)
from src.core.workspace_writer import AsyncWorkspaceWriter

logger = logging.getLogger(__name__)

DEBOUNCE_MS = 2000


def make_workspace_store() -> WorkspaceStore:
    """Factory kept at module level so tests can point the editor at a temporary directory."""
    return WorkspaceStore()


class WorkspaceRecoveryMixin:
    """Methods mixed into SQLEditorDialog. Everything is a no-op until `_ws_start()` succeeds."""

    _ws_started = False
    _ws_restoring = False
    _ws_finalized = False
    _ws_writer: Optional[AsyncWorkspaceWriter] = None
    _ws_store: Optional[WorkspaceStore] = None
    _ws_warned = False

    # ------------------------------------------------------------------ lifecycle

    def _ws_start(self) -> None:
        """Called once at the end of the dialog's __init__. Failures degrade to a plain editor."""
        try:
            self._ws_start_impl()
        except Exception:
            logger.warning("workspace recovery could not start", exc_info=True)
            self._ws_started = False
            self._ws_writer = None

    def _ws_start_impl(self) -> None:
        if self.config_mgr.get_app_setting(SETTING_ENABLED, True) is False:
            return
        try:
            profile_id = validate_profile_id(self.config.get("id"))
        except WorkspaceStoreError:
            return
        self._ws_profile_id = profile_id
        self._ws_store = make_workspace_store()
        self._ws_store.cleanup_stale_tmp()
        interval = autosave_seconds(self.config_mgr.get_app_setting(SETTING_AUTOSAVE_SECONDS, None))

        for index in range(self.editor_tabs.count()):
            self._ws_on_tab_added(self.editor_tabs.widget(index))
        self._ws_restore()

        self._ws_writer = AsyncWorkspaceWriter(self._ws_store, profile_id)
        self._ws_dirty = False
        self._ws_debounce = QTimer(self)
        self._ws_debounce.setSingleShot(True)
        self._ws_debounce.setInterval(DEBOUNCE_MS)
        self._ws_debounce.timeout.connect(self._ws_save_now)
        self._ws_periodic = QTimer(self)
        self._ws_periodic.setInterval(interval * 1000)
        self._ws_periodic.timeout.connect(self._ws_periodic_tick)
        self._ws_periodic.start()
        self.editor_tabs.tabBar().tabMoved.connect(lambda *_: self._ws_schedule(immediate=True))
        self.editor_tabs.currentChanged.connect(lambda *_: self._ws_schedule(immediate=True))
        self.db_combo.currentTextChanged.connect(lambda *_: self._ws_schedule(immediate=True))
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._ws_on_about_to_quit)
        self._ws_started = True
        # mark the session as open right away so a crash before the first edit is still detected
        self._ws_schedule(immediate=True)

    def _ws_recovery_active(self) -> bool:
        return self._ws_started and self._ws_writer is not None and not self._ws_finalized

    # ------------------------------------------------------------------ triggers

    def _ws_on_tab_added(self, tab) -> None:
        if tab is None or getattr(tab, "_ws_connected", False):
            return
        tab._ws_connected = True
        tab._ws_id = getattr(tab, "_ws_id", None) or uuid.uuid4().hex[:12]
        tab.editor.textChanged.connect(self._ws_text_changed)
        tab.title_changed.connect(lambda *_: self._ws_schedule())
        self._ws_schedule(immediate=True)

    def _ws_text_changed(self) -> None:
        self._ws_schedule()

    def _ws_schedule(self, immediate: bool = False) -> None:
        if not self._ws_started or self._ws_restoring or self._ws_finalized:
            return
        self._ws_dirty = True
        if immediate:
            self._ws_debounce.stop()
            self._ws_save_now()
        else:
            self._ws_debounce.start()  # restart: saves 2 s after the last change

    def _ws_periodic_tick(self) -> None:
        if self._ws_started and self._ws_dirty and not self._ws_finalized:
            self._ws_save_now()

    def _ws_on_about_to_quit(self) -> None:
        self._ws_finalize(discard=False)

    # ------------------------------------------------------------------ snapshot / save

    def _ws_snapshot(self, session_state: str = SESSION_OPEN) -> WorkspaceState:
        stored: List[TabState] = []
        current_widget = self.editor_tabs.currentWidget()
        active = 0
        for index in range(self.editor_tabs.count()):
            tab = self.editor_tabs.widget(index)
            text = tab.editor.toPlainText()
            file_path = tab.file_path
            if not file_path and not text.strip():
                continue  # empty untitled tabs are not stored
            cursor = tab.editor.textCursor()
            if tab is current_widget:
                active = len(stored)
            stored.append(TabState(
                id=getattr(tab, "_ws_id", None) or uuid.uuid4().hex[:12],
                title_index=int(getattr(tab, "_tab_index", 1) or 1),
                file_path=file_path,
                # an unmodified file tab keeps the file on disk as the only source of truth
                text=None if (file_path and not tab.is_modified) else text,
                dirty=bool(tab.is_modified),
                cursor=CursorState(cursor.position(), cursor.anchor(), tab.editor.verticalScrollBar().value()),
                file_state=getattr(tab, "file_state", None) if file_path else None,
            ))
        database, schema = self._ws_current_target()
        from src.version import __version__ as VERSION
        return WorkspaceState(
            profile_id=self._ws_profile_id, target_database=database, target_schema=schema,
            active_tab=active, tabs=stored, session_state=session_state, app_version=VERSION,
        )

    def _ws_current_target(self):
        selected = self.db_combo.currentText().strip() if self.db_combo.count() else ""
        if not selected:
            return "", ""
        database, schema = self._database_and_schema_for_selection(selected)
        return database or "", schema or ""

    def _ws_save_now(self, session_state: str = SESSION_OPEN) -> None:
        if not self._ws_recovery_active() and session_state == SESSION_OPEN:
            return
        try:
            state = self._ws_snapshot(session_state)
        except Exception:
            logger.warning("workspace snapshot failed", exc_info=True)
            return
        self._ws_dirty = False
        if state.tabs:
            self._ws_writer.submit(state)
        else:
            self._ws_writer.request_delete()
        self._ws_report_problems()

    def _ws_report_problems(self) -> None:
        writer = self._ws_writer
        if writer is None:
            return
        omitted = writer.take_omitted()
        if omitted:
            self._session_log(f"⚠️ 크기 제한으로 {len(omitted)}개 탭의 초안을 복구 파일에 저장하지 못했습니다.")
        if writer.take_error() and not self._ws_warned:
            self._ws_warned = True
            self._session_log("⚠️ 작업 공간 자동 저장에 실패했습니다. 편집은 계속할 수 있으며 다음 저장 때 다시 시도합니다.")

    # ------------------------------------------------------------------ close

    def _ws_confirm_close(self, loss_warnings: List[str], modified_titles: List[str]) -> str:
        """'close' (keep drafts), 'discard' (delete them) or 'cancel'."""
        lines = [f"• {w}" for w in loss_warnings]
        text = ""
        if lines:
            text += "다음 내용은 손실됩니다:\n\n" + "\n".join(lines) + "\n\n"
        text += (
            f"저장되지 않은 SQL 편집 내용 {len(modified_titles)}개 탭은 다음 실행 때 복원됩니다.\n"
            "복원 데이터를 남기지 않으려면 '버리기'를 선택하세요."
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("닫기 확인")
        box.setText(text)
        keep = box.addButton("닫기 (다음에 복원)", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("버리기", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep:
            return "close"
        if clicked is discard:
            return "discard"
        return "cancel"

    def _ws_unsaved_draft_titles(self) -> List[str]:
        """Titles of modified tabs that hold actual text (a fresh blank tab is not worth a prompt)."""
        titles = []
        for index in range(self.editor_tabs.count()):
            tab = self.editor_tabs.widget(index)
            if tab is not None and tab.is_modified and tab.editor.toPlainText().strip():
                titles.append(tab.get_title().rstrip(" *"))
        return titles

    def _ws_finalize(self, discard: bool) -> None:
        """Final write on close / quit (idempotent). `discard` removes the stored workspace."""
        if not self._ws_started or self._ws_finalized or self._ws_writer is None:
            return
        try:
            self._ws_debounce.stop()
            self._ws_periodic.stop()
            if discard:
                self._ws_writer.request_delete()
            else:
                self._ws_save_now(SESSION_CLOSED)
            self._ws_writer.flush(5.0)
            self._ws_report_problems()
        except Exception:
            logger.warning("workspace final save failed", exc_info=True)
        finally:
            self._ws_finalized = True
            try:
                self._ws_writer.stop(2.0)
            except Exception:
                logger.debug("workspace writer stop failed", exc_info=True)
            app = QApplication.instance()
            if app is not None:
                try:
                    app.aboutToQuit.disconnect(self._ws_on_about_to_quit)
                except (TypeError, RuntimeError):
                    pass

    def done(self, result) -> None:  # Esc / accept / reject paths do not call closeEvent
        self._ws_finalize(discard=False)
        super().done(result)

    # ------------------------------------------------------------------ restore

    def _ws_restore(self) -> None:
        loaded = self._ws_store.load(self._ws_profile_id)
        if loaded.status == STATUS_MISSING:
            return
        if loaded.status == STATUS_CORRUPT:
            name = loaded.backup_path.name if loaded.backup_path else ""
            self._session_log(f"⚠️ 이전 작업 공간 파일을 읽을 수 없어 빈 상태로 시작합니다. 손상된 파일은 보존했습니다: {name}")
            return
        if loaded.status == STATUS_NEWER_VERSION:
            self._session_log("⚠️ 더 새 버전의 TunnelForge가 저장한 작업 공간이라 복원하지 않았습니다 (파일은 그대로 유지됩니다).")
            return
        if loaded.status != STATUS_OK or loaded.state is None or not loaded.state.tabs:
            return
        self._ws_restoring = True
        try:
            restored = self._ws_build_tabs(loaded.state)
            self._ws_apply_target(loaded.state)
        except Exception:
            logger.warning("workspace restore failed", exc_info=True)
            restored = 0
        finally:
            self._ws_restoring = False
        if restored:
            when = "비정상 종료 후 " if loaded.crashed else "이전 세션에서 "
            self._session_log(f"♻️ {when}{restored}개 탭을 복구했습니다. 복구된 SQL은 자동으로 실행되지 않습니다.")

    def _ws_build_tabs(self, state: WorkspaceState) -> int:
        blank_index = None
        if self.editor_tabs.count() == 1:
            first = self.editor_tabs.widget(0)
            # a fresh tab already reports is_modified, so only the content decides whether it is blank
            if not first.file_path and not first.editor.toPlainText().strip():
                blank_index = 0
        created = []
        max_index = 0
        for saved in state.tabs:
            max_index = max(max_index, saved.title_index)
            for tab, saved_tab in self._ws_make_tabs(saved):
                created.append((tab, saved_tab))
        if not created:
            return 0
        if blank_index is not None:
            self.editor_tabs.removeTab(blank_index)
        self._tab_counter = max(self._tab_counter, max_index)
        active = min(state.active_tab, len(created) - 1)
        self.editor_tabs.setCurrentIndex(max(0, active))
        for tab, saved_tab in created:
            self._ws_restore_cursor(tab, saved_tab)
        return len(created)

    def _ws_make_tabs(self, saved: TabState):
        """Yield (tab, saved) pairs for one saved tab (a changed file can yield two)."""
        path = saved.file_path
        if path and saved.text is None:
            if saved.text_omitted:
                self._session_log(f"⚠️ 크기 제한 때문에 초안이 저장되지 않아 디스크 파일로 엽니다: {path}")
            if not os.path.isfile(path):
                self._session_log(f"⚠️ 파일을 찾을 수 없어 탭을 복구하지 못했습니다: {path}")
                return
            tab = self._add_new_tab(path)
            yield tab, saved
            return
        if saved.text is None:
            self._session_log("⚠️ 크기 제한 때문에 저장되지 않은 SQL 초안 탭을 복구하지 못했습니다.")
            return
        keep_draft = True
        if path and saved.file_state is not None:
            status = compare_file_state(path, saved.file_state)
            if status == "changed":
                keep_draft = self._ws_ask_changed_file(path)
            elif status == "missing":
                self._session_log(f"⚠️ 파일을 찾을 수 없습니다. 초안만 복구합니다: {path}")
        if path and not keep_draft:
            disk_tab = self._add_new_tab(path)
            yield disk_tab, TabState(id=saved.id, title_index=saved.title_index, file_path=path)
            draft = self._ws_new_draft_tab(saved, file_path=None, dirty=True)
            self._session_log("내 초안은 새 탭에 보존했습니다 (디스크 파일은 다시 읽었습니다).")
            yield draft, saved
            return
        yield self._ws_new_draft_tab(saved, file_path=path, dirty=saved.dirty or bool(path)), saved

    def _ws_new_draft_tab(self, saved: TabState, file_path: Optional[str], dirty: bool):
        tab = self._add_new_tab()
        tab.set_tab_index(saved.title_index)
        tab.file_path = file_path
        tab.file_state = saved.file_state if file_path else None
        text = saved.text or ""
        tab._set_large_document_mode_for_text(text)
        tab.editor.blockSignals(True)
        tab.editor.setPlainText(text)
        tab.editor.blockSignals(False)
        tab.is_modified = bool(dirty)
        tab.title_changed.emit(tab.get_title())
        return tab

    def _ws_ask_changed_file(self, path: str) -> bool:
        """True = keep my draft, False = reload from disk (the draft is kept in a new tab either way)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("파일이 변경되었습니다")
        box.setText(
            f"'{os.path.basename(path)}' 파일이 마지막 저장 이후 디스크에서 바뀌었습니다.\n"
            "저장하지 않은 내 초안을 유지할까요, 디스크의 파일을 다시 읽을까요?\n"
            "(다시 읽어도 내 초안은 새 탭에 보존됩니다.)"
        )
        keep = box.addButton("내 초안 유지", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("디스크 파일 다시 읽기", QMessageBox.ButtonRole.ActionRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is keep

    def _ws_restore_cursor(self, tab, saved: TabState) -> None:
        editor = tab.editor
        length = len(editor.toPlainText())
        position = min(saved.cursor.position, length)
        anchor = min(saved.cursor.anchor, length)
        cursor = editor.textCursor()
        cursor.setPosition(anchor)
        cursor.setPosition(position, cursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        line = saved.cursor.first_visible_line
        editor.verticalScrollBar().setValue(line)
        QTimer.singleShot(0, lambda e=editor, v=line: e.verticalScrollBar().setValue(v))

    def _ws_apply_target(self, state: WorkspaceState) -> None:
        """Select the saved database/schema if the combo still offers it; the SQL is restored either way."""
        wanted = state.target_schema if self._db_engine() == "postgresql" else state.target_database
        if not wanted:
            return
        index = self.db_combo.findText(wanted)
        if index < 0:
            self._session_log(f"⚠️ 이전에 선택한 대상 '{wanted}'를 찾을 수 없어 기본 대상으로 열었습니다.")
            return
        if index != self.db_combo.currentIndex():
            self.db_combo.setCurrentIndex(index)
