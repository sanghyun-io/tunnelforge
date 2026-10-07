"""
스키마 비교 다이얼로그 테스트
- _resolve_connection_params(): 연결 파라미터 검증 헬퍼
- _load_schemas(): 백그라운드 스레드 실행 + tuple 언패킹 + try/finally cleanup 검증
- _start_compare(): 사전 검증 + credentials 조회 + Rust 비교용 endpoint 전달
  + 비교 시점 스키마 이름 캡처
- closeEvent(): 진행 중인 스레드 정리 검증
"""
import time

import pytest
from unittest.mock import MagicMock, patch

# PyQt6 QApplication 필요 (위젯 생성 전 초기화)
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QCloseEvent
import sys

app = QApplication.instance() or QApplication(sys.argv)


from src.ui.dialogs.diff_dialog import SchemaDiffDialog, SchemaCompareThread
from src.core.schema_diff import (
    CompareLevel, CompareResult, SeveritySummary, VersionContext,
    DiffType, TableDiff,
)


def _wait_for_schema_load(dialog, side, timeout=3000):
    """백그라운드 스키마 로드 스레드가 끝날 때까지 대기하고, 큐잉된 시그널을 처리한다."""
    thread = dialog._schema_load_threads.get(side)
    assert thread is not None, f"'{side}' 스키마 로드 스레드가 시작되지 않았습니다"
    finished = thread.wait(timeout)
    QApplication.processEvents()
    assert finished, f"'{side}' 스키마 로드 스레드가 제한 시간 내에 끝나지 않았습니다"


@pytest.fixture
def mock_tunnel_engine():
    engine = MagicMock()
    engine.is_running.return_value = True
    engine.get_connection_info.return_value = ('127.0.0.1', 3307)
    return engine


@pytest.fixture
def mock_config_manager():
    cm = MagicMock()
    cm.get_tunnel_credentials.return_value = ('testuser', 'testpass')
    return cm


@pytest.fixture
def sample_tunnels():
    return [
        {'id': 'tunnel-1', 'name': '서버1', 'local_port': 3307},
        {'id': 'tunnel-2', 'name': '서버2', 'local_port': 3308},
    ]


@pytest.fixture
def dialog(sample_tunnels, mock_tunnel_engine, mock_config_manager):
    """SchemaDiffDialog 인스턴스 (초기 스키마 로드 모킹)

    __init__ 중 _connect_signals()가 source/target 스키마 로드를 백그라운드
    스레드로 시작시키므로, `with patch(...)` 블록이 해제(=create_rust_db_connector 원복)되기
    전에 두 스레드가 끝나도록 대기한다. 대기하지 않으면 스레드가 patch 해제 후에
    실제 Rust connector 를 만들려는 경합이 생긴다.
    """
    with patch('src.ui.dialogs.diff_workers.create_rust_db_connector') as MockConnector:
        mock_conn = MagicMock()
        mock_conn.connect.return_value = (True, 'OK')
        mock_conn.get_schemas.return_value = ['db1', 'db2']
        MockConnector.return_value = mock_conn

        dlg = SchemaDiffDialog(
            tunnels=sample_tunnels,
            tunnel_engine=mock_tunnel_engine,
            config_manager=mock_config_manager,
        )

        for side in ('source', 'target'):
            thread = dlg._schema_load_threads.get(side)
            if thread is not None:
                thread.wait(3000)
                QApplication.processEvents()
    return dlg


# ============================================================
# _resolve_connection_params() 테스트
# ============================================================

class TestResolveConnectionParams:
    """_resolve_connection_params() 헬퍼 테스트"""

    def test_success(self, dialog, mock_tunnel_engine, mock_config_manager):
        """정상: (True, host, port, user, password) 반환"""
        result = dialog._resolve_connection_params('tunnel-1')
        assert result[0] is True
        assert result[1:] == ('127.0.0.1', 3307, 'testuser', 'testpass')

    def test_tunnel_not_running(self, dialog, mock_tunnel_engine):
        """터널 미실행 시 실패"""
        mock_tunnel_engine.is_running.return_value = False
        result = dialog._resolve_connection_params('tunnel-1')
        assert result[0] is False
        assert result[1] == "터널 연결 필요"

    def test_no_host(self, dialog, mock_tunnel_engine):
        """host가 None일 때 실패"""
        mock_tunnel_engine.get_connection_info.return_value = (None, None)
        result = dialog._resolve_connection_params('tunnel-1')
        assert result[0] is False
        assert result[1] == "연결 정보 없음"

    def test_no_credentials(self, dialog, mock_config_manager):
        """자격 증명 없을 때 실패"""
        mock_config_manager.get_tunnel_credentials.return_value = ('', '')
        result = dialog._resolve_connection_params('tunnel-1')
        assert result[0] is False
        assert result[1] == "자격 증명 없음"


