import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from src.core.migration_analyzer import ActionType, CleanupAction
from src.ui.dialogs import migration_dialogs
from src.ui.workers.migration_worker import CleanupWorker
from tests.conftest import FakeMySQLConnector

# 모듈 레벨에서 QApplication을 생성하고 참조를 유지한다. 변수에 바인딩하지 않으면
# (예: 단순히 `QApplication.instance() or QApplication([])`만 호출) 이 표현식 문이 끝나는
# 즉시 파이썬 refcount가 0이 되어 새로 만든 QApplication이 즉시 파괴되고, 이후 이 프로세스에서
# 첫 QWidget(다이얼로그)을 생성하는 테스트가 "Must construct a QApplication before a QWidget"로
# 네이티브 크래시한다. 모듈이 살아있는 동안 참조를 붙잡아 두어야 한다.
_APP = QApplication.instance() or QApplication([])


def _cleanup_action() -> CleanupAction:
    return CleanupAction(
        action_type=ActionType.DELETE,
        table="orders",
        description="delete orphan orders",
        sql="DELETE FROM `app`.`orders` WHERE `user_id` IS NULL",
        affected_rows=3,
    )


def test_cleanup_worker_rejects_legacy_actual_cleanup_mode():
    with pytest.raises(RuntimeError, match="Rust Core"):
        CleanupWorker(
            connector=FakeMySQLConnector(),
            schema="app",
            actions=[_cleanup_action()],
            dry_run=False,
        )


def test_cleanup_worker_allows_dry_run_mode():
    worker = CleanupWorker(
        connector=FakeMySQLConnector(),
        schema="app",
        actions=[_cleanup_action()],
        dry_run=True,
    )

    assert worker.dry_run is True


# ============================================================
# migration_dialogs.py 회귀 테스트 (WP-3.6)
# ============================================================

