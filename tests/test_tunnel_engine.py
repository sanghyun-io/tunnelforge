"""
TunnelEngine 테스트
"""
import json
from dataclasses import replace
import pytest
import paramiko
import socket
from unittest.mock import patch, MagicMock, PropertyMock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import src.core.tunnel_engine as tunnel_engine_module
from src.core.ssh_host_trust import SshHostKeyTrustStore, SshHostTrustStoreError


def _server_key():
    public_bytes = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    message = paramiko.Message()
    message.add_string("ssh-ed25519")
    message.add_string(public_bytes)
    return paramiko.Ed25519Key(data=message.asbytes())


def _trust(store, config, key):
    check = store.check(
        config["bastion_host"], int(config["bastion_port"]), key
    )
    store.approve(check, key=key)


def _engine_with_probe(key, store=None):
    store = store or SshHostKeyTrustStore.in_memory()
    return tunnel_engine_module.TunnelEngine(
        trust_store=store,
        host_key_probe=MagicMock(return_value=key),
    )


def _call_ssh_operation(engine, operation, config):
    if operation == "start_tunnel":
        return engine.start_tunnel(config, check_port=False)
    if operation == "create_temp_tunnel":
        return engine.create_temp_tunnel(config)
    if operation == "test_connection":
        return engine.test_connection(config)
    if operation == "test_target_reachable_from_bastion":
        return engine.test_target_reachable_from_bastion(config)
    raise AssertionError(f"Unknown operation: {operation}")


SSH_OPERATIONS = (
    "start_tunnel",
    "create_temp_tunnel",
    "test_connection",
    "test_target_reachable_from_bastion",
)


