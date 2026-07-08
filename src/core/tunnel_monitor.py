"""
터널 상태 모니터링
- 연결 상태 실시간 감시
- Latency 측정 (Rust DB Core health connection 사용)
- 자동 재연결
- 이벤트 히스토리
"""
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any
from enum import Enum

from src.core.db_core_service import create_rust_db_connector, normalize_db_engine
from src.core.logger import get_logger
from src.core.constants import DEFAULT_MYSQL_PORT, DEFAULT_LOCAL_HOST

logger = get_logger(__name__)


class TunnelState(Enum):
    """터널 연결 상태"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass
class TunnelStatus:
    """터널 상태 정보"""
    tunnel_id: str
    state: TunnelState = TunnelState.DISCONNECTED
    connected_at: Optional[datetime] = None
    last_check: Optional[datetime] = None
    latency_ms: Optional[float] = None
    error_message: Optional[str] = None
    reconnect_count: int = 0
    latency_history: List[float] = field(default_factory=list)

    def get_connection_duration(self) -> Optional[float]:
        """연결 지속 시간 (초)"""
        if self.state == TunnelState.CONNECTED and self.connected_at:
            return (datetime.now() - self.connected_at).total_seconds()
        return None

    def get_average_latency(self) -> Optional[float]:
        """평균 Latency (최근 10회)"""
        if not self.latency_history:
            return None
        recent = self.latency_history[-10:]
        return sum(recent) / len(recent)

    def format_duration(self) -> str:
        """연결 지속 시간 포맷팅"""
        duration = self.get_connection_duration()
        if duration is None:
            return "-"

        hours = int(duration // 3600)
        minutes = int((duration % 3600) // 60)
        seconds = int(duration % 60)

        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"


@dataclass
class TunnelEvent:
    """터널 이벤트"""
    timestamp: datetime
    tunnel_id: str
    event_type: str  # "connected", "disconnected", "reconnected", "error"
    message: str


class TunnelMonitor:
    """터널 상태 모니터"""

    def __init__(self, tunnel_engine, config_manager=None, max_events: int = 100):
        """
        Args:
            tunnel_engine: TunnelEngine 인스턴스
            config_manager: ConfigManager 인스턴스 (자동 재연결 설정용)
            max_events: 저장할 최대 이벤트 수
        """
        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager
        self._statuses: Dict[str, TunnelStatus] = {}
        self._events: List[TunnelEvent] = []
        self._max_events = max_events
        self._running = False
        self._auto_reconnect = True
        self._max_reconnect_attempts = 5
        self._callbacks: List[Callable[[str, TunnelStatus], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()  # RLock: 재진입 가능 (on_tunnel_connected 등 내부 중첩 호출 대응)

        # Health check용 Rust DB Core 연결 캐시 (터널별 1개씩 유지)
        self._health_connections: Dict[str, Any] = {}

        # 설정에서 자동 재연결 설정 로드
        if config_manager:
            self._auto_reconnect = config_manager.get_app_setting(
                'auto_reconnect', True
            )
            self._max_reconnect_attempts = config_manager.get_app_setting(
                'max_reconnect_attempts', 5
            )

    def add_callback(self, callback: Callable[[str, TunnelStatus], None]):
        """상태 변경 콜백 등록

        Args:
            callback: callback(tunnel_id, status)
        """
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable):
        """콜백 제거"""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _notify_callbacks(self, tunnel_id: str, status: TunnelStatus):
        """콜백 호출"""
        for callback in self._callbacks:
            try:
                callback(tunnel_id, status)
            except Exception as e:
                logger.error(f"콜백 실행 오류: {e}")

    def start_monitoring(self, interval: int = 5):
        """모니터링 시작

        Args:
            interval: 체크 간격 (초)
        """
        if self._running:
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            args=(interval,),
            daemon=True
        )
        self._thread.start()
        logger.info("터널 모니터링 시작")

    def stop_monitoring(self):
        """모니터링 중지"""
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

        # Health check 연결 모두 정리
        self._cleanup_all_health_connections()
        logger.info("터널 모니터링 중지")

    def _cleanup_health_connection(self, tunnel_id: str):
        """특정 터널의 health check 연결 정리

        딕셔너리 pop만 락으로 보호하고, 실제 연결 종료(I/O)는 락 밖에서 수행한다.
        """
        with self._lock:
            conn = self._health_connections.pop(tunnel_id, None)
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

        for tunnel_id, conn in connections.items():
            try:
                conn.close()
                logger.debug(f"Health check 연결 정리: {tunnel_id}")
            except Exception:
                pass

    def is_running(self) -> bool:
        """모니터링 실행 중 여부"""
        return self._running

    def get_status(self, tunnel_id: str) -> TunnelStatus:
        """특정 터널 상태 조회"""
        with self._lock:
            if tunnel_id not in self._statuses:
                self._statuses[tunnel_id] = TunnelStatus(tunnel_id=tunnel_id)
            return self._statuses[tunnel_id]

    def get_all_statuses(self) -> Dict[str, TunnelStatus]:
        """모든 터널 상태 조회"""
        with self._lock:
            return dict(self._statuses)

    def get_recent_events(self, tunnel_id: Optional[str] = None,
                          limit: int = 20) -> List[TunnelEvent]:
        """최근 이벤트 조회

        Args:
            tunnel_id: 특정 터널 ID (None이면 전체)
            limit: 최대 반환 개수

        Returns:
            이벤트 목록 (최신순)
        """
        with self._lock:
            if tunnel_id:
                filtered = [e for e in self._events if e.tunnel_id == tunnel_id]
            else:
                filtered = list(self._events)

            return filtered[-limit:][::-1]  # 최신순

    def set_auto_reconnect(self, enabled: bool):
        """자동 재연결 설정"""
        self._auto_reconnect = enabled
        if self.config_manager:
            self.config_manager.set_app_setting('auto_reconnect', enabled)
        logger.info(f"자동 재연결: {'활성화' if enabled else '비활성화'}")

    def is_auto_reconnect_enabled(self) -> bool:
        """자동 재연결 활성화 여부"""
        return self._auto_reconnect

    def set_max_reconnect_attempts(self, count: int):
        """최대 재연결 시도 횟수 설정"""
        self._max_reconnect_attempts = max(1, count)
        if self.config_manager:
            self.config_manager.set_app_setting(
                'max_reconnect_attempts', self._max_reconnect_attempts
            )

    def get_max_reconnect_attempts(self) -> int:
        """최대 재연결 시도 횟수"""
        return self._max_reconnect_attempts

    def _monitor_loop(self, interval: int):
        """모니터링 메인 루프"""
        while self._running and not self._stop_event.is_set():
            try:
                self._check_all_tunnels()
            except Exception as e:
                logger.error(f"모니터링 루프 오류: {e}")

            self._stop_event.wait(interval)

    def _check_all_tunnels(self):
        """모든 활성 터널 상태 확인

        Latency 측정(네트워크 I/O)은 self._lock을 점유하지 않은 상태에서 수행한다.
        상태 전이는 1단계(락 보유)에서 처리하고, 연결 중인 터널 목록만 모아
        락 밖에서 측정한 뒤 2단계(락 재획득)에서 결과를 반영한다.
        """
        # 현재 활성 터널 목록
        active_ids = set(self.tunnel_engine.active_tunnels.keys())
        latency_targets: List[str] = []

        # 1단계: 상태 전이 및 이벤트 처리 (락 보유)
        with self._lock:
            for tunnel_id in active_ids:
                status = self._statuses.get(tunnel_id)
                if not status:
                    status = TunnelStatus(tunnel_id=tunnel_id)
                    self._statuses[tunnel_id] = status

                # 상태 업데이트
                was_connected = (status.state == TunnelState.CONNECTED)

                if self.tunnel_engine.is_running(tunnel_id):
                    # 연결 중
                    if status.state != TunnelState.CONNECTED:
                        status.state = TunnelState.CONNECTED
                        status.connected_at = datetime.now()
                        status.reconnect_count = 0
                        status.error_message = None
                        self._add_event(tunnel_id, "connected", "터널 연결됨")

                    status.last_check = datetime.now()
                    # Latency 측정은 락 밖에서 수행하도록 대상만 기록
                    latency_targets.append(tunnel_id)
                else:
                    # 연결 끊김 감지
                    if was_connected:
                        status.state = TunnelState.DISCONNECTED
                        status.connected_at = None
                        status.latency_ms = None
                        self._add_event(tunnel_id, "disconnected", "터널 연결 끊김")

                        # Health check 연결 정리
                        self._cleanup_health_connection(tunnel_id)

                        # 자동 재연결 시도
                        if self._auto_reconnect:
                            self._attempt_reconnect(tunnel_id)

                    self._notify_callbacks(tunnel_id, status)

            # 비활성 터널 상태 업데이트
            for tunnel_id, status in list(self._statuses.items()):
                if tunnel_id not in active_ids:
                    if status.state == TunnelState.CONNECTED:
                        status.state = TunnelState.DISCONNECTED
                        status.connected_at = None
                        status.latency_ms = None
                        self._add_event(tunnel_id, "disconnected", "터널 연결 종료")
                        # Health check 연결 정리
                        self._cleanup_health_connection(tunnel_id)
                        self._notify_callbacks(tunnel_id, status)

        # 2단계: 락 밖에서 Latency 측정 (DB 프로토콜 통신 포함)
        latency_results = {
            tunnel_id: self._measure_latency(tunnel_id)
            for tunnel_id in latency_targets
        }

        # 3단계: 측정 결과 반영 (락 재획득)
        with self._lock:
            for tunnel_id, latency in latency_results.items():
                status = self._statuses.get(tunnel_id)
                if not status:
                    continue
                # 측정 도중 터널이 끊어졌으면 결과를 반영하지 않는다
                if not self.tunnel_engine.is_running(tunnel_id):
                    continue

                if latency >= 0:
                    status.latency_ms = latency
                    status.latency_history.append(latency)
                    # 히스토리 최대 100개 유지
                    if len(status.latency_history) > 100:
                        status.latency_history = status.latency_history[-100:]
                else:
                    status.latency_ms = None

                self._notify_callbacks(tunnel_id, status)

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
        try:
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
        except Exception as e:
            logger.debug(f"Health check 연결 생성 실패 ({tunnel_id}): {e}")
            return None

    def _attempt_reconnect(self, tunnel_id: str):
        """자동 재연결 시도

        재연결 상태(state/error_message/reconnect_count) 변경은 항상 self._lock을
        직접 획득하여 보호한다 (호출자가 이미 락을 쥐고 있어도 RLock이라 안전).

        Args:
            tunnel_id: 터널 ID
        """
        with self._lock:
            status = self._statuses.get(tunnel_id)
            if not status:
                return

            # 최대 재연결 시도 횟수 체크
            if status.reconnect_count >= self._max_reconnect_attempts:
                status.state = TunnelState.ERROR
                status.error_message = "최대 재연결 시도 횟수 초과"
                self._add_event(
                    tunnel_id, "error",
                    f"재연결 실패 (시도 {status.reconnect_count}회)"
                )
                return

            # 백오프 딜레이: 1s, 2s, 5s, 10s, 30s, 60s
            backoff = [1, 2, 5, 10, 30, 60]
            delay = backoff[min(status.reconnect_count, len(backoff) - 1)]

            status.state = TunnelState.RECONNECTING
            status.reconnect_count += 1

            self._add_event(
                tunnel_id, "reconnecting",
                f"재연결 시도 {status.reconnect_count}/{self._max_reconnect_attempts} ({delay}초 대기)"
            )

        # 별도 스레드에서 재연결 시도 (락을 점유하지 않은 상태로 예약)
        def reconnect():
            time.sleep(delay)

            if not self._running:
                return

            try:
                # start_tunnel은 config dict를 요구함. stop_tunnel이 tunnel_configs를
                # 삭제하므로, 반드시 stop 호출 전에 config를 조회/보관한다.
                config = self.tunnel_engine.tunnel_configs.get(tunnel_id)
                if not config:
                    with self._lock:
                        status.state = TunnelState.ERROR
                        status.error_message = "재연결 실패: 터널 설정을 찾을 수 없음"
                        self._add_event(
                            tunnel_id, "error", status.error_message
                        )
                        self._notify_callbacks(tunnel_id, status)
                    return

                # 끊긴 stale server 객체와 tunnel_configs 엔트리를 정리하여
                # 포트 충돌 및 객체 누수를 방지한다.
                self.tunnel_engine.stop_tunnel(tunnel_id)

                # 자동 재연결은 포트 재사용 상황이라 포트 충돌 체크를 건너뛴다.
                success, msg = self.tunnel_engine.start_tunnel(
                    config, check_port=False
                )

                should_retry = False
                with self._lock:
                    if success:
                        status.state = TunnelState.CONNECTED
                        status.connected_at = datetime.now()
                        status.reconnect_count = 0
                        status.error_message = None
                        self._add_event(tunnel_id, "reconnected", "자동 재연결 성공")
                    else:
                        status.state = TunnelState.ERROR
                        status.error_message = msg
                        should_retry = self._auto_reconnect and self._running

                    self._notify_callbacks(tunnel_id, status)

                # 재귀 호출(및 그에 이은 sleep)은 락을 놓은 뒤 수행하여
                # 대기 중에도 다른 스레드가 상태를 조회/갱신할 수 있게 한다.
                if should_retry:
                    self._attempt_reconnect(tunnel_id)

            except Exception as e:
                logger.error(f"재연결 오류 ({tunnel_id}): {e}")
                with self._lock:
                    status.state = TunnelState.ERROR
                    status.error_message = str(e)

        threading.Thread(target=reconnect, daemon=True).start()

    def _add_event(self, tunnel_id: str, event_type: str, message: str):
        """이벤트 추가"""
        event = TunnelEvent(
            timestamp=datetime.now(),
            tunnel_id=tunnel_id,
            event_type=event_type,
            message=message
        )
        self._events.append(event)

        # 최대 이벤트 수 유지
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]

        logger.debug(f"터널 이벤트: [{tunnel_id}] {event_type} - {message}")

    def on_tunnel_connected(self, tunnel_id: str):
        """터널 연결 시 호출 (외부에서 호출용)"""
        with self._lock:
            status = self.get_status(tunnel_id)
            status.state = TunnelState.CONNECTED
            status.connected_at = datetime.now()
            status.reconnect_count = 0
            status.error_message = None
            self._add_event(tunnel_id, "connected", "터널 연결됨")
            self._notify_callbacks(tunnel_id, status)

    def on_tunnel_disconnected(self, tunnel_id: str, error: str = None):
        """터널 연결 해제 시 호출 (외부에서 호출용)"""
        with self._lock:
            status = self.get_status(tunnel_id)
            status.state = TunnelState.DISCONNECTED
            status.connected_at = None
            status.latency_ms = None
            if error:
                status.error_message = error
                self._add_event(tunnel_id, "error", f"연결 오류: {error}")
            else:
                self._add_event(tunnel_id, "disconnected", "터널 연결 종료")
            # Health check 연결 정리
            self._cleanup_health_connection(tunnel_id)
            self._notify_callbacks(tunnel_id, status)