class _FakeSignal:
    """PyQt 시그널을 흉내내는 결정론적 페이크 (connect/disconnect/emit 지원)"""

    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def disconnect(self, slot=None):
        if slot is None:
            if not self._slots:
                raise TypeError("disconnect() failed: no connections")
            self._slots = []
            return
        if slot not in self._slots:
            raise TypeError("disconnect() failed between signal and slot")
        self._slots.remove(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class _FakeAnalyzerWorker:
    """closeEvent 검증용 페이크 Worker - quit()/wait()/terminate() 호출 시 즉시 실패"""

    def __init__(self):
        self.progress = _FakeSignal()
        self.analysis_complete = _FakeSignal()
        self.finished = _FakeSignal()

    def isRunning(self):
        return True

    def quit(self):
        raise AssertionError("quit()가 호출되어서는 안 됩니다 (Worker를 강제 종료하지 않음)")

    def wait(self, *args, **kwargs):
        raise AssertionError("wait()가 호출되어서는 안 됩니다 (UI 스레드를 블로킹하지 않음)")

    def terminate(self):
        raise AssertionError("terminate()가 호출되어서는 안 됩니다 (강제 종료 폴백 제거됨)")


class _FakeCloseEvent:
    def __init__(self):
        self.accepted = False
        self.ignored = False

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


def test_close_event_detaches_worker_without_terminate_or_wait(monkeypatch):
    """closeEvent는 실행 중인 Worker를 강제 종료(quit/wait/terminate)하지 않고
    백그라운드로 분리한 뒤, Worker가 스스로 끝나면 커넥터를 정리해야 한다."""
    disconnect_calls = []
    monkeypatch.setattr(
        migration_dialogs,
        "_disconnect_connector_in_background",
        lambda connector: disconnect_calls.append(connector),
    )
    monkeypatch.setattr(
        migration_dialogs.QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    fake_worker = _FakeAnalyzerWorker()
    connector = FakeMySQLConnector()

    dialog = type("DummyMigrationDialog", (), {})()
    dialog._is_closing = False
    dialog._disconnect_deferred_to_worker_completion = False
    dialog.worker = fake_worker
    dialog.cleanup_worker = None
    dialog.connector = connector

    event = _FakeCloseEvent()
    migration_dialogs.MigrationAnalyzerDialog.closeEvent(dialog, event)

    assert event.accepted is True
    assert event.ignored is False
    assert dialog._disconnect_deferred_to_worker_completion is True
    assert disconnect_calls == []  # Worker가 아직 끝나지 않았으므로 아직 정리되지 않음

    # Worker가 스스로 끝나면 그제서야 백그라운드 커넥터 정리가 트리거되어야 한다
    fake_worker.finished.emit(True, "분석 완료")

    assert disconnect_calls == [connector]


def test_migration_wizard_skips_final_disconnect_when_dialog_deferred(monkeypatch):
    """다이얼로그가 연결 해제를 Worker 완료로 위임했다면(closeEvent 중 감지),
    MigrationWizard.start()의 finally 블록이 다시 동기적으로 disconnect()하면 안 된다."""
    import src.ui.dialogs.db_dialogs as db_dialogs

    captured = {}

    class _RecordingConnector:
        def __init__(self):
            self.disconnect_called = False

        def disconnect(self):
            self.disconnect_called = True

    class _FakeDBConnectionDialog:
        def __init__(self, parent, tunnel_engine, config_manager):
            self.connector = _RecordingConnector()
            captured["connector"] = self.connector

        def exec(self):
            return QDialog.DialogCode.Accepted

    class _FakeDeferredAnalyzerDialog:
        def __init__(self, parent, connector, config_manager):
            self.connector = connector
            self.disconnect_deferred_to_worker_completion = True

        def exec(self):
            return None

    monkeypatch.setattr(db_dialogs, "DBConnectionDialog", _FakeDBConnectionDialog)
    monkeypatch.setattr(migration_dialogs, "MigrationAnalyzerDialog", _FakeDeferredAnalyzerDialog)

    result = migration_dialogs.MigrationWizard.start(None, tunnel_engine=None, config_manager=None)

    assert result is True
    assert captured["connector"].disconnect_called is False


def test_update_fk_tree_renders_cycle_only_tables_without_db_query(monkeypatch):
    """분석 결과의 fk_tree가 사이클로만 구성되어 있어도(고전적 루트가 없어도) 트리에 렌더되어야 하며,
    이 과정에서 MigrationAnalyzer를 재생성해 DB를 다시 조회해서는 안 된다."""
    QApplication.instance() or QApplication([])

    monkeypatch.setattr(migration_dialogs.MigrationAnalyzerDialog, "load_schemas", lambda self: None)

    def _fail_if_constructed(*args, **kwargs):
        raise AssertionError("update_fk_tree는 MigrationAnalyzer를 생성하면 안 됩니다 (DB 재조회 금지)")

    monkeypatch.setattr(migration_dialogs, "MigrationAnalyzer", _fail_if_constructed)

    dialog = migration_dialogs.MigrationAnalyzerDialog(None, FakeMySQLConnector(), None)
    try:
        dialog.update_fk_tree({"employee": ["employee"]}, "loaded_schema")

        assert dialog.tree_fk.topLevelItemCount() == 1

        top_item = dialog.tree_fk.topLevelItem(0)
        rendered_lines = []

        def _collect(item):
            rendered_lines.append(item.text(0))
            for i in range(item.childCount()):
                _collect(item.child(i))

        _collect(top_item)
        rendered_tree_text = "\n".join(rendered_lines)

        assert "employee" in rendered_tree_text
        assert "순환 참조" in rendered_tree_text

        pane_text = dialog.txt_fk_tree.toPlainText()
        assert "employee" in pane_text
        assert "순환 참조" in pane_text
        assert pane_text != "FK 관계가 없습니다."
    finally:
        dialog.close()


def test_fk_tree_text_formats_roots_and_unvisited_cycles():
    """_format_fk_tree_text는 정상 루트뿐 아니라 루트에서 도달 불가능한(사이클 전용) 테이블도
    별도 진입점으로 렌더해야 한다."""
    text = migration_dialogs._format_fk_tree_text(
        {"root": ["child"], "child": [], "self_ref": ["self_ref"]}
    )

    assert "root" in text
    assert "child" in text
    assert "self_ref" in text
    assert "순환 참조" in text


def test_manual_guide_markdown_to_safe_html_balances_tags_and_fences():
    """ManualGuideDialog._markdown_to_safe_html은 굵게/코드 펜스 태그를 항상 짝지어 생성해야 하고,
    원본 마크다운 백틱이 결과에 남아있으면 안 되며, HTML 특수문자는 이스케이프되어야 한다."""
    content = (
        "**중요 안내**\n"
        "\n"
        "```sql\n"
        "ALTER TABLE t ADD COLUMN c INT;\n"
        "```\n"
        "\n"
        "```\n"
        "plain fence content\n"
        "```\n"
        "\n"
        "<script>alert(1)</script>\n"
    )

    result = migration_dialogs.ManualGuideDialog._markdown_to_safe_html(content)

    assert result.count("<b>") == result.count("</b>")
    assert result.count("<pre") == result.count("</pre>")
    assert "```" not in result
    assert "&lt;script&gt;" in result
    assert "<script>" not in result


def test_execute_cleanup_real_run_disabled_before_action_generation(monkeypatch):
    """dry_run=False는 정리 작업 목록 생성/선택 행 조회 이전에 즉시 비활성화 안내 후 반환해야 하며,
    (이전의 도달 불가능한 2차 확인 블록처럼) 이후 코드가 실행되면 안 된다."""
    warnings = []
    monkeypatch.setattr(
        migration_dialogs.QMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append(args),
    )

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("generate_cleanup_sql은 호출되면 안 됩니다 (dry_run=False는 조기 반환해야 함)")

    monkeypatch.setattr(migration_dialogs.MigrationAnalyzer, "generate_cleanup_sql", _fail_if_called)

    dialog = type("DummyMigrationDialog", (), {})()
    dialog.analysis_result = SimpleNamespace(schema="app", orphan_records=[])
    dialog.cleanup_worker = None
    dialog.table_orphans = None  # 접근되면 AttributeError로 실패 (도달 불가 코드 회귀 방지용 트립와이어)

    migration_dialogs.MigrationAnalyzerDialog.execute_cleanup(dialog, dry_run=False)

    assert dialog.cleanup_worker is None
    assert len(warnings) == 1
    assert "실행 비활성화" in warnings[0]
    assert migration_dialogs.LEGACY_CLEANUP_EXECUTION_DISABLED_TOOLTIP in warnings[0]