class TestTunnelEngine:
    """TunnelEngine 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self):
        """각 테스트 전 TunnelEngine 인스턴스 생성"""
        self.server_key = _server_key()
        self.trust_store = SshHostKeyTrustStore.in_memory()
        self.trust_store.approve(
            self.trust_store.check("1.2.3.4", 22, self.server_key),
            key=self.server_key,
        )
        self.engine = tunnel_engine_module.TunnelEngine(
            trust_store=self.trust_store,
            host_key_probe=MagicMock(return_value=self.server_key),
        )

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
            assert sample_tunnel_config['bastion_key'] not in msg

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

    def test_target_reachable_from_bastion_success(self, sample_tunnel_config):
        """Bastion에서 Target DB 포트 도달성 확인 성공"""
        with patch.object(self.engine, '_load_private_key', return_value=MagicMock()):
            with patch('src.core.tunnel_engine.paramiko.SSHClient') as mock_client_cls:
                mock_client = MagicMock()
                mock_transport = MagicMock()
                mock_transport.is_active.return_value = True
                mock_transport.open_channel.return_value = MagicMock()
                mock_client.get_transport.return_value = mock_transport
                mock_client_cls.return_value = mock_client

                success, msg = self.engine.test_target_reachable_from_bastion(sample_tunnel_config)

                assert success is True
                assert "도달 성공" in msg
                mock_transport.open_channel.assert_called_once()

    def test_target_reachable_from_bastion_failure(self, sample_tunnel_config):
        """Bastion에서 Target DB 포트 도달성 확인 실패"""
        with patch.object(self.engine, '_load_private_key', return_value=MagicMock()):
            with patch('src.core.tunnel_engine.paramiko.SSHClient') as mock_client_cls:
                mock_client = MagicMock()
                mock_transport = MagicMock()
                mock_transport.is_active.return_value = True
                mock_transport.open_channel.side_effect = socket.timeout("timed out")
                mock_client.get_transport.return_value = mock_transport
                mock_client_cls.return_value = mock_client

                success, msg = self.engine.test_target_reachable_from_bastion(sample_tunnel_config)

                assert success is False
                assert "시간 초과" in msg


def test_probe_ssh_host_key_performs_no_authentication():
    server_key = _server_key()
    connected_socket = MagicMock()
    transport = MagicMock()
    transport.get_remote_server_key.return_value = server_key

    with patch(
        "src.core.tunnel_engine.socket.create_connection",
        return_value=connected_socket,
    ) as create_connection:
        with patch(
            "src.core.tunnel_engine.paramiko.Transport", return_value=transport
        ) as transport_class:
            result = tunnel_engine_module.probe_ssh_host_key(
                "bastion.example", 2222, 7
            )

    assert result is server_key
    create_connection.assert_called_once_with(("bastion.example", 2222), timeout=7)
    transport_class.assert_called_once_with(connected_socket)
    transport.start_client.assert_called_once_with(timeout=7)
    transport.get_remote_server_key.assert_called_once_with()
    transport.auth_none.assert_not_called()
    transport.auth_password.assert_not_called()
    transport.auth_publickey.assert_not_called()
    transport.close.assert_called_once_with()
    connected_socket.close.assert_called_once_with()


def test_probe_ssh_host_key_closes_resources_when_handshake_fails():
    connected_socket = MagicMock()
    transport = MagicMock()
    transport.start_client.side_effect = socket.timeout("timed out")

    with patch(
        "src.core.tunnel_engine.socket.create_connection",
        return_value=connected_socket,
    ):
        with patch(
            "src.core.tunnel_engine.paramiko.Transport", return_value=transport
        ):
            with pytest.raises(socket.timeout):
                tunnel_engine_module.probe_ssh_host_key(
                    "bastion.example", 22, 5
                )

    transport.close.assert_called_once_with()
    connected_socket.close.assert_called_once_with()


def test_inspect_and_approve_ssh_server_use_public_check(sample_tunnel_config):
    server_key = _server_key()
    store = SshHostKeyTrustStore.in_memory()
    engine = _engine_with_probe(server_key, store)

    check = engine.inspect_ssh_server(sample_tunnel_config)

    assert check.status == "approval_required"
    assert check.approval_token
    assert check.approval_token not in repr(check)
    assert check.fingerprint_sha256.startswith("SHA256:")
    assert not hasattr(check, "key")
    engine.approve_ssh_server(check)
    assert engine.inspect_ssh_server(sample_tunnel_config).status == "trusted"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("host", "forged.example"),
        ("port", 2222),
        ("key_type", "ssh-rsa"),
        ("fingerprint_sha256", "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    ),
)
def test_engine_rejects_forged_approval_check_and_consumes_token(
    sample_tunnel_config, field, value
):
    server_key = _server_key()
    engine = _engine_with_probe(server_key)
    check = engine.inspect_ssh_server(sample_tunnel_config)
    forged = replace(check, **{field: value})

    with pytest.raises(SshHostTrustStoreError):
        engine.approve_ssh_server(forged)
    with pytest.raises(SshHostTrustStoreError):
        engine.approve_ssh_server(check)

    assert engine._host_key_probe.call_count == 2


def test_server_key_changed_after_inspect_cannot_be_approved(
    sample_tunnel_config
):
    inspected_key = _server_key()
    changed_key = _server_key()
    store = SshHostKeyTrustStore.in_memory()
    probe = MagicMock(side_effect=[inspected_key, changed_key])
    engine = tunnel_engine_module.TunnelEngine(
        trust_store=store,
        host_key_probe=probe,
    )
    check = engine.inspect_ssh_server(sample_tunnel_config)

    with pytest.raises(SshHostTrustStoreError):
        engine.approve_ssh_server(check)

    assert probe.call_count == 2
    assert store.check(
        sample_tunnel_config["bastion_host"],
        sample_tunnel_config["bastion_port"],
        inspected_key,
    ).status == "approval_required"


def test_approval_token_cannot_be_reused(sample_tunnel_config):
    server_key = _server_key()
    store = SshHostKeyTrustStore.in_memory()
    engine = _engine_with_probe(server_key, store)
    check = engine.inspect_ssh_server(sample_tunnel_config)

    engine.approve_ssh_server(check)
    with pytest.raises(SshHostTrustStoreError):
        engine.approve_ssh_server(check)

    assert engine._host_key_probe.call_count == 2


@pytest.mark.parametrize("operation", SSH_OPERATIONS)
def test_unknown_host_stops_before_private_key_load(
    operation, sample_tunnel_config
):
    server_key = _server_key()
    engine = _engine_with_probe(server_key)
    engine._load_private_key = MagicMock()
    sample_tunnel_config.update(
        {
            "bastion_user": "sensitive-ssh-user",
            "bastion_key": "C:/sensitive/private-key",
            "db_user": "sensitive-db-user",
            "db_password": "sensitive-db-password",
        }
    )

    result = _call_ssh_operation(engine, operation, sample_tunnel_config)

    assert result[0] is False
    message = result[-1]
    assert "SHA256:" in message
    assert sample_tunnel_config["bastion_key"] not in message
    assert sample_tunnel_config["bastion_user"] not in message
    assert sample_tunnel_config["db_user"] not in message
    assert sample_tunnel_config["db_password"] not in message
    engine._load_private_key.assert_not_called()


@pytest.mark.parametrize("operation", SSH_OPERATIONS)
def test_changed_host_stops_before_private_key_load(
    operation, sample_tunnel_config
):
    first_key = _server_key()
    changed_key = _server_key()
    store = SshHostKeyTrustStore.in_memory()
    _trust(store, sample_tunnel_config, first_key)
    engine = _engine_with_probe(changed_key, store)
    engine._load_private_key = MagicMock()

    result = _call_ssh_operation(engine, operation, sample_tunnel_config)

    assert result[0] is False
    assert "SHA256:" in result[-1]
    assert "변경" in result[-1]
    engine._load_private_key.assert_not_called()


@pytest.mark.parametrize("operation", SSH_OPERATIONS)
def test_corrupt_trust_store_stops_without_exposing_credentials(
    operation, sample_tunnel_config, tmp_path
):
    path = tmp_path / "ssh_host_trust.json"
    path.write_text(json.dumps({"version": 99, "hosts": []}), encoding="utf-8")
    engine = _engine_with_probe(_server_key(), SshHostKeyTrustStore(path))
    engine._load_private_key = MagicMock()
    sample_tunnel_config.update(
        {
            "bastion_key": "C:/sensitive/private-key",
            "db_user": "sensitive-db-user",
            "db_password": "sensitive-db-password",
        }
    )

    result = _call_ssh_operation(engine, operation, sample_tunnel_config)

    assert result[0] is False
    assert "신뢰 저장소" in result[-1]
    assert sample_tunnel_config["bastion_key"] not in result[-1]
    assert sample_tunnel_config["db_user"] not in result[-1]
    assert sample_tunnel_config["db_password"] not in result[-1]
    engine._load_private_key.assert_not_called()


def test_forwarder_receives_fresh_trusted_key(sample_tunnel_config):
    server_key = _server_key()
    store = SshHostKeyTrustStore.in_memory()
    _trust(store, sample_tunnel_config, server_key)
    engine = _engine_with_probe(server_key, store)
    engine._load_private_key = MagicMock(return_value=MagicMock())

    with patch("src.core.tunnel_engine.SSHTunnelForwarder") as forwarder:
        forwarder.return_value = MagicMock()
        success, _message = engine.start_tunnel(
            sample_tunnel_config, check_port=False
        )

    assert success is True
    assert forwarder.call_args.kwargs["ssh_host_key"] is server_key


@pytest.mark.parametrize(
    "operation",
    ("start_tunnel", "create_temp_tunnel", "test_connection"),
)
def test_all_forwarder_paths_disable_ssh_config_and_pin_raw_endpoint(
    operation, sample_tunnel_config
):
    server_key = _server_key()
    store = SshHostKeyTrustStore.in_memory()
    _trust(store, sample_tunnel_config, server_key)
    engine = _engine_with_probe(server_key, store)
    engine._load_private_key = MagicMock(return_value=MagicMock())
    engine.test_target_reachable_from_bastion = MagicMock(
        return_value=(True, "reachable")
    )

    with patch("src.core.tunnel_engine.SSHTunnelForwarder") as forwarder:
        forwarder.return_value = MagicMock()
        if operation == "start_tunnel":
            result = engine.start_tunnel(sample_tunnel_config, check_port=False)
        elif operation == "create_temp_tunnel":
            result = engine.create_temp_tunnel(sample_tunnel_config)
        else:
            result = engine.test_connection(sample_tunnel_config)

    assert result[0] is True
    assert forwarder.call_count == 1
    assert forwarder.call_args.kwargs["ssh_config_file"] is None
    assert forwarder.call_args.kwargs["ssh_host_key"] is server_key


def test_target_preflight_uses_reject_policy_and_expected_key(
    sample_tunnel_config
):
    sample_tunnel_config["bastion_port"] = 2222
    server_key = _server_key()
    store = SshHostKeyTrustStore.in_memory()
    _trust(store, sample_tunnel_config, server_key)
    engine = _engine_with_probe(server_key, store)
    engine._load_private_key = MagicMock(return_value=MagicMock())
    ssh_client = MagicMock()
    transport = MagicMock()
    transport.is_active.return_value = True
    transport.open_channel.return_value = MagicMock()
    ssh_client.get_transport.return_value = transport

    with patch(
        "src.core.tunnel_engine.paramiko.SSHClient", return_value=ssh_client
    ):
        success, _message = engine.test_target_reachable_from_bastion(
            sample_tunnel_config
        )

    assert success is True
    ssh_client.get_host_keys.return_value.add.assert_called_once_with(
        "[1.2.3.4]:2222", "ssh-ed25519", server_key
    )
    ssh_client.set_missing_host_key_policy.assert_called_once()
    policy = ssh_client.set_missing_host_key_policy.call_args.args[0]
    assert isinstance(policy, paramiko.RejectPolicy)
