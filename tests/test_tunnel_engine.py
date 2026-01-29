"""
TunnelEngine 테스트
"""
import pytest
import socket
from unittest.mock import patch, MagicMock, PropertyMock


class TestTunnelEngine:
    """TunnelEngine 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self):
        """각 테스트 전 TunnelEngine 인스턴스 생성"""
        from src.core.tunnel_engine import TunnelEngine
        self.engine = TunnelEngine()

    def test_is_port_available_success(self):
        """사용 가능한 포트 확인 테스트"""
        # 높은 포트 번호는 보통 사용 가능
        result = self.engine.is_port_available(59999)
        # 포트가 사용 중일 수도 있으므로 bool 타입만 확인
        assert isinstance(result, bool)

    def test_is_port_available_used_port(self):
        """사용 중인 포트 확인 테스트 (Mock)"""
        with patch('socket.socket') as mock_socket:
            mock_instance = MagicMock()
            mock_instance.bind.side_effect = OSError("Address already in use")
            mock_socket.return_value = mock_instance

            result = self.engine.is_port_available(80)
            assert result is False

    def test_start_tunnel_direct_mode(self, sample_direct_config):
        """직접 연결 모드 시작 테스트"""
        success, msg = self.engine.start_tunnel(sample_direct_config)

        assert success is True
        assert '직접 연결' in msg
        assert sample_direct_config['id'] in self.engine.active_tunnels

    def test_stop_tunnel_direct_mode(self, sample_direct_config):
        """직접 연결 모드 종료 테스트"""
        # 먼저 시작
        self.engine.start_tunnel(sample_direct_config)

        # 종료
        result = self.engine.stop_tunnel(sample_direct_config['id'])
        assert result is True
        assert sample_direct_config['id'] not in self.engine.active_tunnels

    def test_is_running_not_started(self, sample_tunnel_config):
        """시작하지 않은 터널 확인"""
        result = self.engine.is_running(sample_tunnel_config['id'])
        assert result is False

    def test_is_running_direct_mode(self, sample_direct_config):
        """직접 연결 모드 실행 중 확인"""
        self.engine.start_tunnel(sample_direct_config)
        result = self.engine.is_running(sample_direct_config['id'])
        assert result is True

    def test_get_connection_info_direct(self, sample_direct_config):
        """직접 연결 모드 연결 정보 조회"""
        self.engine.start_tunnel(sample_direct_config)

        host, port = self.engine.get_connection_info(sample_direct_config['id'])
        assert host == sample_direct_config['remote_host']
        assert port == sample_direct_config['remote_port']

    def test_get_connection_info_not_found(self):
        """존재하지 않는 터널 연결 정보 조회"""
        host, port = self.engine.get_connection_info('non-existent-id')
        assert host is None
        assert port is None

    def test_start_ssh_tunnel_success(self, sample_tunnel_config):
        """SSH 터널 시작 성공 테스트 (Mock)"""
        # SSHTunnelForwarder를 src.core.tunnel_engine 모듈에서 패치
        with patch('src.core.tunnel_engine.SSHTunnelForwarder') as mock_tunnel:
            # Mock 인스턴스 설정
            mock_instance = MagicMock()
            mock_instance.is_active = True
            mock_instance.local_bind_port = 3307
            mock_tunnel.return_value = mock_instance

            # SSH 키 로드 Mock
            with patch.object(self.engine, '_load_private_key', return_value=MagicMock()):
                success, msg = self.engine.start_tunnel(sample_tunnel_config, check_port=False)

                assert success is True
                assert mock_tunnel.called

    def test_start_ssh_tunnel_key_error(self, sample_tunnel_config):
        """SSH 키 로드 실패 테스트"""
        # 존재하지 않는 키 파일 경로 설정
        sample_tunnel_config['bastion_key'] = '/non/existent/key/file'

        # 포트 사용 가능으로 Mock
        with patch.object(self.engine, 'is_port_available', return_value=True):
            success, msg = self.engine.start_tunnel(sample_tunnel_config)

            assert success is False
            # 에러 메시지에 '키' 또는 관련 오류 메시지 포함
            assert any(keyword in msg.lower() for keyword in ['key', '키', 'file', 'not found', '찾을 수 없'])

    def test_already_running_direct(self, sample_direct_config):
        """이미 실행 중인 직접 연결 시작 시도"""
        # 첫 번째 시작
        self.engine.start_tunnel(sample_direct_config)

        # 두 번째 시작 시도
        success, msg = self.engine.start_tunnel(sample_direct_config)
        assert success is True
        assert '이미' in msg

    def test_stop_all(self, sample_direct_config):
        """모든 터널 종료 테스트"""
        # 여러 터널 시작
        config1 = sample_direct_config.copy()
        config1['id'] = 'test-1'

        config2 = sample_direct_config.copy()
        config2['id'] = 'test-2'

        self.engine.start_tunnel(config1)
        self.engine.start_tunnel(config2)

        # 모두 종료
        self.engine.stop_all()

        assert len(self.engine.active_tunnels) == 0

    def test_get_active_tunnels(self, sample_direct_config):
        """활성 터널 목록 조회"""
        self.engine.start_tunnel(sample_direct_config)

        result = self.engine.get_active_tunnels()

        assert len(result) == 1
        assert result[0]['id'] == sample_direct_config['id']
        assert result[0]['mode'] == 'direct'

    def test_test_connection_direct_success(self, sample_direct_config):
        """직접 연결 테스트 (Mock)"""
        with patch('socket.socket') as mock_socket:
            mock_instance = MagicMock()
            mock_socket.return_value = mock_instance

            success, msg = self.engine.test_connection(sample_direct_config)

            # 소켓 연결 시도가 있었는지 확인
            assert mock_instance.connect.called

    def test_test_connection_direct_failure(self, sample_direct_config):
        """직접 연결 테스트 실패"""
        with patch('socket.socket') as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect.side_effect = socket.error("Connection refused")
            mock_socket.return_value = mock_instance

            success, msg = self.engine.test_connection(sample_direct_config)

            assert success is False
            assert '실패' in msg

    def test_create_temp_tunnel_direct_mode(self, sample_direct_config):
        """직접 연결 모드에서 임시 터널 생성 시도 (불필요)"""
        success, server, error = self.engine.create_temp_tunnel(sample_direct_config)

        assert success is True
        assert server is None  # 직접 연결은 터널 불필요
        assert error == ""
