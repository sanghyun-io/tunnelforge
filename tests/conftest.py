"""
pytest 공용 fixtures
"""
import pytest
import os
import json
from pathlib import Path
from unittest.mock import MagicMock, patch


@pytest.fixture
def canonical_constants():
    """mysql-upgrade-checker canonical 상수 기준값 로드"""
    fixture_path = Path(__file__).parent / "fixtures" / "canonical_constants.json"
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)


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


# ============================================================
# Migration 테스트용 Fixtures
# ============================================================

class FakeMySQLConnector:
    """결정론적 DB 동작 시뮬레이터

    DB 없이 MigrationAnalyzer/FixWizard 등을 테스트하기 위한
    MySQLConnector 대체 객체.
    """

    def __init__(self):
        self.query_results = {}    # query pattern → result
        self.executed_queries = []  # 실행 이력
        self.fail_on = {}          # query pattern → Exception
        self._committed = False
        self._rolled_back = False
        self._tables = {}          # schema → [table_names]
        self.connection = MagicMock()

    def execute(self, query, params=None):
        self.executed_queries.append((query, params))
        query_str = query.strip()

        # fail_on 패턴 매칭
        for pattern, exc in self.fail_on.items():
            if pattern in query_str:
                raise exc

        # query_results 패턴 매칭
        for pattern, result in self.query_results.items():
            if pattern in query_str:
                return result

        return []

    def get_tables(self, schema):
        return self._tables.get(schema, [])

    def commit(self):
        self._committed = True

    def rollback(self):
        self._rolled_back = True

    def get_session_sql_mode(self) -> str:
        return ''

    def set_session_sql_mode(self, mode: str) -> bool:
        return True


@pytest.fixture
def fake_connector():
    """빈 FakeMySQLConnector"""
    return FakeMySQLConnector()


@pytest.fixture
def fake_connector_with_data():
    """일반적인 마이그레이션 시나리오용 FakeMySQLConnector"""
    conn = FakeMySQLConnector()
    conn._tables = {'test_db': ['users', 'orders', 'products']}
    conn.query_results = {
        'INFORMATION_SCHEMA.TABLES': [
            {'TABLE_NAME': 'users', 'TABLE_COLLATION': 'utf8_general_ci', 'ENGINE': 'InnoDB', 'TABLE_ROWS': 100},
            {'TABLE_NAME': 'orders', 'TABLE_COLLATION': 'utf8mb4_unicode_ci', 'ENGINE': 'InnoDB', 'TABLE_ROWS': 500},
        ],
        'INFORMATION_SCHEMA.COLUMNS': [
            {'TABLE_NAME': 'users', 'COLUMN_NAME': 'name', 'CHARACTER_SET_NAME': 'utf8',
             'COLLATION_NAME': 'utf8_general_ci', 'DATA_TYPE': 'varchar',
             'CHARACTER_MAXIMUM_LENGTH': 255, 'COLUMN_TYPE': 'varchar(255)'},
        ],
        'TABLE_CONSTRAINTS': [],
        'KEY_COLUMN_USAGE': [],
        '@@sql_mode': [{'sql_mode': 'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES'}],
    }
    return conn


@pytest.fixture
def sample_compatibility_issue():
    """샘플 CompatibilityIssue"""
    from src.core.migration_constants import IssueType, CompatibilityIssue
    return CompatibilityIssue(
        issue_type=IssueType.CHARSET_ISSUE,
        severity="warning",
        location="test_db.users",
        description="테이블이 utf8mb3 collation 사용 중",
        suggestion="ALTER TABLE ... CONVERT TO CHARACTER SET utf8mb4",
        table_name="users",
        column_name=None,
    )