# ============================================================
# _load_schemas() 테스트
# ============================================================

class TestLoadSchemas:
    """_load_schemas() 테스트 (백그라운드 스레드에서 실행됨)"""

    def test_success_loads_schemas(self, dialog, mock_tunnel_engine, mock_config_manager):
        """정상 경로: 스키마 목록이 콤보박스에 로드됨"""
        with patch('src.ui.dialogs.diff_workers.create_rust_db_connector') as MockConn:
            mock_conn = MagicMock()
            mock_conn.connect.return_value = (True, 'OK')
            mock_conn.get_schemas.return_value = ['schema_a', 'schema_b', 'schema_c']
            MockConn.return_value = mock_conn

            dialog.source_schema_combo.clear()
            dialog._load_schemas('source')
            _wait_for_schema_load(dialog, 'source')

            # tuple 언패킹으로 호출 확인
            mock_tunnel_engine.get_connection_info.assert_called()
            mock_config_manager.get_tunnel_credentials.assert_called()

            # Rust connector 에 올바른 인자 전달 확인
            MockConn.assert_called_with(
                'mysql', '127.0.0.1', 3307, 'testuser', 'testpass'
            )

            # 스키마 목록 로드 확인
            items = [dialog.source_schema_combo.itemText(i)
                     for i in range(dialog.source_schema_combo.count())]
            assert items == ['schema_a', 'schema_b', 'schema_c']

            # disconnect 호출 (finally 블록)
            mock_conn.disconnect.assert_called_once()

    def test_load_schemas_is_non_blocking(self, dialog):
        """_load_schemas 호출 직후 UI 스레드가 즉시 반환되어야 한다 (동기 블로킹 금지)"""
        with patch('src.ui.dialogs.diff_workers.create_rust_db_connector') as MockConn:
            def _slow_connect():
                time.sleep(0.2)
                return (True, 'OK')

            mock_conn = MagicMock()
            mock_conn.connect.side_effect = _slow_connect
            mock_conn.get_schemas.return_value = ['db1']
            MockConn.return_value = mock_conn

            dialog.source_schema_combo.clear()
            dialog._load_schemas('source')

            # connect()가 아직 끝나지 않았을 시점 — 호출이 동기 블로킹이었다면
            # 이 지점에 도달하기까지 최소 0.2초가 걸렸을 것이고 콤보가 이미 채워졌을 것
            assert dialog.source_schema_combo.count() == 0

            _wait_for_schema_load(dialog, 'source')

            items = [dialog.source_schema_combo.itemText(i)
                     for i in range(dialog.source_schema_combo.count())]
            assert items == ['db1']

    def test_tunnel_not_running(self, dialog, mock_tunnel_engine):
        """터널 미실행 시 '(터널 연결 필요)' 표시 (사전 검증 실패라 스레드 없이 즉시 반영)"""
        mock_tunnel_engine.is_running.return_value = False

        dialog.source_schema_combo.clear()
        dialog._load_schemas('source')

        assert dialog.source_schema_combo.itemText(0) == "(터널 연결 필요)"

    def test_no_connection_info(self, dialog, mock_tunnel_engine):
        """get_connection_info가 (None, None) 반환 시"""
        mock_tunnel_engine.get_connection_info.return_value = (None, None)

        dialog.source_schema_combo.clear()
        dialog._load_schemas('source')

        assert dialog.source_schema_combo.itemText(0) == "(연결 정보 없음)"

    def test_no_credentials(self, dialog, mock_config_manager):
        """자격 증명 없음 시 '(자격 증명 없음)' 표시"""
        mock_config_manager.get_tunnel_credentials.return_value = ('', '')

        dialog.source_schema_combo.clear()
        dialog._load_schemas('source')

        assert dialog.source_schema_combo.itemText(0) == "(자격 증명 없음)"

    def test_connection_failure(self, dialog):
        """DB 연결 실패 시 '(연결 실패)' 표시"""
        with patch('src.ui.dialogs.diff_workers.create_rust_db_connector') as MockConn:
            mock_conn = MagicMock()
            mock_conn.connect.return_value = (False, '연결 거부')
            MockConn.return_value = mock_conn

            dialog.source_schema_combo.clear()
            dialog._load_schemas('source')
            _wait_for_schema_load(dialog, 'source')

            assert dialog.source_schema_combo.itemText(0) == "(연결 실패)"
            # 연결 실패해도 disconnect는 finally에서 호출
            mock_conn.disconnect.assert_called_once()

    def test_exception_shows_error_and_cleanup(self, dialog):
        """예외 발생 시 '(오류)' 표시 + connector cleanup"""
        with patch('src.ui.dialogs.diff_workers.create_rust_db_connector') as MockConn:
            mock_conn = MagicMock()
            mock_conn.connect.side_effect = Exception("네트워크 오류")
            MockConn.return_value = mock_conn

            dialog.source_schema_combo.clear()
            dialog._load_schemas('source')
            _wait_for_schema_load(dialog, 'source')

            assert dialog.source_schema_combo.itemText(0) == "(오류)"
            # finally에서 disconnect 호출 확인
            mock_conn.disconnect.assert_called_once()

    def test_disconnect_exception_swallowed(self, dialog):
        """disconnect에서 예외가 발생해도 무시됨"""
        with patch('src.ui.dialogs.diff_workers.create_rust_db_connector') as MockConn:
            mock_conn = MagicMock()
            mock_conn.connect.return_value = (True, 'OK')
            mock_conn.get_schemas.return_value = ['db1']
            mock_conn.disconnect.side_effect = Exception("disconnect 실패")
            MockConn.return_value = mock_conn

            dialog.source_schema_combo.clear()
            # 예외가 전파되지 않아야 함
            dialog._load_schemas('source')
            _wait_for_schema_load(dialog, 'source')

            items = [dialog.source_schema_combo.itemText(i)
                     for i in range(dialog.source_schema_combo.count())]
            assert items == ['db1']

    def test_target_side(self, dialog, mock_tunnel_engine, mock_config_manager):
        """'target' side도 정상 동작"""
        with patch('src.ui.dialogs.diff_workers.create_rust_db_connector') as MockConn:
            mock_conn = MagicMock()
            mock_conn.connect.return_value = (True, 'OK')
            mock_conn.get_schemas.return_value = ['target_db']
            MockConn.return_value = mock_conn

            dialog.target_schema_combo.clear()
            dialog._load_schemas('target')
            _wait_for_schema_load(dialog, 'target')

            items = [dialog.target_schema_combo.itemText(i)
                     for i in range(dialog.target_schema_combo.count())]
            assert items == ['target_db']

    def test_stale_result_ignored_after_reload(self, dialog):
        """뒤늦게 도착한 옛(stale) 스레드의 결과가 최신 콤보 상태를 덮어쓰면 안 된다

        실제 스레드 스케줄링 순서에 의존하지 않도록, .start()로 스레드를 실행하는
        대신 시그널을 직접 emit하여 '먼저 등록된 스레드가 나중에 완료를 알려오는'
        상황을 결정적으로 재현한다.
        """
        from src.ui.dialogs.diff_dialog import SchemaLoadThread

        old_thread = SchemaLoadThread('source', '127.0.0.1', 3307, 'u', 'p')
        old_thread.loaded.connect(dialog._on_schema_loaded)
        old_thread.load_failed.connect(dialog._on_schema_load_failed)

        new_thread = SchemaLoadThread('source', '127.0.0.1', 3307, 'u', 'p')
        new_thread.loaded.connect(dialog._on_schema_loaded)
        new_thread.load_failed.connect(dialog._on_schema_load_failed)

        # old_thread가 '현재' 스레드였다가 new_thread로 교체된 상황
        dialog._schema_load_threads['source'] = old_thread
        dialog._schema_load_threads['source'] = new_thread

        # 최신 스레드가 먼저 결과를 반영
        new_thread.loaded.emit('source', ['new_db'])
        items = [dialog.source_schema_combo.itemText(i)
                 for i in range(dialog.source_schema_combo.count())]
        assert items == ['new_db']

        # 뒤늦게 도착한 옛 스레드의 결과는 무시되어야 한다
        old_thread.loaded.emit('source', ['old_db'])
        items = [dialog.source_schema_combo.itemText(i)
                 for i in range(dialog.source_schema_combo.count())]
        assert items == ['new_db']


