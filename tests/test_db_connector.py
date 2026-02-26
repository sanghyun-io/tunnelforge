"""
MySQLConnector 및 MetadataCache 단위 테스트
"""
import time
import pytest
from unittest.mock import MagicMock, patch, call


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

    def test_get_stats(self):
        """캐시 통계 반환 확인"""
        self.cache.set('valid1', 'a')
        self.cache.set('valid2', 'b')

        # 하나를 만료
        key = 'valid2'
        value, _ = self.cache._cache[key]
        self.cache._cache[key] = (value, time.time() - 100)

        stats = self.cache.get_stats()
        assert stats['total_entries'] == 2
        assert stats['valid_entries'] == 1
        assert stats['ttl_seconds'] == 5

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
        with patch('pymysql.connect') as mock_connect:
            mock_conn = MagicMock()
            mock_connect.return_value = mock_conn

            success, msg = self.connector.connect()

            assert success is True
            assert '성공' in msg
            assert self.connector.connection is mock_conn

    def test_connect_mysql_error(self):
        """MySQL 에러 발생 시 실패 반환"""
        import pymysql
        with patch('pymysql.connect') as mock_connect:
            mock_connect.side_effect = pymysql.Error(1045, "Access denied")

            success, msg = self.connector.connect()

            assert success is False
            assert '1045' in msg or 'MySQL' in msg

    def test_connect_general_error(self):
        """일반 예외 발생 시 실패 반환"""
        with patch('pymysql.connect') as mock_connect:
            mock_connect.side_effect = Exception("Connection refused")

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
        """스키마 목록 조회 확인 (시스템 DB 제외)"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            {'Database': 'myapp'},
            {'Database': 'information_schema'},
            {'Database': 'mysql'},
            {'Database': 'performance_schema'},
            {'Database': 'sys'},
            {'Database': 'testdb'},
        ]
        mock_conn.cursor.return_value = mock_cursor
        self.connector.connection = mock_conn

        schemas = self.connector.get_schemas(use_cache=False)

        assert 'myapp' in schemas
        assert 'testdb' in schemas
        assert 'information_schema' not in schemas
        assert 'mysql' not in schemas
        assert 'performance_schema' not in schemas
        assert 'sys' not in schemas

    def test_get_schemas_uses_cache(self):
        """스키마 조회 시 캐시 활용 확인"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [{'Database': 'cached_db'}]
        mock_conn.cursor.return_value = mock_cursor
        self.connector.connection = mock_conn

        # 첫 번째 조회 (DB 접근)
        schemas1 = self.connector.get_schemas(use_cache=True)
        # 두 번째 조회 (캐시 사용)
        schemas2 = self.connector.get_schemas(use_cache=True)

        # cursor는 1번만 호출되어야 함
        assert mock_conn.cursor.call_count == 1
        assert schemas1 == schemas2

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

        tables = self.connector.get_tables(use_cache=False)

        assert 'users' in tables
        assert 'orders' in tables

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

    def test_invalidate_cache_all(self):
        """전체 캐시 무효화 확인"""
        # 캐시에 항목 추가
        self.connector._cache.set(f"{self.connector._cache_key_prefix}:schemas", ['db1'])
        self.connector._cache.set(f"{self.connector._cache_key_prefix}:tables:db1", ['t1'])

        self.connector.invalidate_cache()

        assert self.connector._cache.get(f"{self.connector._cache_key_prefix}:schemas") is None

    def test_invalidate_cache_specific_schema(self):
        """특정 스키마 캐시만 무효화 확인"""
        # 두 스키마의 테이블 캐시 설정
        prefix = self.connector._cache_key_prefix
        self.connector._cache.set(f"{prefix}:tables:schema_a", ['t1'])
        self.connector._cache.set(f"{prefix}:tables:schema_b", ['t2'])

        self.connector.invalidate_cache(schema='schema_a')

        # schema_a 캐시는 제거됨
        assert self.connector._cache.get(f"{prefix}:tables:schema_a") is None
        # schema_b 캐시는 유지됨
        assert self.connector._cache.get(f"{prefix}:tables:schema_b") == ['t2']

    def test_context_manager_connects_and_disconnects(self):
        """컨텍스트 매니저 연결/해제 확인"""
        with patch('pymysql.connect') as mock_connect:
            mock_conn = MagicMock()
            mock_connect.return_value = mock_conn

            with self.connector:
                assert self.connector.connection is not None

            assert self.connector.connection is None

    def test_get_db_version_returns_tuple(self):
        """DB 버전 튜플 반환 확인"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = {'VERSION()': '8.0.32-ubuntu'}
        mock_conn.cursor.return_value = mock_cursor
        self.connector.connection = mock_conn

        version = self.connector.get_db_version()
        assert version == (8, 0, 32)

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
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = {'Database': 'mydb'}
        mock_conn.cursor.return_value = mock_cursor
        self.connector.connection = mock_conn

        result = self.connector.schema_exists('mydb')
        assert result is True

    def test_schema_exists_false(self):
        """스키마 미존재 확인"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value = mock_cursor
        self.connector.connection = mock_conn

        result = self.connector.schema_exists('nonexistent')
        assert result is False

    def test_table_exists_true(self):
        """테이블 존재 확인"""
        # get_tables를 mock하여 테스트
        self.connector.get_tables = MagicMock(return_value=['users', 'orders'])
        assert self.connector.table_exists('users') is True

    def test_table_exists_false(self):
        """테이블 미존재 확인"""
        self.connector.get_tables = MagicMock(return_value=['users', 'orders'])
        assert self.connector.table_exists('products') is False
