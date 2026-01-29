"""
MySQLShellExporter 테스트
"""
import pytest
from unittest.mock import patch, MagicMock


class TestMySQLShellChecker:
    """MySQLShellChecker 클래스 테스트"""

    def test_check_installation_success(self, mock_subprocess_mysqlsh):
        """mysqlsh 설치 확인 성공 테스트"""
        from src.exporters.mysqlsh_exporter import MySQLShellChecker

        installed, msg, version = MySQLShellChecker.check_installation()

        assert installed is True
        assert 'Ver' in msg or version is not None

    def test_check_installation_not_found(self):
        """mysqlsh 미설치 테스트"""
        from src.exporters.mysqlsh_exporter import MySQLShellChecker

        with patch('subprocess.run') as mock:
            mock.side_effect = FileNotFoundError()

            installed, msg, version = MySQLShellChecker.check_installation()

            assert installed is False
            assert '설치' in msg
            assert version is None

    def test_check_installation_timeout(self):
        """mysqlsh 타임아웃 테스트"""
        import subprocess
        from src.exporters.mysqlsh_exporter import MySQLShellChecker

        with patch('subprocess.run') as mock:
            mock.side_effect = subprocess.TimeoutExpired('mysqlsh', 10)

            installed, msg, version = MySQLShellChecker.check_installation()

            assert installed is False
            assert '시간 초과' in msg

    def test_get_install_guide(self):
        """설치 가이드 반환 테스트"""
        from src.exporters.mysqlsh_exporter import MySQLShellChecker

        guide = MySQLShellChecker.get_install_guide()

        assert 'Windows' in guide
        assert 'macOS' in guide
        assert 'Linux' in guide


class TestMySQLShellConfig:
    """MySQLShellConfig 클래스 테스트"""

    def test_get_uri(self):
        """URI 생성 테스트"""
        from src.exporters.mysqlsh_exporter import MySQLShellConfig

        config = MySQLShellConfig(
            host='localhost',
            port=3306,
            user='root',
            password='secret123'
        )

        uri = config.get_uri()

        assert uri == 'root:secret123@localhost:3306'

    def test_get_masked_uri(self):
        """마스킹된 URI 생성 테스트"""
        from src.exporters.mysqlsh_exporter import MySQLShellConfig

        config = MySQLShellConfig(
            host='db.example.com',
            port=3307,
            user='admin',
            password='my_password'
        )

        masked = config.get_masked_uri()

        assert 'admin' in masked
        assert 'my_password' not in masked
        assert '****' in masked
        assert 'db.example.com:3307' in masked


class TestForeignKeyResolver:
    """ForeignKeyResolver 클래스 테스트"""

    @pytest.fixture
    def mock_connector(self):
        """MySQLConnector Mock"""
        connector = MagicMock()
        return connector

    def test_resolve_required_tables_no_deps(self, mock_connector):
        """FK 의존성 없는 경우"""
        from src.exporters.mysqlsh_exporter import ForeignKeyResolver

        # 빈 FK 정보 반환
        mock_connector.execute.return_value = []

        resolver = ForeignKeyResolver(mock_connector)
        selected = ['users', 'products']
        required, added = resolver.resolve_required_tables(selected, 'mydb')

        assert 'users' in required
        assert 'products' in required
        assert len(added) == 0

    def test_resolve_required_tables_with_deps(self, mock_connector):
        """FK 의존성 있는 경우"""
        from src.exporters.mysqlsh_exporter import ForeignKeyResolver

        # FK 정보 반환: orders -> users, order_items -> orders
        mock_connector.execute.return_value = [
            {'TABLE_NAME': 'orders', 'REFERENCED_TABLE_NAME': 'users'},
            {'TABLE_NAME': 'order_items', 'REFERENCED_TABLE_NAME': 'orders'}
        ]

        resolver = ForeignKeyResolver(mock_connector)
        # order_items만 선택하면 orders와 users도 추가되어야 함
        selected = ['order_items']
        required, added = resolver.resolve_required_tables(selected, 'mydb')

        assert 'order_items' in required
        assert 'orders' in required or 'orders' in added
        assert 'users' in required or 'users' in added

    def test_get_all_dependencies(self, mock_connector):
        """전체 FK 의존성 조회"""
        from src.exporters.mysqlsh_exporter import ForeignKeyResolver

        mock_connector.execute.return_value = [
            {'TABLE_NAME': 'posts', 'REFERENCED_TABLE_NAME': 'users'},
            {'TABLE_NAME': 'comments', 'REFERENCED_TABLE_NAME': 'posts'},
            {'TABLE_NAME': 'comments', 'REFERENCED_TABLE_NAME': 'users'}
        ]

        resolver = ForeignKeyResolver(mock_connector)
        deps = resolver.get_all_dependencies('blog')

        assert 'posts' in deps
        assert 'users' in deps['posts']
        assert 'comments' in deps
        assert 'posts' in deps['comments']
        assert 'users' in deps['comments']