# ============================================================
# _start_compare() 테스트
# ============================================================

class TestStartCompare:
    """_start_compare() 테스트"""

    def _select(self, dialog, source='db1', target='db2'):
        dialog.source_schema_combo.clear()
        dialog.source_schema_combo.addItem(source)
        dialog.target_schema_combo.clear()
        dialog.target_schema_combo.addItem(target)

    def test_passes_endpoints_with_credentials_to_the_rust_compare(
        self, dialog, mock_tunnel_engine, mock_config_manager
    ):
        """터널 접속 정보 + 별도 조회한 자격 증명으로 비교용 endpoint 를 만든다"""
        mock_tunnel_engine.get_connection_info.side_effect = [
            ('127.0.0.1', 3307),  # source
            ('127.0.0.1', 3308),  # target
        ]
        mock_config_manager.get_tunnel_credentials.side_effect = [
            ('src_user', 'src_pw'),
            ('tgt_user', 'tgt_pw'),
        ]
        self._select(dialog)
        dialog.exact_rows_check.setChecked(True)

        with patch('src.ui.dialogs.diff_dialog.SchemaCompareThread') as MockThread:
            dialog._start_compare()

        source, target, level = MockThread.call_args.args
        assert (source.engine, source.host, source.port, source.user, source.password, source.database) == (
            'mysql', '127.0.0.1', 3307, 'src_user', 'src_pw', 'db1')
        assert (target.port, target.user, target.password, target.database) == (3308, 'tgt_user', 'tgt_pw', 'db2')
        assert level == CompareLevel.STANDARD
        assert MockThread.call_args.kwargs == {'exact_row_counts': True}
        MockThread.return_value.start.assert_called_once()

    def test_source_tunnel_not_running_shows_warning(
        self, dialog, mock_tunnel_engine
    ):
        """소스 터널 미실행 시 경고 메시지 표시 후 리턴"""
        mock_tunnel_engine.is_running.return_value = False

        with patch('src.ui.dialogs.diff_dialog.QMessageBox') as MockMsg, \
             patch('src.ui.dialogs.diff_dialog.SchemaCompareThread') as MockThread:
            self._select(dialog)
            dialog._start_compare()

            MockMsg.warning.assert_called_once()
            assert "소스" in MockMsg.warning.call_args[0][2]
            MockThread.assert_not_called()

    def test_target_no_credentials_shows_warning(
        self, dialog, mock_tunnel_engine, mock_config_manager
    ):
        """타겟 자격 증명 없을 때 경고 메시지"""
        mock_config_manager.get_tunnel_credentials.side_effect = [
            ('user', 'pass'),  # source OK
            ('', ''),          # target fail
        ]

        with patch('src.ui.dialogs.diff_dialog.QMessageBox') as MockMsg, \
             patch('src.ui.dialogs.diff_dialog.SchemaCompareThread') as MockThread:
            self._select(dialog)
            dialog._start_compare()

            MockMsg.warning.assert_called_once()
            assert "타겟" in MockMsg.warning.call_args[0][2]
            MockThread.assert_not_called()

    def test_captures_source_and_target_schema_at_compare_start(self, dialog):
        """비교 시작 시점의 스키마 이름을 캡처해야 한다 (완료 후 콤보 변경과 무관하게 고정)"""
        self._select(dialog, 'src_db', 'tgt_db')
        with patch('src.ui.dialogs.diff_dialog.SchemaCompareThread'):
            dialog._start_compare()

        assert dialog._compared_source_schema == 'src_db'
        assert dialog._compared_target_schema == 'tgt_db'

    def test_start_compare_connects_to_compare_finished_not_finished(self, dialog):
        """_start_compare()가 compare_finished(신규 이름)에 연결해야 한다"""
        self._select(dialog)
        with patch('src.ui.dialogs.diff_dialog.SchemaCompareThread') as MockThread:
            dialog._start_compare()

        MockThread.return_value.compare_finished.connect.assert_called_once_with(
            dialog._on_compare_finished
        )


