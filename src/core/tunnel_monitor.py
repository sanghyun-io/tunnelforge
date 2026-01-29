"""
터널 상태 모니터링
- 연결 상태 실시간 감시
- Latency 측정 (MySQL ping 사용)
- 자동 재연결
- 이벤트 히스토리
"""
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any
from enum import Enum

import pymysql

from src.core.logger import get_logger

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
        self._lock = threading.Lock()

        # Health check용 MySQL 연결 캐시 (터널별 1개씩 유지)
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
        """특정 터널의 health check 연결 정리"""
        conn = self._health_connections.pop(tunnel_id, None)
        if conn:
            try:
                conn.close()
                logger.debug(f"Health check 연결 정리: {tunnel_id}")
            except Exception:
                pass

    def _cleanup_all_health_connections(self):
        """모든 health check 연결 정리"""
        for tunnel_id in list(self._health_connections.keys()):
            self._cleanup_health_connection(tunnel_id)

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
        """모든 활성 터널 상태 확인"""
        # 현재 활성 터널 목록
        active_ids = set(self.tunnel_engine.active_tunnels.keys())

        with self._lock:
            # 연결된 터널 체크
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

                    # Latency 측정
                    latency = self._measure_latency(tunnel_id)
                    if latency >= 0:
                        status.latency_ms = latency
                        status.latency_history.append(latency)
                        # 히스토리 최대 100개 유지
                        if len(status.latency_history) > 100:
                            status.latency_history = status.latency_history[-100:]

                    status.last_check = datetime.now()
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

    def _measure_latency(self, tunnel_id: str) -> float:
        """Latency 측정 (MySQL ping 사용)

        MySQL ping을 사용하여 실제 DB 연결 응답 시간을 측정합니다.
        TCP 소켓만 연결하면 MySQL handshake_error가 발생하므로,
        정상적인 MySQL 프로토콜을 사용합니다.

        Args:
            tunnel_id: 터널 ID

        Returns:
            지연 시간 (밀리초), 측정 실패 시 -1
        """
        try:
            config = self.tunnel_engine.tunnel_configs.get(tunnel_id)
            if not config:
                return -1

            # DB 인증 정보 확인
            db_username = config.get('db_username', '')
            db_password = config.get('db_password', '')
            db_name = config.get('db_name', '')

            # 인증 정보가 없으면 측정 불가
            if not db_username:
                logger.debug(f"Latency 측정 스킵 (DB 인증 정보 없음): {tunnel_id}")
                return -1

            connection_mode = config.get('connection_mode', 'ssh')

            if connection_mode == 'direct':
                host = config.get('remote_host', '')
                port = int(config.get('remote_port', 3306))
            else:
                # SSH 터널 모드: 로컬 포트 사용
                local_port = config.get('local_port')
                if not local_port:
                    return -1
                host = '127.0.0.1'
                port = int(local_port)

            # 캐시된 연결 사용 또는 새 연결 생성
            conn = self._health_connections.get(tunnel_id)

            # 연결이 없거나 끊어진 경우 재연결
            if conn is None:
                conn = self._create_health_connection(
                    tunnel_id, host, port, db_username, db_password, db_name
                )
                if conn is None:
                    return -1

            # MySQL ping으로 latency 측정
            start = time.time()
            try:
                conn.ping(reconnect=False)
                latency = (time.time() - start) * 1000
                return latency
            except Exception as e:
                logger.debug(f"MySQL ping 실패 ({tunnel_id}): {e}")
                # 연결 끊김 - 캐시에서 제거
                self._cleanup_health_connection(tunnel_id)
                return -1

        except Exception as e:
            logger.debug(f"Latency 측정 오류 ({tunnel_id}): {e}")
            return -1

    def _create_health_connection(
        self, tunnel_id: str, host: str, port: int,
        username: str, password: str, database: str
    ) -> Optional[Any]:
        """Health check용 MySQL 연결 생성

        Args:
            tunnel_id: 터널 ID
            host: DB 호스트
            port: DB 포트
            username: DB 사용자명
            password: DB 비밀번호
            database: DB 이름

        Returns:
            pymysql Connection 또는 None
        """
        try:
            conn = pymysql.connect(
                host=host,
                port=port,
                user=username,
                password=password,
                database=database if database else None,
                connect_timeout=5,
                read_timeout=5,
                write_timeout=5,
                autocommit=True
            )
            self._health_connections[tunnel_id] = conn
            logger.debug(f"Health check 연결 생성: {tunnel_id}")
            return conn
        except Exception as e:
            logger.debug(f"Health check 연결 생성 실패 ({tunnel_id}): {e}")
            return None

    def _attempt_reconnect(self, tunnel_id: str):
        """자동 재연결 시도

        Args:
            tunnel_id: 터널 ID
        """
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

        # 별도 스레드에서 재연결 시도
        def reconnect():
            time.sleep(delay)

            if not self._running:
                return

            try:
                success, msg = self.tunnel_engine.start_tunnel(tunnel_id)

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
                        # 다시 재연결 시도
                        if self._auto_reconnect and self._running:
                            self._attempt_reconnect(tunnel_id)

                    self._notify_callbacks(tunnel_id, status)

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
