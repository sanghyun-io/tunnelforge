import inspect
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.core.sql_validator import SchemaMetadata
from src.ui.dialogs.sql_editor_dialog import (
    LARGE_SQL_RENDER_LIMIT_BYTES,
    SQLEditorDialog,
    SQLEditorTab,
    format_metadata_db_version,
)


app = QApplication.instance() or QApplication(sys.argv)


# =====================================================================
# 테스트 픽스처 (Fake DB 계층) — 실제 DB 없이 WP-2.1 회귀를 검증하기 위한 더블
# =====================================================================
class FakeCursor:
    """RustDbCursor를 흉내내는 최소 커서. description=None 은 비행 statement만 의미한다."""

    def __init__(self, description=None, rows=None, rowcount=0, error=None):
        self.description = description
        self._rows = list(rows or [])
        self.rowcount = rowcount
        self._error = error
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._error:
            raise self._error
        return self.rowcount

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    """RustDbConnection을 흉내내는 최소 연결."""

    def __init__(self, cursor_factory=None):
        self.open = True
        self.autocommit_value = None
        self.committed = False
        self.rolled_back = False
        self._cursor_factory = cursor_factory or (lambda: FakeCursor())
        self.cursors_created = []

    def cursor(self):
        c = self._cursor_factory()
        self.cursors_created.append(c)
        return c

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def autocommit(self, value):
        self.autocommit_value = value

    def close(self):
        self.open = False


class FakeConnector:
    """RustDbConnector를 흉내내는 최소 커넥터."""

    def __init__(self, connection=None):
        self.connection = connection or FakeConnection()
        self.connected = False
        self.disconnected = False

    def connect(self):
        self.connected = True
        self.connection.open = True
        return True, "ok"

    def disconnect(self):
        self.disconnected = True
        self.connection.open = False


class FakeHistory:
    """SQLHistory를 흉내내는 인메모리 기록기."""

    def __init__(self):
        self.entries = []
        self.status_batches = []
        self._counter = 0

    def add_query(self, query, success, result_count=0, execution_time=0.0,
                  status='completed', error=None):
        self._counter += 1
        history_id = f"h{self._counter}"
        self.entries.append({
            'id': history_id,
            'query': query,
            'success': success,
            'result_count': result_count,
            'execution_time': execution_time,
            'status': status,
            'error': error,
        })
        return history_id

    def update_status_batch(self, history_ids, new_status):
        self.status_batches.append((list(history_ids), new_status))
        for entry in self.entries:
            if entry['id'] in history_ids:
                entry['status'] = new_status


class FakeCancelableWorker(QObject):
    """finished pyqtSignal을 실제로 가진 취소 가능한 워커 더블 (QThread 대신 QObject로 경량화)."""

    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.cancelled = False
        self._running = True

    def isRunning(self):
        return self._running

    def cancel(self):
        self.cancelled = True


def make_dialog(monkeypatch):
    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)

    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("testuser", "testpass")

    tunnel_engine = MagicMock()
    tunnel_engine.is_running.return_value = True
    tunnel_engine.get_connection_info.return_value = ("127.0.0.1", 3307)

    return SQLEditorDialog(
        None,
        {
            "id": "test-tunnel",
            "name": "테스트 터널",
            "connection_mode": "direct",
            "remote_host": "127.0.0.1",
            "remote_port": 3306,
        },
        config_manager,
        tunnel_engine,
    )


def close_dialog(dialog):
    for i in range(dialog.editor_tabs.count()):
        tab = dialog.editor_tabs.widget(i)
        if tab:
            tab.is_modified = False
    dialog.close()