class TestSchemaCompareThread:
    """비교 스레드: Rust facade 호출 결과를 CompareResult 로 내보낸다"""

    def test_compare_finished_signal_exists_separately_from_qthread_finished(self):
        thread = SchemaCompareThread(MagicMock(), MagicMock())
        received = []
        thread.compare_finished.connect(received.append)
        thread.compare_finished.emit("result")
        assert received == ["result"]

    def test_run_calls_facade_and_forwards_progress(self):
        facade = MagicMock()

        def fake_compare(source, target, level, exact_row_counts, on_event):
            on_event({"event": "progress", "message": "소스 스키마 추출 중..."})
            return {"tables": [{"name": "t", "diff_type": "added"}], "summary": {"critical": 1},
                    "sync_sql": "-- sql", "source_version": "8.4.3"}

        facade.compare_schemas.side_effect = fake_compare
        thread = SchemaCompareThread("SRC", "TGT", CompareLevel.STRICT, exact_row_counts=True, facade=facade)
        progress, finished = [], []
        thread.progress.connect(progress.append)
        thread.compare_finished.connect(finished.append)
        thread.run()

        assert facade.compare_schemas.call_args.args == ("SRC", "TGT")
        assert facade.compare_schemas.call_args.kwargs["level"] == "strict"
        assert facade.compare_schemas.call_args.kwargs["exact_row_counts"] is True
        assert progress == ["소스 스키마 추출 중..."]
        result = finished[0]
        assert result.diffs[0].diff_type == DiffType.ADDED
        assert result.summary.critical == 1 and result.sync_sql == "-- sql"

    def test_run_reports_core_errors(self):
        facade = MagicMock()
        facade.compare_schemas.side_effect = RuntimeError("source schema: column query failed")
        thread = SchemaCompareThread("SRC", "TGT", facade=facade)
        errors = []
        thread.error.connect(errors.append)
        thread.run()
        assert errors == ["source schema: column query failed"]