class TestMySQLShellExporter:
    """MySQLShellExporter 클래스 테스트"""

    def test_exporter_initialization(self):
        """Exporter 초기화 테스트"""
        from src.exporters.mysqlsh_exporter import MySQLShellExporter, MySQLShellConfig

        config = MySQLShellConfig('localhost', 3306, 'root', 'password')
        exporter = MySQLShellExporter(config)

        assert exporter.config == config
        assert exporter._connector is None


class TestMySQLShellImporter:
    """MySQLShellImporter 클래스 테스트"""

    def test_importer_initialization(self):
        """Importer 초기화 테스트"""
        from src.exporters.mysqlsh_exporter import MySQLShellImporter, MySQLShellConfig

        config = MySQLShellConfig('localhost', 3306, 'root', 'password')
        importer = MySQLShellImporter(config)

        assert importer.config == config


class TestConvenienceFunctions:
    """편의 함수 테스트"""

    def test_check_mysqlsh_function(self, mock_subprocess_mysqlsh):
        """check_mysqlsh 함수 테스트"""
        from src.exporters.mysqlsh_exporter import check_mysqlsh

        installed, msg = check_mysqlsh()

        assert isinstance(installed, bool)
        assert isinstance(msg, str)


class TestTableProgressTracker:
    """TableProgressTracker 클래스 테스트"""

    def test_tracker_initialization_with_metadata(self):
        """메타데이터로 초기화 테스트"""
        from src.exporters.mysqlsh_exporter import TableProgressTracker

        metadata = {
            'chunk_counts': {'users': 10, 'orders': 50},
            'table_sizes': {'users': 1024000, 'orders': 5120000},
            'total_bytes': 6144000
        }

        tracker = TableProgressTracker(metadata)

        assert tracker.chunk_counts == metadata['chunk_counts']
        assert tracker.table_sizes == metadata['table_sizes']
        assert tracker.total_bytes == 6144000

    def test_tracker_initialization_without_metadata(self):
        """메타데이터 없이 초기화 테스트"""
        from src.exporters.mysqlsh_exporter import TableProgressTracker

        tracker = TableProgressTracker(None)

        assert tracker.chunk_counts == {}
        assert tracker.table_sizes == {}
        assert tracker.total_bytes == 0

    def test_format_size(self):
        """크기 포맷팅 테스트"""
        from src.exporters.mysqlsh_exporter import TableProgressTracker

        tracker = TableProgressTracker(None)

        assert tracker.format_size(500) == '500 B'
        assert 'KB' in tracker.format_size(2048)
        assert 'MB' in tracker.format_size(5 * 1024 * 1024)
        assert 'GB' in tracker.format_size(2 * 1024 * 1024 * 1024)

    def test_get_table_info(self):
        """테이블 정보 조회 테스트"""
        from src.exporters.mysqlsh_exporter import TableProgressTracker

        metadata = {
            'chunk_counts': {'users': 5},
            'table_sizes': {'users': 1024000},
            'total_bytes': 1024000
        }

        tracker = TableProgressTracker(metadata)
        size, chunks = tracker.get_table_info('users')

        assert size == 1024000
        assert chunks == 5

    def test_get_table_info_not_found(self):
        """존재하지 않는 테이블 정보 조회"""
        from src.exporters.mysqlsh_exporter import TableProgressTracker

        tracker = TableProgressTracker(None)
        size, chunks = tracker.get_table_info('non_existent')

        assert size == 0
        assert chunks == 1  # 기본값
