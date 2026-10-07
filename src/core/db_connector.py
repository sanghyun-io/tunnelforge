"""
MySQL 데이터베이스 연결 클래스
- 메타데이터 캐싱 (TTL 기반) 지원
"""
import time
from typing import Any, Dict, List, Optional, Tuple

from src.core.logger import get_logger
from src.core.db_core_service import (
    RustDbConnection,
    RustDbConnector,
    get_shared_db_core_facade,
    parse_db_version_tuple,
)

logger = get_logger('db_connector')


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

# 전역 메타데이터 캐시 인스턴스 (연결 간 공유)
_global_metadata_cache = MetadataCache(ttl_seconds=300)


class MySQLConnector:
    """MySQL 데이터베이스 연결 및 쿼리 실행 클래스

    메타데이터 캐싱을 지원하여 스키마/테이블 목록 조회 성능 향상
    """

    def __init__(self, host: str, port: int, user: str, password: str,
                 database: str = None, use_cache: bool = True, facade: Optional[Any] = None):
        """
        Args:
            host: MySQL 호스트
            port: MySQL 포트
            user: MySQL 사용자
            password: MySQL 비밀번호
            database: 기본 데이터베이스
            use_cache: 메타데이터 캐싱 사용 여부 (기본 True)
            facade: 주입할 Rust DB core facade (없으면 앱 공유 facade 사용)
        """
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.engine = "mysql"
        self.connection: Optional[RustDbConnection] = None
        self.facade = facade if facade is not None else get_shared_db_core_facade()
        # 연결 프로토콜은 RustDbConnector에 위임 — 커넥터별 전용 서브프로세스를 띄우지 않는다.
        self._delegate = RustDbConnector(
            "mysql", host, port, user, password,
            database=database, facade=self.facade,
        )

        # 캐싱 설정
        self._use_cache = use_cache
        self._cache = _global_metadata_cache if use_cache else None
        self._cache_key_prefix = f"{host}:{port}"

    def connect(self) -> Tuple[bool, str]:
        """데이터베이스 연결"""
        success, msg = self._delegate.connect()
        if success:
            self.connection = self._delegate.connection
            return True, "연결 성공"
        return False, f"MySQL 오류: {msg}"

    def disconnect(self):
        """연결 종료"""
        if self.connection:
            try:
                self.connection.close()
            except Exception:
                pass
            finally:
                self.connection = None
        # delegate가 참조하던 연결 정보도 함께 정리 (재사용 시 잔여 상태 방지)
        self._delegate.connection = None
        self._delegate.connection_id = None

    def is_connected(self) -> bool:
        """연결 상태 확인"""
        if self.connection:
            try:
                self.connection.ping(reconnect=False)
                return True
            except Exception:
                return False
        return False

    def get_schemas(self, use_cache: bool = True) -> List[str]:
        """스키마(데이터베이스) 목록 조회 (시스템 DB 제외)

        조회 자체는 RustDbConnector delegate에 위임하고, TTL 캐시만 이 wrapper가 감싼다.

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
            self._delegate.connection = self.connection
            result = self._delegate.get_schemas()

            # 캐시에 저장
            if use_cache and self._cache:
                self._cache.set(cache_key, result)

            return result
        except Exception as e:
            logger.error(f"스키마 조회 오류: {e}")
            return []

    def schema_exists(self, schema_name: str) -> bool:
        """특정 스키마 존재 여부 확인 (시스템 DB 포함)

        조회는 RustDbConnector delegate에 위임한다.
        """
        if not self.connection:
            return False

        # 빈 스키마명은 이전 MySQL 구현과 동일하게 항상 미존재로 취급한다
        # (Rust delegate는 빈 문자열에 True를 반환할 수 있어 계약을 복원한다).
        if not schema_name:
            return False

        try:
            self._delegate.connection = self.connection
            return self._delegate.schema_exists(schema_name)
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
            # Rust inventory lists base tables; views are exported separately.
            result = self._delegate.get_tables(target_schema or None, use_cache=False)
            if use_cache and self._cache:
                self._cache.set(cache_key, result)
            return result
        except Exception as e:
            logger.error(f"테이블 조회 오류: {e}")
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
            logger.error(f"쿼리 실행 오류: {e}")
            return []

    def get_db_version(self) -> Tuple[int, int, int]:
        """DB 버전 반환 (major, minor, patch). 예: MySQL 8.0.32-ubuntu → (8, 0, 32), 실패 시 (0, 0, 0)"""
        return parse_db_version_tuple(self.get_db_version_string())

    def get_db_version_string(self) -> str:
        """DB 버전 문자열 반환 (원본)"""
        if not self.connection:
            return ""
        try:
            self._delegate.connection = self.connection
            return self._delegate.get_db_version_string()
        except Exception:
            return ""

    def get_column_names(self, table: str, schema: str = None) -> List[str]:
        """테이블 컬럼명 목록 (정의 순서)"""
        if not self.connection:
            return []
        try:
            self._delegate.connection = self.connection
            return self._delegate.get_column_names(table, schema or self.database)
        except Exception as e:
            logger.error(f"컬럼 조회 오류: {e}")
            return []

    def has_named_timezone(self, name: str) -> bool:
        """MySQL 이 지역명 타임존(name)을 아는지"""
        if not self.connection:
            return False
        try:
            self._delegate.connection = self.connection
            return self._delegate.has_named_timezone(name)
        except Exception:
            return False

    def __enter__(self):
        """컨텍스트 매니저 진입"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """컨텍스트 매니저 종료"""
        self.disconnect()
        return False

