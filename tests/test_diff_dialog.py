"""
스키마 비교 다이얼로그 테스트
- _resolve_connection_params(): 연결 파라미터 검증 헬퍼
- _load_schemas(): tuple 언패킹 + try/finally cleanup 검증
- _start_compare(): 사전 검증 + tuple 언패킹 + credentials 조회 검증
"""
import pytest
from unittest.mock import MagicMock, patch, call

# PyQt6 QApplication 필요 (위젯 생성 전 초기화)
from PyQt6.QtWidgets import QApplication
import sys

app = QApplication.instance() or QApplication(sys.argv)


from src.ui.dialogs.diff_dialog import SchemaDiffDialog


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
    """SchemaDiffDialog 인스턴스 (초기 스키마 로드 모킹)"""
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
    """_load_schemas() 테스트"""

    def test_success_loads_schemas(self, dialog, mock_tunnel_engine, mock_config_manager):
        """정상 경로: 스키마 목록이 콤보박스에 로드됨"""
        with patch('src.ui.dialogs.diff_dialog.MySQLConnector') as MockConn:
            mock_conn = MagicMock()
            mock_conn.connect.return_value = (True, 'OK')
            mock_conn.get_schemas.return_value = ['schema_a', 'schema_b', 'schema_c']
            MockConn.return_value = mock_conn

            dialog.source_schema_combo.clear()
            dialog._load_schemas('source')

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

    def test_tunnel_not_running(self, dialog, mock_tunnel_engine):
        """터널 미실행 시 '(터널 연결 필요)' 표시"""
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

            items = [dialog.target_schema_combo.itemText(i)
                     for i in range(dialog.target_schema_combo.count())]
            assert items == ['target_db']


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