def test_message_panel_is_separate_from_result_tabs(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        assert dialog.result_tabs.count() == 0
        assert dialog.result_tabs.indexOf(dialog.message_text) == -1
        assert dialog._message_collapsed is True
        assert dialog.message_text.isHidden()
        assert not dialog.message_summary.isHidden()
        assert dialog.btn_toggle_message.text() == "실행 로그 펼치기"
    finally:
        close_dialog(dialog)


def test_split_queries_preserves_comments_dollar_quotes_and_delimiters():
    sql = """-- comment; ignored
SELECT 'a;b';
CREATE FUNCTION f() RETURNS void AS $body$
BEGIN
    RAISE NOTICE 'x;y';
END
$body$ LANGUAGE plpgsql;
DELIMITER //
CREATE PROCEDURE p()
BEGIN
    SELECT 'c;d';
END//
DELIMITER ;
SELECT 1;"""

    assert SQLEditorDialog._split_queries(None, sql) == [
        "-- comment; ignored\nSELECT 'a;b'",
        "CREATE FUNCTION f() RETURNS void AS $body$\n"
        "BEGIN\n"
        "    RAISE NOTICE 'x;y';\n"
        "END\n"
        "$body$ LANGUAGE plpgsql",
        "CREATE PROCEDURE p()\nBEGIN\n    SELECT 'c;d';\nEND",
        "SELECT 1",
    ]


def test_get_query_at_cursor_uses_statement_parser_ranges(monkeypatch):
    dialog = make_dialog(monkeypatch)
    sql = """SELECT 1;
CREATE FUNCTION f() RETURNS void AS $body$
BEGIN
    RAISE NOTICE 'x;y';
END
$body$ LANGUAGE plpgsql;
SELECT 2;"""
    try:
        dialog.editor.setPlainText(sql)
        cursor = dialog.editor.textCursor()
        cursor.setPosition(sql.index("RAISE NOTICE"))
        dialog.editor.setTextCursor(cursor)

        assert dialog._get_query_at_cursor() == (
            "CREATE FUNCTION f() RETURNS void AS $body$\n"
            "BEGIN\n"
            "    RAISE NOTICE 'x;y';\n"
            "END\n"
            "$body$ LANGUAGE plpgsql"
        )
    finally:
        close_dialog(dialog)


def test_message_panel_toggle_changes_height(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog._toggle_message_panel()
        assert dialog._message_collapsed is False
        assert not dialog.message_text.isHidden()
        assert dialog.message_text.maximumHeight() == 220
        assert dialog.btn_toggle_message.text() == "실행 로그 접기"

        dialog._toggle_message_panel()
        assert dialog._message_collapsed is True
        assert dialog.message_text.isHidden()
    finally:
        close_dialog(dialog)


def test_query_result_collapses_log_and_updates_summary(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog.history_manager = FakeHistory()
        dialog._toggle_message_panel()

        dialog._on_query_result(0, True, ["id"], [[1], [2]], "", 2, 0.125)

        assert dialog._message_collapsed is True
        assert dialog.message_text.isHidden()
        assert "2행 반환" in dialog.message_summary.text()
        assert "0.125초" in dialog.message_summary.text()
    finally:
        close_dialog(dialog)


def test_postgresql_header_labels_selector_as_schema(monkeypatch):
    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("postgres", "tunnelpass")
    tunnel_engine = MagicMock()

    dialog = SQLEditorDialog(
        None,
        {
            "id": "pg-test",
            "name": "PostgreSQL 테스트",
            "connection_mode": "direct",
            "remote_host": "127.0.0.1",
            "remote_port": 35432,
            "db_engine": "postgresql",
            "default_database": "tf_target",
        },
        config_manager,
        tunnel_engine,
    )
    try:
        assert dialog.db_selector_label.text() == "📂 Schema:"
    finally:
        close_dialog(dialog)


def test_result_tabs_can_be_deleted_and_cleared(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog._add_result_table(["id"], [[1]], 0.01)
        dialog._add_result_table(["id"], [[2]], 0.01)

        assert dialog.result_tabs.count() == 2
        assert dialog.result_tabs.tabText(0).startswith("결과 1")
        assert dialog.result_tabs.tabText(1).startswith("결과 2")

        dialog.close_result_tab(0)
        assert dialog.result_tabs.count() == 1
        assert dialog.result_tabs.tabText(0).startswith("결과 2")

        assert dialog._clear_result_tabs() is True
        assert dialog.result_tabs.count() == 0

        dialog._add_result_table(["id"], [[3]], 0.01)
        assert dialog.result_tabs.tabText(0).startswith("결과 1")
    finally:
        close_dialog(dialog)


def test_refresh_databases_uses_configured_engine(monkeypatch):
    refresh_databases = SQLEditorDialog.refresh_databases

    class FakeConnector:
        def __init__(self):
            self.disconnected = False

        def connect(self):
            return True, "ok"

        def get_schemas(self):
            return ["public"]

        def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("postgres", "tunnelpass")
    tunnel_engine = MagicMock()
    connector = FakeConnector()
    created = {}

    dialog = SQLEditorDialog(
        None,
        {
            "id": "pg-test",
            "name": "PostgreSQL 테스트",
            "connection_mode": "direct",
            "remote_host": "127.0.0.1",
            "remote_port": 35432,
            "db_engine": "postgresql",
            "default_database": "tf_target",
        },
        config_manager,
        tunnel_engine,
    )
    try:
        dialog.refresh_databases = refresh_databases.__get__(dialog, SQLEditorDialog)
        dialog._resolve_db_target = MagicMock(return_value=("127.0.0.1", 35432, None, None))
        def create_connector(*args):
            created["args"] = args
            return connector

        dialog._create_db_connector = MagicMock(side_effect=create_connector)
        dialog._load_metadata = MagicMock()

        dialog.refresh_databases()

        assert created["args"] == (
            "127.0.0.1",
            35432,
            "postgres",
            "tunnelpass",
            "tf_target",
            "public",
        )
        assert dialog.db_combo.findText("public") >= 0
        assert connector.disconnected is True
    finally:
        close_dialog(dialog)


def test_postgresql_query_connection_uses_database_and_selected_schema(monkeypatch):
    class FakeCursor:
        def execute(self, sql):
            self.sql = sql

    class FakeConnection:
        open = False

        def __init__(self):
            self.autocommit_value = None
            self.cursor_obj = FakeCursor()

        def autocommit(self, value):
            self.autocommit_value = value

        def cursor(self):
            return self.cursor_obj

    class FakeConnector:
        def __init__(self):
            self.connection = FakeConnection()

        def connect(self):
            self.connection.open = True
            return True, "ok"

    dialog = make_dialog(monkeypatch)
    connector = FakeConnector()
    created = {}
    try:
        dialog.config.update(
            {
                "db_engine": "postgresql",
                "remote_port": 35432,
                "default_database": "tf_target",
                "default_schema": None,
            }
        )
        dialog.db_combo.clear()
        dialog.db_combo.addItem("public")
        dialog.db_combo.setCurrentText("public")
        dialog._resolve_db_target = MagicMock(return_value=("127.0.0.1", 35432, None, None))
        dialog._create_db_connector = MagicMock(
            side_effect=lambda *args: created.setdefault("args", args) and connector
        )

        success, error = dialog._ensure_connection()

        assert success is True
        assert error is None
        assert created["args"] == (
            "127.0.0.1",
            35432,
            "testuser",
            "testpass",
            "tf_target",
            "public",
        )
    finally:
        close_dialog(dialog)


def test_refresh_databases_passes_mysql_default_database(monkeypatch):
    refresh_databases = SQLEditorDialog.refresh_databases

    class FakeConnector:
        def connect(self):
            return True, "ok"

        def get_schemas(self):
            return ["tf_source84"]

        def disconnect(self):
            pass

    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("root", "tunnelpass")
    tunnel_engine = MagicMock()
    created = {}

    dialog = SQLEditorDialog(
        None,
        {
            "id": "mysql84-test",
            "name": "MySQL 8.4 테스트",
            "connection_mode": "direct",
            "remote_host": "127.0.0.1",
            "remote_port": 33406,
            "db_engine": "mysql",
            "default_database": "tf_source84",
        },
        config_manager,
        tunnel_engine,
    )
    try:
        dialog.refresh_databases = refresh_databases.__get__(dialog, SQLEditorDialog)
        dialog._resolve_db_target = MagicMock(return_value=("127.0.0.1", 33406, None, None))
        dialog._create_db_connector = MagicMock(
            side_effect=lambda *args: created.setdefault("args", args) and FakeConnector()
        )
        dialog._load_metadata = MagicMock()

        dialog.refresh_databases()

        assert created["args"] == (
            "127.0.0.1",
            33406,
            "root",
            "tunnelpass",
            "tf_source84",
            "",
        )
    finally:
        close_dialog(dialog)


def test_format_metadata_db_version_tolerates_empty_rust_version():
    assert format_metadata_db_version("") == "unknown"
    assert format_metadata_db_version("8.4.7") == "8.4"
    assert format_metadata_db_version((16, 2, 0)) == "16.2"


def test_metadata_loaded_does_not_crash_on_empty_version(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        metadata = SchemaMetadata()
        metadata.tables = {"users"}
        metadata.db_version = ""
        dialog._on_validation_requested = MagicMock()

        dialog._on_metadata_loaded(metadata)

        assert "1개 테이블 로드됨" in dialog.validation_label.text()
        assert "unknown" in dialog.validation_label.text()
    finally:
        close_dialog(dialog)


def test_metadata_loaded_populates_schema_tree(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog.db_combo.addItem("app")
        dialog.db_combo.addItem("archive")
        dialog.db_combo.setCurrentText("app")
        metadata = SchemaMetadata()
        metadata.tables = {"orders", "users"}
        metadata.columns = {
            "users": {"id", "email"},
            "orders": {"id", "user_id"},
        }
        metadata.db_version = (8, 4, 0)

        dialog._on_metadata_loaded(metadata)

        assert [
            dialog.schema_tree.topLevelItem(i).text(0)
            for i in range(dialog.schema_tree.topLevelItemCount())
        ] == ["app", "archive"]
        root = dialog.schema_tree.topLevelItem(0)
        assert root.text(0) == "app"
        assert [root.child(i).text(0) for i in range(root.childCount())] == [
            "orders",
            "users",
        ]
        users = root.child(1)
        assert [users.child(i).text(0) for i in range(users.childCount())] == [
            "email",
            "id",
        ]
    finally:
        close_dialog(dialog)


def test_schema_tree_table_click_inserts_quoted_table_name(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        metadata = SchemaMetadata()
        metadata.tables = {"users"}
        metadata.columns = {"users": {"id"}}
        dialog._on_metadata_loaded(metadata)
        dialog.editor.setPlainText("SELECT * FROM ")
        dialog.editor.moveCursor(dialog.editor.textCursor().MoveOperation.End)

        root = dialog.schema_tree.topLevelItem(0)
        table_item = root.child(0)
        dialog._on_schema_tree_item_clicked(table_item, 0)

        assert dialog.editor.toPlainText() == "SELECT * FROM `users` "
    finally:
        close_dialog(dialog)


def test_large_sql_file_disables_expensive_editor_features(tmp_path):
    tab = SQLEditorTab(tab_index=1)
    large_sql = "SELECT * FROM users;\n" * ((LARGE_SQL_RENDER_LIMIT_BYTES // 20) + 2000)
    file_path = tmp_path / "fixedStyle.sql"
    file_path.write_text(large_sql, encoding="utf-8")
    try:
        assert tab.load_file(str(file_path)) is True

        assert tab.editor.is_large_document_mode() is True
        assert tab.editor.highlighter.document() is None
        assert not tab.editor._validation_timer.isActive()
        assert "대용량 SQL" in tab.validation_label.text()
    finally:
        tab.close()


def test_small_content_reenables_editor_features_after_large_file(tmp_path):
    tab = SQLEditorTab(tab_index=1)
    large_sql = "SELECT * FROM users;\n" * ((LARGE_SQL_RENDER_LIMIT_BYTES // 20) + 2000)
    file_path = tmp_path / "fixedStyle.sql"
    file_path.write_text(large_sql, encoding="utf-8")
    try:
        assert tab.load_file(str(file_path)) is True

        tab.set_content("SELECT 1;")

        assert tab.editor.is_large_document_mode() is False
        assert tab.editor.highlighter.document() == tab.editor.document()
        assert tab.validation_label.text() == ""
    finally:
        tab.close()


# =====================================================================
# WP-2.1: 쿼리 분류 통합 + 트랜잭션/스레딩 정합성 회귀 테스트
# =====================================================================
def test_autocommit_worker_uses_classifier_and_streaming_columns_for_empty_select(monkeypatch):
    from src.ui.dialogs import sql_editor_dialog as module
    from src.ui.dialogs import sql_editor_workers as workers_module

    connection = FakeConnection()
    connection.connection_id = "conn-1"
    connection.facade = MagicMock()
    connection.facade.execute_on_connection_streaming.return_value = {
        "columns": ["id", "name"], "rows": [], "rows_affected": 0,
    }

    connector = MagicMock()
    connector.connect.return_value = (True, "ok")
    connector.connection = connection

    monkeypatch.setattr(workers_module, "create_sql_editor_connector", lambda *a, **k: connector)

    worker = module.SQLQueryWorker(
        "127.0.0.1", 3306, "user", "pass", "db",
        ["SELECT * FROM users WHERE id=-1"],
    )

    results = []
    worker.query_result.connect(lambda *args: results.append(args))
    worker.run()

    assert len(results) == 1
    idx, returns_rows, columns, rows, error, affected, exec_time = results[0]
    assert idx == 0
    assert returns_rows is True
    assert columns == ["id", "name"]
    assert rows == []
    assert error == ""


def test_on_query_result_empty_select_creates_result_tab_and_history(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        fake_history = FakeHistory()
        dialog.history_manager = fake_history
        dialog.worker = MagicMock()
        dialog.worker.isRunning.return_value = False
        dialog.worker.queries = ["SELECT * FROM t WHERE 1=0"]

        dialog._on_query_result(0, True, ["id"], [], "", 0, 0.1)

        assert dialog.result_tabs.count() == 1
        assert len(fake_history.entries) == 1
        assert fake_history.entries[0]['success'] is True
        assert fake_history.entries[0]['result_count'] == 0

        text = dialog.message_text.toPlainText()
        assert "0행 반환" in text
        assert "영향받음" not in text
    finally:
        close_dialog(dialog)


def test_transaction_description_empty_list_is_row_returning(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog.history_manager = FakeHistory()

        dialog._on_transaction_query_result(0, "SELECT * FROM t WHERE 1=0", True, [], [], "", 0, 0.05)

        assert dialog.pending_queries == []
        assert dialog.btn_commit.isEnabled() is False
        assert dialog.btn_rollback.isEnabled() is False
    finally:
        close_dialog(dialog)


def test_apply_limit_handles_with_comments_and_formatted_limit(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        assert dialog._apply_limit(
            "WITH big AS (SELECT 1) SELECT * FROM big", 1000
        ) == "WITH big AS (SELECT 1) SELECT * FROM big\nLIMIT 1000"

        assert dialog._apply_limit(
            "-- c\nSELECT * FROM t", 1000
        ) == "-- c\nSELECT * FROM t\nLIMIT 1000"

        assert dialog._apply_limit(
            "SELECT *\nFROM t\nLIMIT 10", 1000
        ) == "SELECT *\nFROM t\nLIMIT 10"

        assert dialog._apply_limit(
            "SELECT 1 -- note", 1000
        ) == "SELECT 1 -- note\nLIMIT 1000"
    finally:
        close_dialog(dialog)


def test_schema_change_with_pending_changes_restores_previous_selection_on_cancel(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog.db_combo.clear()
        dialog.db_combo.addItem("db_a")
        dialog.db_combo.addItem("db_b")
        dialog.db_combo.setCurrentText("db_a")

        dialog._connected_target = ("db_a", "")
        fake_connection = FakeConnection()
        dialog.db_connection = fake_connection
        dialog.pending_queries = [{
            'query': 'UPDATE t SET x=1', 'type': 'UPDATE', 'affected': 1,
            'timestamp': '00:00:00', 'history_id': 'h1',
        }]
        dialog._load_metadata = MagicMock()

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)

        dialog.db_combo.setCurrentText("db_b")

        assert dialog.db_combo.currentText() == "db_a"
        assert dialog.db_connection is fake_connection
        assert fake_connection.open is True
        dialog._load_metadata.assert_not_called()
    finally:
        close_dialog(dialog)


def test_schema_change_confirm_rolls_back_and_reconnects_metadata(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog.db_combo.clear()
        dialog.db_combo.addItem("db_a")
        dialog.db_combo.addItem("db_b")
        dialog.db_combo.setCurrentText("db_a")

        dialog._connected_target = ("db_a", "")
        fake_connection = FakeConnection()
        fake_connector = FakeConnector(fake_connection)
        dialog.db_connection = fake_connection
        dialog._db_connector = fake_connector
        dialog.pending_queries = [{
            'query': 'UPDATE t SET x=1', 'type': 'UPDATE', 'affected': 1,
            'timestamp': '00:00:00', 'history_id': 'h1',
        }]
        dialog.history_manager = FakeHistory()
        dialog._load_metadata = MagicMock()

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

        dialog.db_combo.setCurrentText("db_b")

        assert fake_connection.rolled_back is True
        assert dialog.pending_queries == []
        assert dialog._connected_target is None
        dialog._load_metadata.assert_called_once_with("db_b")
    finally:
        close_dialog(dialog)


def test_mysql_ddl_marks_existing_pending_auto_committed(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog.config['db_engine'] = 'mysql'
        fake_history = FakeHistory()
        dialog.history_manager = fake_history
        dialog.pending_queries = [
            {'query': 'UPDATE t SET x=1', 'type': 'UPDATE', 'affected': 1,
             'timestamp': '00:00:00', 'history_id': 'h1'},
            {'query': 'UPDATE t SET y=2', 'type': 'UPDATE', 'affected': 1,
             'timestamp': '00:00:01', 'history_id': 'h2'},
        ]
        dialog.worker = MagicMock()
        dialog.worker.isRunning.return_value = False
        dialog.worker.queries = ["CREATE INDEX idx ON t(id)"]

        dialog._on_transaction_query_result(0, "CREATE INDEX idx ON t(id)", False, [], [], "", 1, 0.02)

        assert fake_history.status_batches == [(['h1', 'h2'], 'auto_committed_by_ddl')]
        assert dialog.pending_queries == []
        assert fake_history.entries[-1]['status'] == 'committed'
        assert dialog.btn_rollback.isEnabled() is False
        assert "DDL로 인해 이전 미커밋 변경이 자동 커밋" in dialog.message_text.toPlainText()
    finally:
        close_dialog(dialog)


def test_postgresql_error_rolls_back_and_blocks_commit(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog.config['db_engine'] = 'postgresql'
        fake_history = FakeHistory()
        dialog.history_manager = fake_history
        dialog.pending_queries = [{
            'query': 'UPDATE t SET x=1', 'type': 'UPDATE', 'affected': 1,
            'timestamp': '00:00:00', 'history_id': 'h1',
        }]
        fake_connection = FakeConnection()
        dialog.db_connection = fake_connection

        dialog._on_postgres_transaction_rolled_back("syntax error")

        assert fake_history.status_batches == [(['h1'], 'rolled_back_due_to_error')]
        assert dialog.pending_queries == []
        assert dialog._pg_rolled_back_due_to_error is True

        # _do_commit()이 가드에 걸려 QMessageBox.warning을 띄우므로, 오프스크린 환경에서
        # 모달이 블로킹되지 않도록 미리 무해화
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)

        dialog._do_commit()
        assert fake_connection.committed is False
    finally:
        close_dialog(dialog)


def test_execute_sql_transaction_uses_qthread_worker_not_process_events(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        fake_connection = FakeConnection()
        dialog.db_connection = fake_connection
        dialog._ensure_connection = MagicMock(return_value=(True, None))
        dialog.auto_commit_check.setChecked(False)

        started = {}

        class FakeWorker:
            def __init__(self, connection, queries, engine):
                started['connection'] = connection
                started['queries'] = queries
                started['engine'] = engine
                self.progress = MagicMock()
                self.query_result = MagicMock()
                self.postgres_rolled_back = MagicMock()
                self.finished = MagicMock()

            def start(self):
                started['started'] = True

            def isRunning(self):
                # closeEvent()가 dialog.worker.isRunning()을 확인하므로 정리 시 확인창을 띄우지 않도록 False 고정
                return False

        monkeypatch.setattr(
            "src.ui.dialogs.sql_editor_dialog.SQLTransactionExecutionWorker",
            FakeWorker,
        )

        dialog._execute_sql("UPDATE t SET x=1")

        assert started.get('started') is True
        assert started.get('connection') is fake_connection
        assert fake_connection.cursors_created == []
        assert "def _execute_query_in_thread" not in inspect.getsource(SQLEditorDialog)
    finally:
        close_dialog(dialog)


def test_autocommit_history_is_per_query_failure_not_prelogged_batch_success(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        fake_history = FakeHistory()
        dialog.history_manager = fake_history
        dialog._resolve_db_target = MagicMock(return_value=("127.0.0.1", 3306, None, None))
        dialog.db_combo.clear()
        dialog.db_combo.addItem("testdb")
        dialog.db_combo.setCurrentText("testdb")

        class FakeWorker:
            def __init__(self, *a, **k):
                self.progress = MagicMock()
                self.query_result = MagicMock()
                self.finished = MagicMock()

            def start(self):
                pass

            def isRunning(self):
                return False

        monkeypatch.setattr(
            "src.ui.dialogs.sql_editor_dialog.SQLQueryWorker",
            FakeWorker,
        )

        dialog._execute_with_autocommit(["BAD SQL"], "BAD SQL")

        assert fake_history.entries == []

        dialog._on_query_result(0, False, [], [], "syntax error", 0, 0.01)

        assert len(fake_history.entries) == 1
        assert fake_history.entries[0]['success'] is False
        assert fake_history.entries[0]['status'] == 'error'
    finally:
        close_dialog(dialog)


def test_persistent_and_autocommit_temp_tunnels_are_separate(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        closed = []
        dialog.engine.close_temp_tunnel = lambda ts: closed.append(ts)

        fake_connector = FakeConnector()
        dialog._create_db_connector = MagicMock(return_value=fake_connector)
        dialog._resolve_db_target = MagicMock(return_value=("127.0.0.1", 3306, "T1", None))
        dialog.db_combo.clear()
        dialog.db_combo.addItem("testdb")
        dialog.db_combo.setCurrentText("testdb")

        success, error = dialog._ensure_connection()
        assert success is True
        assert error is None
        assert dialog._persistent_temp_server == "T1"

        dialog._resolve_db_target = MagicMock(return_value=("127.0.0.1", 3306, "T2", None))

        class FakeWorker:
            def __init__(self, *a, **k):
                self.progress = MagicMock()
                self.query_result = MagicMock()
                self.finished = MagicMock()

            def start(self):
                pass

            def isRunning(self):
                return False

        monkeypatch.setattr(
            "src.ui.dialogs.sql_editor_dialog.SQLQueryWorker",
            FakeWorker,
        )
        dialog._execute_with_autocommit(["SELECT 1"], "SELECT 1")
        assert dialog._autocommit_temp_server == "T2"

        dialog._cleanup()
        assert closed == ["T2"]
        assert dialog._persistent_temp_server == "T1"

        dialog._close_db_connection()
        assert closed == ["T2", "T1"]
    finally:
        close_dialog(dialog)


def test_fetch_primary_keys_postgresql_uses_pg_information_schema(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog.config['db_engine'] = 'postgresql'
        fake_cursor = FakeCursor(rows=[{'column_name': 'id'}])
        fake_connection = FakeConnection(cursor_factory=lambda: fake_cursor)
        dialog.db_connection = fake_connection

        result = dialog._fetch_primary_keys("public", "users")

        assert result == ["id"]
        sql = fake_cursor.executed[0][0].lower()
        assert "table_constraints" in sql
        assert "key_column_usage" in sql
        assert "column_key" not in sql
    finally:
        close_dialog(dialog)


def test_clear_result_tabs_prompts_before_dropping_pending_cell_edits(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog._add_result_table(["id"], [[1]], 0.01, "SELECT * FROM users")
        table = dialog.result_tabs.widget(0)
        table._edit_context = {
            'schema': None, 'table': 'users', 'pk_columns': ['id'], 'pk_indices': [0],
            'columns': ['id'], 'pending_edits': {(0, 0): 2},
        }

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
        assert dialog._clear_result_tabs() is False
        assert dialog.result_tabs.count() == 1

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        assert dialog._clear_result_tabs() is True
        assert dialog.result_tabs.count() == 0
    finally:
        close_dialog(dialog)


def test_close_event_warns_for_pending_cell_edits(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog._add_result_table(["id"], [[1]], 0.01, "SELECT * FROM users")
        table = dialog.result_tabs.widget(0)
        table._edit_context = {
            'schema': None, 'table': 'users', 'pk_columns': ['id'], 'pk_indices': [0],
            'columns': ['id'], 'pending_edits': {(0, 0): 2},
        }

        captured = {}

        def fake_question(parent, title, text, *args, **kwargs):
            captured['text'] = text
            return QMessageBox.StandardButton.No

        monkeypatch.setattr(QMessageBox, "question", fake_question)

        event = MagicMock()
        dialog.closeEvent(event)

        assert "셀 편집" in captured['text']
        event.ignore.assert_called_once()
        event.accept.assert_not_called()

        # 실제 종료(finally의 close_dialog)가 동일 확인창에 다시 막히지 않도록 정리
        table._edit_context['pending_edits'].clear()
    finally:
        close_dialog(dialog)


def test_replaced_validation_and_autocomplete_workers_are_retained_until_finished(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        worker = FakeCancelableWorker()

        dialog._retire_worker(worker)

        assert worker.cancelled is True
        assert worker in dialog._retired_workers

        worker.finished.emit()

        assert worker not in dialog._retired_workers
    finally:
        close_dialog(dialog)


def test_metadata_connect_failure_surfaces_label(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        class FakeFailingConnector:
            def connect(self):
                return False, "bad password"

            def disconnect(self):
                pass

        dialog._resolve_db_target = MagicMock(return_value=("127.0.0.1", 3306, None, None))
        dialog._create_db_connector = MagicMock(return_value=FakeFailingConnector())
        dialog.db_combo.clear()
        dialog.db_combo.addItem("app")
        dialog.db_combo.setCurrentText("app")

        dialog._load_metadata("app")

        assert "메타데이터 DB 연결 실패" in dialog.validation_label.text()
        assert "bad password" in dialog.validation_label.text()
    finally:
        close_dialog(dialog)
