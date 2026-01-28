"""
MySQL 데이터베이스 연결 클래스
- 메타데이터 캐싱 (TTL 기반) 지원
"""
import time
import pymysql
from typing import List, Dict, Any, Optional, Tuple


class MetadataCache:
    """스키마/테이블 메타데이터 캐시 (TTL 기반)

    동일 메타데이터 반복 조회 시 DB 쿼리를 제거하여 성능 향상
    """

    def __init__(self, ttl_seconds: int = 300):
        """
        Args:
            ttl_seconds: 캐시 유효 시간 (기본 5분)
        """
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        """캐시에서 값 조회

        Args:
            key: 캐시 키

        Returns:
            캐시된 값 또는 None (만료/미존재 시)
        """
        if key in self._cache:
            value, timestamp = self._cache[key]
            if time.time() - timestamp < self._ttl:
                return value
            # 만료된 항목 삭제
            del self._cache[key]
        return None

    def set(self, key: str, value: Any):
        """캐시에 값 저장

        Args:
            key: 캐시 키
            value: 저장할 값
        """
        self._cache[key] = (value, time.time())

    def invalidate(self, pattern: str = None):
        """캐시 무효화

        Args:
            pattern: 무효화할 키 패턴 (None이면 전체 삭제)
        """
        if pattern is None:
            self._cache.clear()
        else:
            keys_to_delete = [k for k in self._cache if pattern in k]
            for k in keys_to_delete:
                del self._cache[k]

    def get_stats(self) -> Dict[str, int]:
        """캐시 통계 반환"""
        now = time.time()
        valid_count = sum(1 for _, (_, ts) in self._cache.items() if now - ts < self._ttl)
        return {
            'total_entries': len(self._cache),
            'valid_entries': valid_count,
            'ttl_seconds': self._ttl
        }


# 전역 메타데이터 캐시 인스턴스 (연결 간 공유)
_global_metadata_cache = MetadataCache(ttl_seconds=300)


