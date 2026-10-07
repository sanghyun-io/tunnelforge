"""
MySQLConnector 및 MetadataCache 단위 테스트
"""
import time
import pytest
from unittest.mock import MagicMock, patch, call


class FakeFacade:
    def __init__(self, fail: Exception = None):
        self.fail = fail
        self.closed = []

    def open_connection(self, endpoint):
        if self.fail:
            raise self.fail
        self.endpoint = endpoint
        return "conn-1"

    def close_connection(self, connection_id):
        self.closed.append(connection_id)
        return True

    def execute_on_connection(self, connection_id, sql):
        return []

    catalog_values = {}

    def catalog(self, connection_id, kind, **args):
        self.catalog_calls = getattr(self, "catalog_calls", []) + [(kind, args)]
        return list(self.catalog_values.get(kind, []))


# =====================================================================
# MetadataCache 테스트
# =====================================================================

class TestMetadataCache:
    """MetadataCache 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from src.core.db_connector import MetadataCache
        self.cache = MetadataCache(ttl_seconds=5)

    def test_set_and_get_value(self):
        """값 저장 및 조회 확인"""
        self.cache.set('key1', ['table1', 'table2'])
        result = self.cache.get('key1')
        assert result == ['table1', 'table2']

    def test_get_nonexistent_key_returns_none(self):
        """존재하지 않는 키 조회 시 None 반환"""
        result = self.cache.get('nonexistent')
        assert result is None

    def test_expired_entry_returns_none(self):
        """만료된 캐시 항목 조회 시 None 반환"""
        from src.core.db_connector import MetadataCache
        short_cache = MetadataCache(ttl_seconds=1)
        short_cache.set('expiring_key', 'value')

        # 캐시 항목의 시간을 과거로 조작
        key = 'expiring_key'
        value, _ = short_cache._cache[key]
        short_cache._cache[key] = (value, time.time() - 2)

        result = short_cache.get('expiring_key')
        assert result is None

    def test_expired_entry_is_deleted(self):
        """만료된 캐시 항목이 삭제됨을 확인"""
        key = 'to_expire'
        self.cache.set(key, 'data')
        value, _ = self.cache._cache[key]
        self.cache._cache[key] = (value, time.time() - 100)

        self.cache.get(key)
        assert key not in self.cache._cache

    def test_invalidate_all(self):
        """전체 캐시 무효화 확인"""
        self.cache.set('a', 1)
        self.cache.set('b', 2)
        self.cache.set('c', 3)

        self.cache.invalidate()
        assert len(self.cache._cache) == 0

    def test_invalidate_with_pattern(self):
        """패턴으로 특정 항목만 무효화"""
        self.cache.set('prefix:key1', 1)
        self.cache.set('prefix:key2', 2)
        self.cache.set('other:key3', 3)

        self.cache.invalidate('prefix')

        assert self.cache.get('prefix:key1') is None
        assert self.cache.get('prefix:key2') is None
        assert self.cache.get('other:key3') == 3

    def test_overwrite_existing_key(self):
        """기존 키 덮어쓰기 확인"""
        self.cache.set('key', 'old_value')
        self.cache.set('key', 'new_value')
        assert self.cache.get('key') == 'new_value'


# =====================================================================
# MySQLConnector 테스트
# =====================================================================

class TestMySQLConnector:
    """MySQLConnector 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from src.core.db_connector import MySQLConnector
        self.connector = MySQLConnector(
            host='127.0.0.1',
            port=3306,
            user='test_user',
            password='test_pass',
            database='test_db',
            use_cache=True
        )

    def test_initial_state_not_connected(self):
        """초기 상태 미연결 확인"""
        assert self.connector.connection is None
        assert self.connector.is_connected() is False

    def test_connect_success(self):
        """연결 성공 테스트"""
        self.connector._delegate.facade = FakeFacade()

        success, msg = self.connector.connect()

        assert success is True
        assert '성공' in msg
        assert self.connector.connection is not None

    def test_connect_mysql_error(self):
        """MySQL 에러 발생 시 실패 반환"""
        self.connector._delegate.facade = FakeFacade(Exception("Access denied"))

        success, msg = self.connector.connect()

        assert success is False
        assert 'Access denied' in msg or 'MySQL' in msg

    def test_connect_general_error(self):
        """일반 예외 발생 시 실패 반환"""
        self.connector._delegate.facade = FakeFacade(Exception("Connection refused"))

        success, msg = self.connector.connect()

        assert success is False
        assert '오류' in msg

    def test_disconnect_closes_connection(self):
        """연결 종료 확인"""
        mock_conn = MagicMock()
        self.connector.connection = mock_conn

        self.connector.disconnect()

        mock_conn.close.assert_called_once()
        assert self.connector.connection is None

    def test_disconnect_when_not_connected(self):
        """미연결 상태 disconnect 호출 시 예외 없음"""
        self.connector.connection = None
        # 예외 없이 통과해야 함
        self.connector.disconnect()

    def test_is_connected_true(self):
        """연결 상태 확인 - 연결됨"""
        mock_conn = MagicMock()
        mock_conn.ping.return_value = None
        self.connector.connection = mock_conn

        assert self.connector.is_connected() is True

    def test_is_connected_ping_fails(self):
        """ping 실패 시 미연결 상태 반환"""
        mock_conn = MagicMock()
        mock_conn.ping.side_effect = Exception("Lost connection")
        self.connector.connection = mock_conn

        assert self.connector.is_connected() is False

    def test_get_schemas_returns_list(self):
        """스키마 목록은 Rust catalog.query(schemas) 결과 그대로 (시스템 스키마 제외는 Rust 가 한다)"""
        facade = FakeFacade()
        facade.catalog_values = {'schemas': ['myapp', 'testdb']}
        self.connector._delegate.facade = facade
        self.connector.connection = MagicMock()

        assert self.connector.get_schemas(use_cache=False) == ['myapp', 'testdb']

    def test_get_schemas_uses_cache(self):
        """스키마 조회 시 캐시 활용 확인"""
        facade = FakeFacade()
        facade.catalog_values = {'schemas': ['cached_db']}
        self.connector._delegate.facade = facade
        self.connector.connection = MagicMock()

        schemas1 = self.connector.get_schemas(use_cache=True)
        schemas2 = self.connector.get_schemas(use_cache=True)

        # Rust 조회는 1번만 일어나야 함
        assert len(facade.catalog_calls) == 1
        assert schemas1 == schemas2 == ['cached_db']

    def test_get_schemas_no_connection(self):
        """연결 없을 때 빈 리스트 반환"""
        self.connector.connection = None
        result = self.connector.get_schemas()
        assert result == []

    def test_get_tables_returns_list(self):
        """테이블 목록 조회 확인"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            {'Tables_in_test_db': 'users'},
            {'Tables_in_test_db': 'orders'},
        ]
        mock_conn.cursor.return_value = mock_cursor
        self.connector.connection = mock_conn
        self.connector._delegate.get_tables = MagicMock(return_value=['users', 'orders'])

        tables = self.connector.get_tables(use_cache=False)

        assert 'users' in tables
        assert 'orders' in tables

    def test_get_tables_uses_rust_base_table_inventory_and_preserves_cache(self):
        self.connector.connection = MagicMock()
        self.connector.connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            {"Tables_in_app": "users"}, {"Tables_in_app": "users_view"},
        ]
        self.connector._delegate.get_tables = MagicMock(return_value=["users"])
        self.connector._cache = MagicMock()
        self.connector._cache.get.side_effect = [None, ["users"]]
        assert self.connector.get_tables("app") == ["users"]
        assert self.connector.get_tables("app") == ["users"]
        self.connector._delegate.get_tables.assert_called_once_with("app", use_cache=False)
        self.connector.connection.cursor.assert_not_called()
        self.connector._cache.set.assert_called_once()

    def test_get_tables_no_connection(self):
        """연결 없을 때 빈 리스트 반환"""
        self.connector.connection = None
        result = self.connector.get_tables()
        assert result == []

    def test_execute_returns_results(self):
        """쿼리 실행 및 결과 반환 확인"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            {'id': 1, 'name': 'Alice'},
            {'id': 2, 'name': 'Bob'},
        ]
        mock_conn.cursor.return_value = mock_cursor
        self.connector.connection = mock_conn

        result = self.connector.execute("SELECT * FROM users")

        assert len(result) == 2
        assert result[0]['name'] == 'Alice'

    def test_execute_no_connection(self):
        """연결 없을 때 빈 리스트 반환"""
        self.connector.connection = None
        result = self.connector.execute("SELECT 1")
        assert result == []

    def test_execute_exception_returns_empty(self):
        """쿼리 실행 예외 시 빈 리스트 반환"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.execute.side_effect = Exception("SQL error")
        mock_conn.cursor.return_value = mock_cursor
        self.connector.connection = mock_conn

        result = self.connector.execute("INVALID SQL")
        assert result == []

    def test_context_manager_connects_and_disconnects(self):
        """컨텍스트 매니저 연결/해제 확인"""
        self.connector._delegate.facade = FakeFacade()

        with self.connector:
            assert self.connector.connection is not None

        assert self.connector.connection is None

    def test_get_db_version_returns_tuple(self):
        """DB 버전 튜플 반환 확인"""
        facade = FakeFacade()
        facade.catalog_values = {'version': ['8.0.32-ubuntu']}
        self.connector._delegate.facade = facade
        self.connector.connection = MagicMock()

        assert self.connector.get_db_version() == (8, 0, 32)

    def test_get_db_version_no_connection(self):
        """연결 없을 때 (0,0,0) 반환"""
        self.connector.connection = None
        version = self.connector.get_db_version()
        assert version == (0, 0, 0)

    def test_use_cache_false_skips_caching(self):
        """use_cache=False 시 캐시 사용 안 함"""
        from src.core.db_connector import MySQLConnector
        connector = MySQLConnector(
            host='127.0.0.1', port=3306,
            user='user', password='pass',
            use_cache=False
        )
        assert connector._cache is None

    def test_schema_exists_true(self):
        """스키마 존재 확인"""
        facade = FakeFacade()
        facade.catalog_values = {'schema_exists': ['mydb']}
        self.connector._delegate.facade = facade
        self.connector.connection = MagicMock()

        assert self.connector.schema_exists('mydb') is True
        assert facade.catalog_calls == [('schema_exists', {'name': 'mydb'})]

    def test_schema_exists_false(self):
        """스키마 미존재 확인"""
        facade = FakeFacade()
        facade.catalog_values = {}
        self.connector._delegate.facade = facade
        self.connector.connection = MagicMock()

        assert self.connector.schema_exists('nonexistent') is False

# =====================================================================
# Facade 주입/공유 테스트 (WP-2.10: connector-facade 통합)
# =====================================================================

class TestMySQLConnectorFacadeUnification:
    """MySQLConnector가 커넥터별 전용 facade를 만들지 않고 공유 facade를 사용하는지 검증"""

    def test_mysql_connector_uses_shared_facade_by_default(self):
        """facade 미지정 시 앱 공유 facade를 사용하고, delegate에 그대로 주입됨을 확인"""
        from src.core import db_connector

        sentinel = object()
        with patch.object(db_connector, 'get_shared_db_core_facade', return_value=sentinel) as mock_get_shared, \
                patch.object(db_connector, 'RustDbConnector') as mock_rust_connector:
            connector = db_connector.MySQLConnector(
                host='127.0.0.1', port=3306, user='u', password='p', database='db'
            )

            mock_get_shared.assert_called_once()
            assert connector.facade is sentinel
            _, kwargs = mock_rust_connector.call_args
            assert kwargs['facade'] is sentinel

    def test_mysql_connector_uses_injected_facade_without_shared_lookup(self):
        """facade를 주입하면 공유 facade 조회를 건너뛰고 delegate에 주입된 facade를 그대로 사용"""
        from src.core import db_connector

        sentinel = object()
        with patch.object(db_connector, 'get_shared_db_core_facade') as mock_get_shared, \
                patch.object(db_connector, 'RustDbConnector') as mock_rust_connector:
            connector = db_connector.MySQLConnector(
                host='127.0.0.1', port=3306, user='u', password='p', database='db',
                facade=sentinel,
            )

            mock_get_shared.assert_not_called()
            assert connector.facade is sentinel
            _, kwargs = mock_rust_connector.call_args
            assert kwargs['facade'] is sentinel
