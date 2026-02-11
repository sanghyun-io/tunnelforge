"""
DB 연결 풀 관리

연결 재사용으로 성능 향상 및 리소스 효율화
"""
import time
import threading
from queue import Queue, Empty
from threading import Lock, Event
from typing import Dict, Optional
import pymysql
from pymysql.connections import Connection

from src.core.logger import get_logger

logger = get_logger(__name__)


class ConnectionPool:
    """DB 연결 풀

    연결 재사용, 유효성 검사, 유휴 연결 정리 기능 제공
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str = None,
        max_connections: int = 5,
        min_connections: int = 1,
        idle_timeout: int = 300,
        connect_timeout: int = 10
    ):
        """연결 풀 초기화

        Args:
            host: DB 호스트
            port: DB 포트
            user: DB 사용자
            password: DB 비밀번호
            database: 데이터베이스명 (옵션)
            max_connections: 최대 연결 수 (기본: 5)
            min_connections: 최소 유지 연결 수 (기본: 1)
            idle_timeout: 유휴 연결 타임아웃 초 (기본: 300)
            connect_timeout: 연결 타임아웃 초 (기본: 10)
        """
        self._pool: Queue = Queue(maxsize=max_connections)
        self._max_connections = max_connections
        self._min_connections = min_connections
        self._idle_timeout = idle_timeout
        self._created_count = 0
        self._in_use_count = 0
        self._lock = Lock()
        self._connection_times: Dict[int, float] = {}  # conn_id -> last_used

        # 연결 정보 저장
        self._conn_params = {
            'host': host,
            'port': port,
            'user': user,
            'password': password,
            'database': database,
            'charset': 'utf8mb4',
            'connect_timeout': connect_timeout,
            'cursorclass': pymysql.cursors.DictCursor,
        }

        # 풀 키 (레지스트리용)
        self._pool_key = f"{user}@{host}:{port}/{database or 'default'}"

        # 정리 스레드
        self._stop_cleaner = Event()
        self._cleaner_thread = None

        logger.info(f"연결 풀 생성: {self._pool_key} (max={max_connections})")

    def _create_connection(self) -> Connection:
        """새 연결 생성"""
        try:
            conn = pymysql.connect(**self._conn_params)
            conn_id = id(conn)
            self._connection_times[conn_id] = time.time()
            logger.debug(f"새 연결 생성: {conn_id}")
            return conn
        except Exception as e:
            logger.error(f"연결 생성 실패: {e}")
            raise

    def _validate_connection(self, conn: Connection) -> bool:
        """연결 유효성 확인 (ping)"""
        try:
            conn.ping(reconnect=False)
            return True
        except Exception:
            return False

    def _is_idle_timeout(self, conn: Connection) -> bool:
        """유휴 타임아웃 초과 여부"""
        conn_id = id(conn)
        last_used = self._connection_times.get(conn_id, 0)
        return (time.time() - last_used) > self._idle_timeout

    def get_connection(self, timeout: float = 5.0) -> Connection:
        """풀에서 연결 획득

        Args:
            timeout: 대기 타임아웃 (초)

        Returns:
            DB 연결 객체

        Raises:
            Exception: 연결 풀 고갈 시
        """
        # 1. 풀에서 즉시 시도
        try:
            conn = self._pool.get_nowait()
            if self._validate_connection(conn) and not self._is_idle_timeout(conn):
                conn_id = id(conn)
                self._connection_times[conn_id] = time.time()
                with self._lock:
                    self._in_use_count += 1
                logger.debug(f"풀에서 연결 획득: {conn_id}")
                return conn
            else:
                # 무효 또는 타임아웃 연결 폐기
                self._close_connection(conn)
        except Empty:
            pass

        # 2. 새 연결 생성 (한도 내)
        with self._lock:
            if self._created_count < self._max_connections:
                conn = self._create_connection()
                self._created_count += 1
                self._in_use_count += 1
                return conn

        # 3. 대기 후 재시도
        try:
            conn = self._pool.get(timeout=timeout)
            if self._validate_connection(conn):
                conn_id = id(conn)
                self._connection_times[conn_id] = time.time()
                with self._lock:
                    self._in_use_count += 1
                logger.debug(f"대기 후 연결 획득: {conn_id}")
                return conn
            else:
                self._close_connection(conn)
                # 재귀 호출로 재시도
                return self.get_connection(timeout=timeout / 2)
        except Empty:
            logger.error(f"연결 풀 고갈: {self._pool_key}")
            raise Exception(f"연결 풀 고갈 (max={self._max_connections})")

    def return_connection(self, conn: Connection):
        """연결을 풀에 반환

        Args:
            conn: 반환할 연결 객체
        """
        if conn is None:
            return

        conn_id = id(conn)
        with self._lock:
            self._in_use_count = max(0, self._in_use_count - 1)

        # 연결 유효성 검사
        if not self._validate_connection(conn):
            logger.debug(f"무효 연결 폐기: {conn_id}")
            self._close_connection(conn)
            return

        # 암묵적 트랜잭션 정리 (REPEATABLE READ 스냅샷 해제)
        try:
            conn.rollback()
        except Exception:
            pass

        # 풀에 반환
        try:
            self._connection_times[conn_id] = time.time()
            self._pool.put_nowait(conn)
            logger.debug(f"연결 반환: {conn_id}")
        except Exception:
            # 풀이 가득 찬 경우 (드문 상황)
            self._close_connection(conn)

    def _close_connection(self, conn: Connection):
        """연결 종료 및 정리"""
        try:
            conn_id = id(conn)
            conn.close()
            with self._lock:
                self._created_count = max(0, self._created_count - 1)
                self._connection_times.pop(conn_id, None)
            logger.debug(f"연결 종료: {conn_id}")
        except Exception as e:
            logger.warning(f"연결 종료 실패: {e}")

    def close_all(self):
        """모든 연결 종료"""
        # 정리 스레드 중지
        self._stop_cleaner.set()
        if self._cleaner_thread and self._cleaner_thread.is_alive():
            self._cleaner_thread.join(timeout=2)

        # 풀의 모든 연결 종료
        closed = 0
        while True:
            try:
                conn = self._pool.get_nowait()
                self._close_connection(conn)
                closed += 1
            except Empty:
                break

        logger.info(f"연결 풀 종료: {self._pool_key} ({closed}개 연결 종료)")

    def start_cleaner(self, interval: int = 60):
        """유휴 연결 정리 스레드 시작

        Args:
            interval: 정리 주기 (초)
        """
        self._stop_cleaner.clear()
        self._cleaner_thread = threading.Thread(
            target=self._cleaner_loop,
            args=(interval,),
            daemon=True,
            name=f"PoolCleaner-{self._pool_key[:20]}"
        )
        self._cleaner_thread.start()
        logger.debug(f"정리 스레드 시작: {interval}초 주기")

    def _cleaner_loop(self, interval: int):
        """정리 루프"""
        while not self._stop_cleaner.wait(interval):
            self._cleanup_idle_connections()

    def _cleanup_idle_connections(self):
        """유휴 타임아웃 초과 연결 정리"""
        cleaned = 0
        remaining = []

        # 풀의 연결 검사
        while True:
            try:
                conn = self._pool.get_nowait()
            except Empty:
                break

            # 최소 연결 수 유지
            current_available = len(remaining) + self._pool.qsize()
            if current_available < self._min_connections:
                remaining.append(conn)
                continue

            # 유휴 타임아웃 검사
            if self._is_idle_timeout(conn) or not self._validate_connection(conn):
                self._close_connection(conn)
                cleaned += 1
            else:
                remaining.append(conn)

        # 남은 연결 다시 풀에 넣기
        for conn in remaining:
            try:
                self._pool.put_nowait(conn)
            except Exception:
                self._close_connection(conn)

        if cleaned > 0:
            logger.debug(f"유휴 연결 정리: {cleaned}개")

    def get_stats(self) -> dict:
        """풀 상태 반환"""
        with self._lock:
            return {
                'pool_key': self._pool_key,
                'total_created': self._created_count,
                'in_use': self._in_use_count,
                'available': self._pool.qsize(),
                'max_connections': self._max_connections,
                'min_connections': self._min_connections,
                'idle_timeout': self._idle_timeout
            }

    @property
    def pool_key(self) -> str:
        """풀 키"""
        return self._pool_key


# =====================================================================
# 전역 풀 레지스트리
# =====================================================================
class ConnectionPoolRegistry:
    """연결 풀 전역 레지스트리 (싱글톤)"""

    _instance = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._pools: Dict[str, ConnectionPool] = {}
                    cls._instance._registry_lock = Lock()
        return cls._instance

    @classmethod
    def instance(cls) -> 'ConnectionPoolRegistry':
        """싱글톤 인스턴스 반환"""
        return cls()

    def get_pool_key(self, host: str, port: int, user: str, database: str = None) -> str:
        """풀 키 생성"""
        return f"{user}@{host}:{port}/{database or 'default'}"

    def get_or_create_pool(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str = None,
        **kwargs
    ) -> ConnectionPool:
        """풀 획득 또는 생성

        동일 연결 정보에 대해 풀 재사용
        """
        pool_key = self.get_pool_key(host, port, user, database)

        with self._registry_lock:
            if pool_key not in self._pools:
                pool = ConnectionPool(
                    host, port, user, password, database, **kwargs
                )
                pool.start_cleaner()
                self._pools[pool_key] = pool
                logger.info(f"풀 등록: {pool_key}")

            return self._pools[pool_key]

    def get_pool(self, pool_key: str) -> Optional[ConnectionPool]:
        """키로 풀 조회"""
        return self._pools.get(pool_key)

    def remove_pool(self, pool_key: str):
        """풀 제거"""
        with self._registry_lock:
            if pool_key in self._pools:
                pool = self._pools.pop(pool_key)
                pool.close_all()
                logger.info(f"풀 제거: {pool_key}")

    def close_all_pools(self):
        """모든 풀 종료"""
        with self._registry_lock:
            for pool_key, pool in list(self._pools.items()):
                pool.close_all()
            self._pools.clear()
            logger.info("모든 연결 풀 종료")

    def get_all_stats(self) -> list:
        """모든 풀 상태 반환"""
        with self._registry_lock:
            return [pool.get_stats() for pool in self._pools.values()]

    @property
    def pool_count(self) -> int:
        """등록된 풀 수"""
        return len(self._pools)


# =====================================================================
# 컨텍스트 매니저
# =====================================================================
class PooledConnection:
    """풀 연결 컨텍스트 매니저

    with 문으로 사용 시 자동 반환
    """

    def __init__(self, pool: ConnectionPool):
        self._pool = pool
        self._conn = None

    def __enter__(self) -> Connection:
        self._conn = self._pool.get_connection()
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._conn:
            # 암묵적 트랜잭션 정리 (예외 여부 무관)
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._pool.return_connection(self._conn)
            self._conn = None
        return False


# 편의 함수
def get_pool_registry() -> ConnectionPoolRegistry:
    """풀 레지스트리 인스턴스 반환"""
    return ConnectionPoolRegistry.instance()
