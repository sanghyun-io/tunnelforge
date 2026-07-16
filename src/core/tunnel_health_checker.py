"""
터널 Health Check 연결 관리

TunnelMonitor에서 분리된 책임:
- 터널별 Health check용 DB 연결 캐시
- DB 자격 증명 조회 (config_manager 복호화 우선, 평문 fallback)
- Rust DB Core 커넥터를 통한 latency 측정
"""
import time
from typing import Any, Dict, Optional

from src.core.db_core_service import (
    DbCoreOutcome,
    DbCoreServiceError,
    create_rust_db_connector,
    normalize_db_engine,
)
from src.core.logger import get_logger
from src.core.constants import DEFAULT_MYSQL_PORT, DEFAULT_LOCAL_HOST

logger = get_logger(__name__)


class TunnelHealthChecker:
    """터널별 Health check DB 연결을 캐시하고 latency를 측정한다.

    호출자(TunnelMonitor)와 동일한 lock을 공유한다 — 딕셔너리 접근만 락으로
    보호하고, 실제 연결 생성/종료 같은 블로킹 I/O는 락 밖에서 수행한다.
    """

    def __init__(self, tunnel_engine, config_manager, lock):
        """
        Args:
            tunnel_engine: TunnelEngine 인스턴스 (활성 터널 설정 조회용)
            config_manager: ConfigManager 인스턴스 (자격 증명 복호화용, None 가능)
            lock: 호출자(TunnelMonitor)와 공유하는 threading.RLock
        """
        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager
        self._lock = lock

        # Health check용 Rust DB Core 연결 캐시 (터널별 1개씩 유지)
        self._health_connections: Dict[str, Any] = {}
        self._transport_failed_generations: Dict[str, int] = {}

    def _cleanup_health_connection(
        self, tunnel_id: str, *, reset_transport_failure: bool = True
    ):
        """특정 터널의 health check 연결 정리

        딕셔너리 pop만 락으로 보호하고, 실제 연결 종료(I/O)는 락 밖에서 수행한다.
        """
        with self._lock:
            conn = self._health_connections.pop(tunnel_id, None)
            if reset_transport_failure:
                self._transport_failed_generations.pop(tunnel_id, None)
        if conn:
            try:
                conn.close()
                logger.debug(f"Health check 연결 정리: {tunnel_id}")
            except Exception:
                pass

    def _cleanup_all_health_connections(self):
        """모든 health check 연결 정리 (딕셔너리 정리만 락으로 보호)"""
        with self._lock:
            connections = dict(self._health_connections)
            self._health_connections.clear()
            self._transport_failed_generations.clear()

        for tunnel_id, conn in connections.items():
            try:
                conn.close()
                logger.debug(f"Health check 연결 정리: {tunnel_id}")
            except Exception:
                pass

    def _reset_transport_failure(self, tunnel_id: str) -> None:
        with self._lock:
            self._transport_failed_generations.pop(tunnel_id, None)

    @staticmethod
    def _connection_process_generation(connection: Any) -> int:
        handle = getattr(connection, "connection_handle", None)
        try:
            return int(getattr(handle, "process_generation", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _mark_transport_failure(
        self, tunnel_id: str, connection: Any, error: DbCoreServiceError
    ) -> None:
        if error.outcome is not DbCoreOutcome.OUTCOME_INDETERMINATE:
            return
        generation = self._connection_process_generation(connection)
        if not generation:
            try:
                generation = int(error.process_generation or 0)
            except (TypeError, ValueError):
                generation = 0
        with self._lock:
            self._transport_failed_generations[tunnel_id] = generation
        logger.warning(
            "Health check transport contamination latched for %s (generation=%s)",
            tunnel_id,
            generation,
        )

    def _get_health_credentials(self, tunnel_id: str, config: Dict[str, Any]) -> tuple:
        """Health check용 DB 자격 증명 조회

        config_manager가 있으면 실제 저장 키(db_user)와 암호화된 비밀번호
        (db_password_encrypted)를 복호화하여 사용한다. config_manager가 없으면
        평문 비밀번호를 추측하지 않고, 암호화된 비밀번호가 존재할 경우
        health check 자체를 스킵한다.

        Args:
            tunnel_id: 터널 ID
            config: 터널 설정 dict

        Returns:
            (db_user, db_password) 튜플. 조회 불가 시 ("", "")
        """
        get_credentials = getattr(self.config_manager, 'get_tunnel_credentials', None)
        if callable(get_credentials):
            try:
                db_user, db_password = get_credentials(tunnel_id)
                return db_user or '', db_password or ''
            except Exception as e:
                logger.debug(f"자격 증명 조회 실패 ({tunnel_id}): {e}")
                return '', ''

        db_user = config.get('db_user', '')
        if config.get('db_password_encrypted'):
            # 복호화 가능한 config_manager가 없어 암호문을 그대로 쓸 수 없음
            logger.debug(f"Latency 측정 스킵 (비밀번호 복호화 불가): {tunnel_id}")
            return '', ''

        return db_user, ''

    def _measure_latency(self, tunnel_id: str) -> float:
        """Latency 측정 (Rust DB Core 연결 사용)

        MySQL/PostgreSQL 모두 실제 DB 프로토콜 연결을 통해 응답 시간을 측정합니다.

        Args:
            tunnel_id: 터널 ID

        Returns:
            지연 시간 (밀리초), 측정 실패 시 -1
        """
        try:
            config = self.tunnel_engine.tunnel_configs.get(tunnel_id)
            if not config:
                return -1

            # DB 인증 정보 확인 (실제 config 키: db_user + 암호화된 db_password_encrypted)
            db_username, db_password = self._get_health_credentials(tunnel_id, config)
            db_name = config.get('default_database') or config.get('db_name') or config.get('default_schema') or ''
            db_engine = normalize_db_engine(config.get('db_engine'), config.get('remote_port'))

            with self._lock:
                if tunnel_id in self._transport_failed_generations:
                    return -1

            # 인증 정보가 없으면 측정 불가
            if not db_username:
                logger.debug(f"Latency 측정 스킵 (DB 인증 정보 없음): {tunnel_id}")
                return -1

            connection_mode = config.get('connection_mode', 'ssh')

            if connection_mode == 'direct':
                host = config.get('remote_host', '')
                port = int(config.get('remote_port', DEFAULT_MYSQL_PORT))
            else:
                # SSH 터널 모드: 로컬 포트 사용
                local_port = config.get('local_port')
                if not local_port:
                    return -1
                host = DEFAULT_LOCAL_HOST
                port = int(local_port)

            # 캐시된 연결 조회 (딕셔너리 조회만 락으로 보호)
            with self._lock:
                conn = self._health_connections.get(tunnel_id)

            # 연결이 없거나 끊어진 경우 재연결 (연결 생성은 락 밖에서 수행)
            if conn is None:
                conn = self._create_health_connection(
                    tunnel_id, db_engine, host, port, db_username, db_password, db_name
                )
                if conn is None:
                    return -1

            # Rust Core connection ping으로 latency 측정
            start = time.time()
            try:
                conn.ping(reconnect=False)
                latency = (time.time() - start) * 1000
                return latency
            except DbCoreServiceError as e:
                self._mark_transport_failure(tunnel_id, conn, e)
                logger.debug(f"DB ping 실패 ({tunnel_id}): {e}")
                self._cleanup_health_connection(
                    tunnel_id,
                    reset_transport_failure=False,
                )
                return -1
            except Exception as e:
                logger.debug(f"DB ping 실패 ({tunnel_id}): {e}")
                # 연결 끊김 - 캐시에서 제거
                self._cleanup_health_connection(tunnel_id)
                return -1

        except Exception as e:
            logger.debug(f"Latency 측정 오류 ({tunnel_id}): {e}")
            return -1

    def _create_health_connection(
        self, tunnel_id: str, engine: str, host: str, port: int,
        username: str, password: str, database: str
    ) -> Optional[Any]:
        """Health check용 DB 연결 생성

        Rust 커넥터 connect()/autocommit() 같은 블로킹 I/O는 락 밖에서 수행하고,
        캐시 딕셔너리 갱신만 락으로 보호한다.

        Args:
            tunnel_id: 터널 ID
            host: DB 호스트
            port: DB 포트
            username: DB 사용자명
            password: DB 비밀번호
            database: DB 이름

        Returns:
            DB connection 또는 None
        """
        connector = None
        try:
            with self._lock:
                if tunnel_id in self._transport_failed_generations:
                    return None
            connector = create_rust_db_connector(
                engine,
                host,
                port,
                username,
                password,
                database if database else None,
            )
            success, message = connector.connect()
            if not success or connector.connection is None:
                logger.debug(f"Health check 연결 생성 실패 ({tunnel_id}): {message}")
                return None
            connector.connection.autocommit(True)
            conn = connector.connection

            # 다른 스레드가 동시에 같은 터널의 연결을 이미 캐시했다면
            # 기존 것을 사용하고 방금 만든 연결은 락 밖에서 닫는다.
            duplicate = None
            with self._lock:
                existing = self._health_connections.get(tunnel_id)
                if existing is not None:
                    duplicate = conn
                    conn = existing
                else:
                    self._health_connections[tunnel_id] = conn

            if duplicate is not None:
                try:
                    duplicate.close()
                except Exception:
                    pass
            else:
                logger.debug(f"Health check 연결 생성: {tunnel_id}")

            return conn
        except DbCoreServiceError as e:
            self._mark_transport_failure(
                tunnel_id,
                getattr(connector, "connection", None),
                e,
            )
            connection = getattr(connector, "connection", None)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            logger.debug(f"Health check 연결 생성 실패 ({tunnel_id}): {e}")
            return None
        except Exception as e:
            logger.debug(f"Health check 연결 생성 실패 ({tunnel_id}): {e}")
            return None
