"""
pytest 공용 fixtures
"""
import pytest
import os
import json
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_ssh_tunnel():
    """SSHTunnelForwarder Mock"""
    with patch('sshtunnel.SSHTunnelForwarder') as mock:
        mock_instance = MagicMock()
        mock_instance.is_active = True
        mock_instance.local_bind_port = 3307
        mock.return_value = mock_instance
        yield mock


@pytest.fixture
def temp_config_dir(tmp_path):
    """임시 설정 디렉토리"""
    config_dir = tmp_path / 'TunnelForge'
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def sample_tunnel_config():
    """샘플 터널 설정"""
    return {
        'id': 'test-tunnel-001',
        'name': '테스트 서버',
        'bastion_host': '1.2.3.4',
        'bastion_port': 22,
        'bastion_user': 'ec2-user',
        'bastion_key': '/path/to/key',
        'remote_host': 'localhost',
        'remote_port': 3306,
        'local_port': 3307,
        'connection_mode': 'ssh_tunnel'
    }


@pytest.fixture
def sample_direct_config():
    """직접 연결 모드 샘플 설정"""
    return {
        'id': 'direct-tunnel-001',
        'name': '직접 연결 서버',
        'remote_host': '192.168.1.100',
        'remote_port': 3306,
        'connection_mode': 'direct'
    }


@pytest.fixture
def sample_config_data():
    """샘플 전체 설정 데이터"""
    return {
        'tunnels': [
            {
                'id': 'test-001',
                'name': '테스트 서버 1',
                'bastion_host': '1.2.3.4',
                'bastion_port': 22,
                'bastion_user': 'user1',
                'bastion_key': '/path/to/key1',
                'remote_host': 'db1.example.com',
                'remote_port': 3306,
                'local_port': 3307
            },
            {
                'id': 'test-002',
                'name': '테스트 서버 2',
                'remote_host': 'db2.example.com',
                'remote_port': 3306,
                'connection_mode': 'direct'
            }
        ],
        'settings': {
            'close_action': 'ask',
            'auto_update_check': True
        }
    }


@pytest.fixture
def mock_paramiko_key():
    """Paramiko SSH 키 Mock"""
    with patch('paramiko.RSAKey.from_private_key_file') as mock:
        mock_key = MagicMock()
        mock.return_value = mock_key
        yield mock


@pytest.fixture
def mock_subprocess_mysqlsh():
    """mysqlsh subprocess Mock"""
    with patch('subprocess.run') as mock:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'Ver 8.0.32'
        mock_result.stderr = ''
        mock.return_value = mock_result
        yield mock