@pytest.fixture
def sample_issues_list():
    """다양한 이슈 타입의 CompatibilityIssue 리스트"""
    from src.core.migration_constants import IssueType, CompatibilityIssue
    return [
        CompatibilityIssue(
            issue_type=IssueType.CHARSET_ISSUE,
            severity="warning",
            location="test_db.users",
            description="utf8mb3 사용 중",
            suggestion="utf8mb4로 변경",
            table_name="users",
        ),
        CompatibilityIssue(
            issue_type=IssueType.INVALID_DATE,
            severity="error",
            location="test_db.orders.created_at",
            description="0000-00-00 날짜값 발견",
            suggestion="NULL로 변경",
            table_name="orders",
            column_name="created_at",
        ),
        CompatibilityIssue(
            issue_type=IssueType.INT_DISPLAY_WIDTH,
            severity="info",
            location="test_db.products.stock",
            description="INT(11) 표시 너비 사용",
            suggestion="8.4에서 자동 무시됨",
            table_name="products",
            column_name="stock",
        ),
        CompatibilityIssue(
            issue_type=IssueType.DEPRECATED_ENGINE,
            severity="warning",
            location="test_db.logs",
            description="MyISAM 엔진 사용",
            suggestion="InnoDB로 변경",
            table_name="logs",
        ),
        CompatibilityIssue(
            issue_type=IssueType.RESERVED_KEYWORD,
            severity="error",
            location="test_db.rank",
            description="예약어 충돌: RANK",
            suggestion="이름 변경 또는 백틱 사용",
            table_name="rank",
        ),
    ]


@pytest.fixture
def mock_mysql_connector():
    """MagicMock 기반 MySQLConnector (단순 테스트용)"""
    connector = MagicMock()
    connector.execute.return_value = []
    connector.get_tables.return_value = []
    connector.connection = MagicMock()
    return connector


def make_issue(issue_type=None, severity="warning", location="test_db.table",
               description="test issue", suggestion="fix it",
               table_name=None, column_name=None, **kwargs):
    """CompatibilityIssue 빠른 생성 헬퍼"""
    from src.core.migration_constants import IssueType, CompatibilityIssue
    if issue_type is None:
        issue_type = IssueType.CHARSET_ISSUE
    return CompatibilityIssue(
        issue_type=issue_type,
        severity=severity,
        location=location,
        description=description,
        suggestion=suggestion,
        table_name=table_name,
        column_name=column_name,
        **kwargs,
    )


def make_column_info(table_name="users", column_name="col",
                     data_type="varchar", column_type="varchar(255)",
                     charset="utf8mb4", collation="utf8mb4_unicode_ci",
                     is_nullable="YES", **kwargs):
    """INFORMATION_SCHEMA.COLUMNS 행 dict 빠른 생성 헬퍼"""
    info = {
        'TABLE_NAME': table_name,
        'COLUMN_NAME': column_name,
        'DATA_TYPE': data_type,
        'COLUMN_TYPE': column_type,
        'CHARACTER_SET_NAME': charset,
        'COLLATION_NAME': collation,
        'IS_NULLABLE': is_nullable,
        'CHARACTER_MAXIMUM_LENGTH': 255,
    }
    info.update(kwargs)
    return info


@pytest.fixture
def sample_dump_sql():
    """마이그레이션 분석용 샘플 SQL 덤프"""
    return """
CREATE TABLE `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(255) CHARACTER SET utf8 DEFAULT NULL,
  `score` float(10,2) DEFAULT '0.00',
  `birth_year` year(2) DEFAULT NULL,
  `status` enum('active','','inactive') DEFAULT 'active',
  `balance` int(8) UNSIGNED ZEROFILL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=MyISAM DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci;

INSERT INTO `users` VALUES (1,'test','0000-00-00','98','active','00000100');

CREATE TABLE `FTS_config` (
  `key` varchar(50) DEFAULT NULL
) ENGINE=InnoDB;

SELECT SQL_CALC_FOUND_ROWS * FROM users LIMIT 10;
SELECT FOUND_ROWS();

SELECT * FROM users GROUP BY name ASC;

GRANT SUPER ON *.* TO 'admin'@'localhost';
"""
