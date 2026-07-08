"""
스키마 비교 다이얼로그 테스트
- _resolve_connection_params(): 연결 파라미터 검증 헬퍼
- _load_schemas(): 백그라운드 스레드 실행 + tuple 언패킹 + try/finally cleanup 검증
- _start_compare(): 사전 검증 + tuple 언패킹 + credentials 조회 검증
  + 이전 커넥터 정리 + 비교 시점 스키마 이름 캡처
- closeEvent(): 진행 중인 스레드 정리 후 커넥터 disconnect 검증
"""
import time

import pytest
from unittest.mock import MagicMock, patch, call

# PyQt6 QApplication 필요 (위젯 생성 전 초기화)
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QCloseEvent
import sys

app = QApplication.instance() or QApplication(sys.argv)


from src.ui.dialogs.diff_dialog import SchemaDiffDialog, SchemaCompareThread
from src.core.schema_diff import (
    CompareLevel, SeveritySummary, VersionContext,
    DiffType, TableDiff, ColumnDiff, ColumnInfo, DiffSeverity,
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
    스레드로 시작시키므로, `with patch(...)` 블록이 해제(=MySQLConnector 원복)되기
    전에 두 스레드가 끝나도록 대기한다. 대기하지 않으면 스레드가 patch 해제 후에
    실제 MySQLConnector를 호출하려는 경합이 생긴다.
    """
    with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConnector:
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
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
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

            # MySQLConnector에 올바른 인자 전달 확인
            MockConn.assert_called_with(
                host='127.0.0.1', port=3307,
                user='testuser', password='testpass'
            )

            # 스키마 목록 로드 확인
            items = [dialog.source_schema_combo.itemText(i)
                     for i in range(dialog.source_schema_combo.count())]
            assert items == ['schema_a', 'schema_b', 'schema_c']

            # disconnect 호출 (finally 블록)
            mock_conn.disconnect.assert_called_once()

    def test_load_schemas_is_non_blocking(self, dialog):
        """_load_schemas 호출 직후 UI 스레드가 즉시 반환되어야 한다 (동기 블로킹 금지)"""
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
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
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
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
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
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
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
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
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
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

    def test_uses_tuple_unpacking_and_credentials(
        self, dialog, mock_tunnel_engine, mock_config_manager
    ):
        """tuple 언패킹 + 자격 증명 별도 조회 확인"""
        mock_tunnel_engine.get_connection_info.side_effect = [
            ('127.0.0.1', 3307),  # source
            ('127.0.0.1', 3308),  # target
        ]
        mock_config_manager.get_tunnel_credentials.side_effect = [
            ('src_user', 'src_pw'),
            ('tgt_user', 'tgt_pw'),
        ]

        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
            mock_src = MagicMock()
            mock_src.connect.return_value = (True, 'OK')
            mock_tgt = MagicMock()
            mock_tgt.connect.return_value = (True, 'OK')
            MockConn.side_effect = [mock_src, mock_tgt]

            # 스키마 선택 설정
            dialog.source_schema_combo.clear()
            dialog.source_schema_combo.addItem('db1')
            dialog.target_schema_combo.clear()
            dialog.target_schema_combo.addItem('db2')

            with patch.object(dialog, '_compare_thread', None):
                with patch('src.ui.dialogs.diff_dialog.SchemaCompareThread') as MockThread:
                    mock_thread = MagicMock()
                    MockThread.return_value = mock_thread

                    dialog._start_compare()

                    # MySQLConnector 호출 인자 확인
                    calls = MockConn.call_args_list
                    assert calls[0] == call(
                        host='127.0.0.1', port=3307,
                        user='src_user', password='src_pw'
                    )
                    assert calls[1] == call(
                        host='127.0.0.1', port=3308,
                        user='tgt_user', password='tgt_pw'
                    )

    def test_cleanup_on_source_connect_failure(
        self, dialog, mock_tunnel_engine, mock_config_manager
    ):
        """소스 연결 실패 시 connector cleanup"""
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn, \
             patch('src.ui.dialogs.diff_dialog.QMessageBox'):
            mock_conn = MagicMock()
            mock_conn.connect.return_value = (False, '실패')
            MockConn.return_value = mock_conn

            dialog.source_schema_combo.clear()
            dialog.source_schema_combo.addItem('db1')
            dialog.target_schema_combo.clear()
            dialog.target_schema_combo.addItem('db2')

            with patch.object(dialog, '_compare_thread', None):
                dialog._start_compare()

            # connector가 정리되었는지 확인
            assert dialog._source_connector is None

    def test_source_tunnel_not_running_shows_warning(
        self, dialog, mock_tunnel_engine
    ):
        """소스 터널 미실행 시 경고 메시지 표시 후 리턴"""
        mock_tunnel_engine.is_running.return_value = False

        with patch('src.ui.dialogs.diff_dialog.QMessageBox') as MockMsg:
            dialog.source_schema_combo.clear()
            dialog.source_schema_combo.addItem('db1')
            dialog.target_schema_combo.clear()
            dialog.target_schema_combo.addItem('db2')

            dialog._start_compare()

            MockMsg.warning.assert_called_once()
            assert "소스" in MockMsg.warning.call_args[0][2]

    def test_target_no_credentials_shows_warning(
        self, dialog, mock_tunnel_engine, mock_config_manager
    ):
        """타겟 자격 증명 없을 때 경고 메시지"""
        # 소스는 정상, 타겟만 credentials 없음
        mock_config_manager.get_tunnel_credentials.side_effect = [
            ('user', 'pass'),  # source OK
            ('', ''),          # target fail
        ]

        with patch('src.ui.dialogs.diff_dialog.QMessageBox') as MockMsg:
            dialog.source_schema_combo.clear()
            dialog.source_schema_combo.addItem('db1')
            dialog.target_schema_combo.clear()
            dialog.target_schema_combo.addItem('db2')

            dialog._start_compare()

            MockMsg.warning.assert_called_once()
            assert "타겟" in MockMsg.warning.call_args[0][2]

    def test_target_connect_failure_cleans_up_both(
        self, dialog, mock_tunnel_engine, mock_config_manager
    ):
        """타겟 연결 실패 시 소스/타겟 모두 정리"""
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn, \
             patch('src.ui.dialogs.diff_dialog.QMessageBox'):
            mock_src = MagicMock()
            mock_src.connect.return_value = (True, 'OK')
            mock_tgt = MagicMock()
            mock_tgt.connect.return_value = (False, '타겟 실패')
            MockConn.side_effect = [mock_src, mock_tgt]

            dialog.source_schema_combo.clear()
            dialog.source_schema_combo.addItem('db1')
            dialog.target_schema_combo.clear()
            dialog.target_schema_combo.addItem('db2')

            with patch.object(dialog, '_compare_thread', None):
                dialog._start_compare()

            assert dialog._source_connector is None
            assert dialog._target_connector is None


# ============================================================
# _start_compare() - 이전 커넥터 정리 + 비교 시점 스키마 캡처
# ============================================================

class TestStartCompareClearsPreviousConnectors:
    """반복 비교 시 이전 커넥터를 정리하고 새로 생성해야 한다 (Rust core 세션 누수 방지)"""

    def test_disconnects_previous_connectors_before_new_compare(
        self, dialog, mock_tunnel_engine, mock_config_manager
    ):
        old_source = MagicMock()
        old_target = MagicMock()
        dialog._source_connector = old_source
        dialog._target_connector = old_target

        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
            mock_src = MagicMock()
            mock_src.connect.return_value = (True, 'OK')
            mock_tgt = MagicMock()
            mock_tgt.connect.return_value = (True, 'OK')
            MockConn.side_effect = [mock_src, mock_tgt]

            dialog.source_schema_combo.clear()
            dialog.source_schema_combo.addItem('db1')
            dialog.target_schema_combo.clear()
            dialog.target_schema_combo.addItem('db2')

            with patch.object(dialog, '_compare_thread', None):
                with patch('src.ui.dialogs.diff_dialog.SchemaCompareThread'):
                    dialog._start_compare()

            old_source.disconnect.assert_called_once()
            old_target.disconnect.assert_called_once()
            # 이전 인스턴스가 아니라 새로 생성된 커넥터로 교체되어야 함
            assert dialog._source_connector is mock_src
            assert dialog._target_connector is mock_tgt


class TestStartCompareCapturesSchemaNames:
    """비교 시작 시점의 스키마 이름을 캡처해야 한다 (완료 후 콤보 변경과 무관하게 고정)"""

    def test_captures_source_and_target_schema_at_compare_start(
        self, dialog, mock_tunnel_engine, mock_config_manager
    ):
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
            mock_src = MagicMock()
            mock_src.connect.return_value = (True, 'OK')
            mock_tgt = MagicMock()
            mock_tgt.connect.return_value = (True, 'OK')
            MockConn.side_effect = [mock_src, mock_tgt]

            dialog.source_schema_combo.clear()
            dialog.source_schema_combo.addItem('src_db')
            dialog.target_schema_combo.clear()
            dialog.target_schema_combo.addItem('tgt_db')

            with patch.object(dialog, '_compare_thread', None):
                with patch('src.ui.dialogs.diff_dialog.SchemaCompareThread'):
                    dialog._start_compare()

        assert dialog._compared_source_schema == 'src_db'
        assert dialog._compared_target_schema == 'tgt_db'


class TestSchemaCompareThreadSignals:
    """완료 시그널이 QThread.finished를 가리지 않는지 확인"""

    def test_compare_finished_signal_exists_separately_from_qthread_finished(self):
        thread = SchemaCompareThread(MagicMock(), MagicMock(), 'src_db', 'tgt_db')

        assert hasattr(thread, 'compare_finished')

        received = []
        thread.compare_finished.connect(
            lambda diffs, summary, version_ctx: received.append((diffs, summary, version_ctx))
        )
        thread.compare_finished.emit([], MagicMock(), MagicMock())
        assert len(received) == 1

    def test_start_compare_connects_to_compare_finished_not_finished(
        self, dialog, mock_tunnel_engine, mock_config_manager
    ):
        """_start_compare()가 compare_finished(신규 이름)에 연결해야 한다"""
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
            mock_src = MagicMock()
            mock_src.connect.return_value = (True, 'OK')
            mock_tgt = MagicMock()
            mock_tgt.connect.return_value = (True, 'OK')
            MockConn.side_effect = [mock_src, mock_tgt]

            dialog.source_schema_combo.clear()
            dialog.source_schema_combo.addItem('db1')
            dialog.target_schema_combo.clear()
            dialog.target_schema_combo.addItem('db2')

            with patch.object(dialog, '_compare_thread', None):
                with patch('src.ui.dialogs.diff_dialog.SchemaCompareThread') as MockThread:
                    mock_thread = MagicMock()
                    MockThread.return_value = mock_thread

                    dialog._start_compare()

                    mock_thread.compare_finished.connect.assert_called_once_with(
                        dialog._on_compare_finished
                    )


# ============================================================
# _generate_script() - 비교 시점 스키마 사용 검증
# ============================================================

class TestGenerateScriptUsesCompareTimeSchema:
    """동기화 스크립트는 비교 시작 시점에 캡처한 타겟 스키마를 사용해야 한다"""

    def test_uses_captured_target_schema_not_live_combo(self, dialog):
        dialog._diffs = [TableDiff(table_name="t1", diff_type=DiffType.UNCHANGED)]
        dialog._severity_summary = SeveritySummary(critical=0, warning=0, info=0)
        dialog._compared_target_schema = 'schema_at_compare_time'

        # 비교 완료 후 유저가 콤보를 바꿔도 스크립트는 비교 시점 스키마를 써야 한다
        dialog.target_schema_combo.clear()
        dialog.target_schema_combo.addItem('schema_changed_after_compare')

        with patch('src.ui.dialogs.diff_dialog.SyncScriptGenerator') as MockGen, \
             patch('src.ui.dialogs.diff_dialog.SyncScriptDialog') as MockDialog:
            mock_generator = MagicMock()
            mock_generator.generate_sync_script.return_value = "-- sql"
            MockGen.return_value = mock_generator
            mock_dialog_instance = MagicMock()
            MockDialog.return_value = mock_dialog_instance

            dialog._generate_script()

            mock_generator.generate_sync_script.assert_called_once_with(
                dialog._diffs, 'schema_at_compare_time'
            )
            mock_dialog_instance.exec.assert_called_once()


# ============================================================
# closeEvent() - 스레드 정리 후 커넥터 disconnect 검증
# ============================================================

class TestCloseEventCleanup:
    """closeEvent 시 진행 중인 스레드를 정리한 뒤 커넥터를 disconnect해야 한다"""

    def test_close_waits_for_running_compare_thread_before_disconnect(self, dialog):
        mock_thread = MagicMock()
        mock_thread.isRunning.return_value = True
        dialog._compare_thread = mock_thread

        mock_source = MagicMock()
        mock_target = MagicMock()
        dialog._source_connector = mock_source
        dialog._target_connector = mock_target

        event = QCloseEvent()
        dialog.closeEvent(event)

        mock_thread.wait.assert_called_once()
        mock_source.disconnect.assert_called_once()
        mock_target.disconnect.assert_called_once()
        assert dialog._source_connector is None
        assert dialog._target_connector is None

    def test_close_disconnects_compare_thread_signals(self, dialog):
        """콜백이 파괴된 위젯을 건드리지 않도록 시그널을 먼저 해제해야 한다"""
        mock_thread = MagicMock()
        mock_thread.isRunning.return_value = False
        dialog._compare_thread = mock_thread

        event = QCloseEvent()
        dialog.closeEvent(event)

        mock_thread.progress.disconnect.assert_called_once()
        mock_thread.compare_finished.disconnect.assert_called_once()
        mock_thread.error.disconnect.assert_called_once()

    def test_close_without_compare_thread_still_disconnects_connectors(self, dialog):
        """비교 스레드가 없어도 커넥터 정리는 정상 동작해야 한다"""
        dialog._compare_thread = None
        mock_source = MagicMock()
        dialog._source_connector = mock_source
        dialog._target_connector = None

        event = QCloseEvent()
        dialog.closeEvent(event)

        mock_source.disconnect.assert_called_once()

    def test_close_waits_for_pending_schema_load_threads(self, dialog):
        """진행 중인 스키마 로드 스레드도 종료를 기다려야 한다"""
        mock_thread = MagicMock()
        mock_thread.isRunning.return_value = True
        dialog._pending_schema_threads = [mock_thread]
        dialog._compare_thread = None

        event = QCloseEvent()
        dialog.closeEvent(event)

        mock_thread.wait.assert_called_once()


# ============================================================
# _on_compare_finished() 테스트
# ============================================================

class TestOnCompareFinished:
    """_on_compare_finished() 시그널 인자 변경 반영 테스트"""

    def test_accepts_three_args(self, dialog):
        """summary, version_ctx 인자 수신 확인"""
        diffs = [TableDiff(table_name="t1", diff_type=DiffType.UNCHANGED)]
        summary = SeveritySummary(critical=0, warning=1, info=2)
        version_ctx = VersionContext(
            source_version=(8, 4, 6),
            target_version=(8, 0, 42),
            source_version_str="8.4.6",
            target_version_str="8.0.42",
        )

        dialog._on_compare_finished(diffs, summary, version_ctx)

        assert dialog._diffs == diffs
        assert dialog._severity_summary == summary
        assert dialog._version_ctx == version_ctx

    def test_severity_bar_visible_with_issues(self, dialog):
        """심각도 이슈가 있으면 요약 바 표시"""
        diffs = [TableDiff(table_name="t1", diff_type=DiffType.ADDED,
                           source_schema=MagicMock())]
        summary = SeveritySummary(critical=1, warning=0, info=0)
        version_ctx = VersionContext()

        dialog._on_compare_finished(diffs, summary, version_ctx)

        # isHidden() 사용: 다이얼로그가 show()되지 않아 isVisible()은 항상 False
        assert not dialog.severity_bar.isHidden()
        assert "Critical: 1" in dialog.severity_bar.text()

    def test_severity_bar_hidden_no_issues(self, dialog):
        """심각도 이슈가 없으면 요약 바 숨김"""
        diffs = [TableDiff(table_name="t1", diff_type=DiffType.UNCHANGED)]
        summary = SeveritySummary(critical=0, warning=0, info=0)
        version_ctx = VersionContext()

        dialog._on_compare_finished(diffs, summary, version_ctx)

        assert dialog.severity_bar.isHidden()

    def test_compare_level_combo_exists(self, dialog):
        """비교 수준 콤보박스 존재 확인"""
        assert hasattr(dialog, 'level_combo')
        assert dialog.level_combo.count() == 3
        # Standard가 기본값
        assert dialog.level_combo.currentData() == CompareLevel.STANDARD