# ============================================================
# _generate_script() - 비교 결과에 담긴 SQL 사용
# ============================================================

class TestGenerateScript:
    """동기화 스크립트는 비교 시점에 Rust core 가 만든 SQL 을 그대로 보여 준다"""

    def test_shows_sync_sql_from_the_compare_result(self, dialog):
        dialog._diffs = [TableDiff(table_name="t1", diff_type=DiffType.MODIFIED)]
        dialog._severity_summary = SeveritySummary(critical=0, warning=1, info=0)
        dialog._sync_sql = "-- sql for schema_at_compare_time"

        # 비교 완료 후 유저가 콤보를 바꿔도 비교 시점에 만든 스크립트를 쓴다
        dialog.target_schema_combo.clear()
        dialog.target_schema_combo.addItem('schema_changed_after_compare')

        with patch('src.ui.dialogs.diff_dialog.SyncScriptDialog') as MockDialog:
            dialog._generate_script()

        MockDialog.assert_called_once_with(dialog, "-- sql for schema_at_compare_time")
        MockDialog.return_value.exec.assert_called_once()

    def test_critical_issues_require_confirmation(self, dialog):
        dialog._diffs = [TableDiff(table_name="t1", diff_type=DiffType.REMOVED)]
        dialog._severity_summary = SeveritySummary(critical=1)
        dialog._sync_sql = "-- sql"

        with patch('src.ui.dialogs.diff_dialog.QMessageBox') as MockMsg, \
             patch('src.ui.dialogs.diff_dialog.SyncScriptDialog') as MockDialog:
            MockMsg.StandardButton = __import__("PyQt6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.StandardButton
            MockMsg.warning.return_value = MockMsg.StandardButton.No
            dialog._generate_script()

        MockMsg.warning.assert_called_once()
        MockDialog.assert_not_called()


# ============================================================
# closeEvent() - 스레드 정리 검증
# ============================================================

class TestCloseEventCleanup:
    """closeEvent 시 진행 중인 스레드를 정리해야 한다"""

    def test_close_waits_for_running_compare_thread(self, dialog):
        mock_thread = MagicMock()
        mock_thread.isRunning.return_value = True
        dialog._compare_thread = mock_thread

        dialog.closeEvent(QCloseEvent())

        mock_thread.wait.assert_called_once()

    def test_close_disconnects_compare_thread_signals(self, dialog):
        """콜백이 파괴된 위젯을 건드리지 않도록 시그널을 먼저 해제해야 한다"""
        mock_thread = MagicMock()
        mock_thread.isRunning.return_value = False
        dialog._compare_thread = mock_thread

        dialog.closeEvent(QCloseEvent())

        mock_thread.progress.disconnect.assert_called_once()
        mock_thread.compare_finished.disconnect.assert_called_once()
        mock_thread.error.disconnect.assert_called_once()

    def test_close_waits_for_pending_schema_load_threads(self, dialog):
        """진행 중인 스키마 로드 스레드도 종료를 기다려야 한다"""
        mock_thread = MagicMock()
        mock_thread.isRunning.return_value = True
        dialog._pending_schema_threads = [mock_thread]
        dialog._compare_thread = None

        dialog.closeEvent(QCloseEvent())

        mock_thread.wait.assert_called_once()


# ============================================================
# _on_compare_finished() 테스트
# ============================================================

def _result(diffs, summary, version_ctx=None, sync_sql="", exact=False):
    return CompareResult(diffs=diffs, summary=summary, version_ctx=version_ctx or VersionContext(),
                         sync_sql=sync_sql, row_counts_exact=exact)


class TestOnCompareFinished:
    """_on_compare_finished(): CompareResult 반영 테스트"""

    def test_stores_result_fields(self, dialog):
        diffs = [TableDiff(table_name="t1", diff_type=DiffType.UNCHANGED, row_count_source=3, row_count_target=3)]
        summary = SeveritySummary(critical=0, warning=1, info=2)
        version_ctx = VersionContext(source_version_str="8.4.6", target_version_str="8.0.42")

        dialog._on_compare_finished(_result(diffs, summary, version_ctx, sync_sql="-- sql"))

        assert dialog._diffs == diffs
        assert dialog._severity_summary == summary
        assert dialog._version_ctx == version_ctx
        assert dialog._sync_sql == "-- sql"
        assert "MySQL 8.4.6" in dialog.severity_bar.text()

    def test_estimated_row_counts_are_marked(self, dialog):
        diffs = [TableDiff(table_name="t1", diff_type=DiffType.UNCHANGED, row_count_source=3, row_count_target=4)]
        dialog._on_compare_finished(_result(diffs, SeveritySummary()))
        assert dialog.diff_tree.topLevelItem(0).text(2) == "≈3 / ≈4"
        dialog._on_compare_finished(_result(diffs, SeveritySummary(), exact=True))
        assert dialog.diff_tree.topLevelItem(0).text(2) == "3 / 4"

    def test_severity_bar_visible_with_issues(self, dialog):
        """심각도 이슈가 있으면 요약 바 표시"""
        diffs = [TableDiff(table_name="t1", diff_type=DiffType.ADDED)]
        dialog._on_compare_finished(_result(diffs, SeveritySummary(critical=1, warning=0, info=0)))

        # isHidden() 사용: 다이얼로그가 show()되지 않아 isVisible()은 항상 False
        assert not dialog.severity_bar.isHidden()
        assert "Critical: 1" in dialog.severity_bar.text()

    def test_severity_bar_hidden_no_issues(self, dialog):
        """심각도 이슈가 없으면 요약 바 숨김"""
        diffs = [TableDiff(table_name="t1", diff_type=DiffType.UNCHANGED)]
        dialog._on_compare_finished(_result(diffs, SeveritySummary()))

        assert dialog.severity_bar.isHidden()

    def test_compare_level_combo_exists(self, dialog):
        """비교 수준 콤보박스 존재 확인"""
        assert hasattr(dialog, 'level_combo')
        assert dialog.level_combo.count() == 3
        # Standard가 기본값
        assert dialog.level_combo.currentData() == CompareLevel.STANDARD