class MySQLConnector:
    """MySQL 데이터베이스 연결 및 쿼리 실행 클래스

    메타데이터 캐싱을 지원하여 스키마/테이블 목록 조회 성능 향상
    """

    def __init__(self, host: str, port: int, user: str, password: str,
                 database: str = None, use_cache: bool = True):
        """
        Args:
            host: MySQL 호스트
            port: MySQL 포트
            user: MySQL 사용자
            password: MySQL 비밀번호
            database: 기본 데이터베이스
            use_cache: 메타데이터 캐싱 사용 여부 (기본 True)
        """
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.connection: Optional[pymysql.Connection] = None

        # 캐싱 설정
        self._use_cache = use_cache
        self._cache = _global_metadata_cache if use_cache else None
        self._cache_key_prefix = f"{host}:{port}"

    def connect(self) -> Tuple[bool, str]:
        """데이터베이스 연결"""
        try:
            self.connection = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=10
            )
            return True, "연결 성공"
        except pymysql.Error as e:
            error_code = e.args[0] if e.args else 0
            error_msg = e.args[1] if len(e.args) > 1 else str(e)
            return False, f"MySQL 오류 ({error_code}): {error_msg}"
        except Exception as e:
            return False, f"연결 오류: {str(e)}"

    def disconnect(self):
        """연결 종료"""
        if self.connection:
            try:
                self.connection.close()
            except Exception:
                pass
            finally:
                self.connection = None

    def is_connected(self) -> bool:
        """연결 상태 확인"""
        if self.connection:
            try:
                self.connection.ping(reconnect=False)
                return True
            except Exception:
                return False
        return False

    def use_database(self, database: str) -> Tuple[bool, str]:
        """데이터베이스 선택"""
        try:
            self.connection.select_db(database)
            self.database = database
            return True, f"데이터베이스 '{database}' 선택됨"
        except Exception as e:
            return False, str(e)

    def get_schemas(self, use_cache: bool = True) -> List[str]:
        """스키마(데이터베이스) 목록 조회 (시스템 DB 제외)

        Args:
            use_cache: 캐시 사용 여부 (기본 True)

        Returns:
            스키마 목록
        """
        if not self.connection:
            return []

        # 캐시 확인
        cache_key = f"{self._cache_key_prefix}:schemas"
        if use_cache and self._cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SHOW DATABASES")
                exclude = {'information_schema', 'mysql', 'performance_schema', 'sys'}
                result = [row['Database'] for row in cursor.fetchall()
                          if row['Database'] not in exclude]

                # 캐시에 저장
                if use_cache and self._cache:
                    self._cache.set(cache_key, result)

                return result
        except Exception as e:
            print(f"스키마 조회 오류: {e}")
            return []

    def schema_exists(self, schema_name: str) -> bool:
        """특정 스키마 존재 여부 확인 (시스템 DB 포함)"""
        if not self.connection:
            return False

        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SHOW DATABASES LIKE %s", (schema_name,))
                return cursor.fetchone() is not None
        except Exception:
            return False

    def get_tables(self, schema: str = None, use_cache: bool = True) -> List[str]:
        """테이블 목록 조회

        Args:
            schema: 스키마명 (None이면 현재 데이터베이스)
            use_cache: 캐시 사용 여부 (기본 True)

        Returns:
            테이블 목록
        """
        if not self.connection:
            return []

        # 캐시 확인
        target_schema = schema or self.database or ''
        cache_key = f"{self._cache_key_prefix}:tables:{target_schema}"
        if use_cache and self._cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            with self.connection.cursor() as cursor:
                if schema:
                    cursor.execute(f"SHOW TABLES FROM `{schema}`")
                else:
                    cursor.execute("SHOW TABLES")

                result = [list(row.values())[0] for row in cursor.fetchall()]

                # 캐시에 저장
                if use_cache and self._cache:
                    self._cache.set(cache_key, result)

                return result
        except Exception as e:
            print(f"테이블 조회 오류: {e}")
            return []

    def execute(self, query: str, params: tuple = None) -> List[Dict[str, Any]]:
        """쿼리 실행 및 결과 반환"""
        if not self.connection:
            return []

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()
        except Exception as e:
            print(f"쿼리 실행 오류: {e}")
            return []

    def execute_many(self, query: str, data: List[tuple]) -> int:
        """배치 쿼리 실행"""
        if not self.connection:
            return 0

        try:
            with self.connection.cursor() as cursor:
                cursor.executemany(query, data)
                self.connection.commit()
                return cursor.rowcount
        except Exception as e:
            print(f"배치 쿼리 오류: {e}")
            return 0

    def get_table_columns(self, table: str, schema: str = None) -> List[Dict[str, Any]]:
        """테이블 컬럼 정보 조회"""
        target_schema = schema or self.database
        if not target_schema:
            return []

        query = """
        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, EXTRA
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """
        return self.execute(query, (target_schema, table))

    def get_create_table_statement(self, table: str, schema: str = None) -> str:
        """CREATE TABLE 문 조회"""
        if not self.connection:
            return ""

        try:
            # 스키마가 지정되면 해당 스키마로 전환
            if schema and schema != self.database:
                self.connection.select_db(schema)

            with self.connection.cursor() as cursor:
                cursor.execute(f"SHOW CREATE TABLE `{table}`")
                result = cursor.fetchone()
                if result:
                    return result.get('Create Table', '')
            return ""
        except Exception as e:
            print(f"CREATE TABLE 조회 오류: {e}")
            return ""

    def get_table_data(self, table: str, schema: str = None, limit: int = None) -> List[Dict[str, Any]]:
        """테이블 데이터 조회"""
        if schema and schema != self.database:
            self.connection.select_db(schema)

        query = f"SELECT * FROM `{table}`"
        if limit:
            query += f" LIMIT {limit}"

        return self.execute(query)

    def get_row_count(self, table: str, schema: str = None) -> int:
        """테이블 행 수 조회"""
        if schema and schema != self.database:
            self.connection.select_db(schema)

        result = self.execute(f"SELECT COUNT(*) as cnt FROM `{table}`")
        if result:
            return result[0].get('cnt', 0)
        return 0

    def invalidate_cache(self, schema: str = None):
        """메타데이터 캐시 무효화

        DDL 작업 (CREATE/DROP/ALTER TABLE 등) 후 호출하여 캐시 갱신

        Args:
            schema: 특정 스키마 캐시만 무효화 (None이면 전체)
        """
        if not self._cache:
            return

        if schema:
            # 특정 스키마의 테이블 캐시만 무효화
            self._cache.invalidate(f"{self._cache_key_prefix}:tables:{schema}")
        else:
            # 해당 연결의 모든 캐시 무효화
            self._cache.invalidate(self._cache_key_prefix)

    def __enter__(self):
        """컨텍스트 매니저 진입"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """컨텍스트 매니저 종료"""
        self.disconnect()
        return False


def test_mysql_connection(host: str, port: int, user: str, password: str) -> Tuple[bool, str]:
    """MySQL 연결 테스트 (유틸리티 함수)"""
    connector = MySQLConnector(host, port, user, password)
    success, msg = connector.connect()
    if success:
        connector.disconnect()
    return success, msg
