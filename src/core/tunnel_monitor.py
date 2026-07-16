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
from typing import Dict, List, Optional, Callable
from enum import Enum

from src.core.logger import get_logger
from src.core.tunnel_health_checker import TunnelHealthChecker

logger = get_logger(__name__)

# 재연결 백오프 정책: 시도 횟수가 늘어날수록 대기 시간을 늘려 재시도 폭주를 방지한다.
RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 30, 60)


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

        # Health check 책임은 TunnelHealthChecker로 위임 (터널별 연결 캐시 포함)
        self._health_checker = TunnelHealthChecker(tunnel_engine, config_manager, self._lock)

        # 설정에서 자동 재연결 설정 로드
        if config_manager:
            self._auto_reconnect = config_manager.get_app_setting(
                'auto_reconnect', True
            )
            self._max_reconnect_attempts = config_manager.get_app_setting(
                'max_reconnect_attempts', 5
            )

    @property
    def config_manager(self):
        """자동 재연결 설정 로드에 쓰인 것과 동일한 config_manager (health checker와 공유)"""
        return self._health_checker.config_manager

    @config_manager.setter
    def config_manager(self, value):
        self._health_checker.config_manager = value

    @property
    def _health_connections(self):
        """health checker가 소유한 동일 dict 객체 (item 대입이 그대로 반영됨)"""
        return self._health_checker._health_connections

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
        """특정 터널의 health check 연결 정리 (TunnelHealthChecker로 위임)"""
        self._health_checker._cleanup_health_connection(tunnel_id)

    def _cleanup_all_health_connections(self):
        """모든 health check 연결 정리 (TunnelHealthChecker로 위임)"""
        self._health_checker._cleanup_all_health_connections()

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
                        self._health_checker._reset_transport_failure(tunnel_id)
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

    def _get_health_credentials(self, tunnel_id: str, config: Dict[str, object]) -> tuple:
        """Health check용 DB 자격 증명 조회 (TunnelHealthChecker로 위임)"""
        return self._health_checker._get_health_credentials(tunnel_id, config)

    def _measure_latency(self, tunnel_id: str) -> float:
        """Latency 측정 (TunnelHealthChecker로 위임)"""
        return self._health_checker._measure_latency(tunnel_id)

    def _create_health_connection(
        self, tunnel_id: str, engine: str, host: str, port: int,
        username: str, password: str, database: str
    ):
        """Health check용 DB 연결 생성 (TunnelHealthChecker로 위임)"""
        return self._health_checker._create_health_connection(
            tunnel_id, engine, host, port, username, password, database
        )

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

            # 백오프 딜레이: RECONNECT_BACKOFF_SECONDS 참조 (증가 정책은 모듈 상수 주석 참조)
            delay = RECONNECT_BACKOFF_SECONDS[min(status.reconnect_count, len(RECONNECT_BACKOFF_SECONDS) - 1)]

            status.state = TunnelState.RECONNECTING
            status.reconnect_count += 1

            self._add_event(
                tunnel_id, "reconnecting",
                f"재연결 시도 {status.reconnect_count}/{self._max_reconnect_attempts} ({delay}초 대기)"
            )

        # 별도 스레드에서 재연결 시도 (락을 점유하지 않은 상태로 예약)
        threading.Thread(
            target=self._reconnect_after_delay,
            args=(tunnel_id, delay, status),
            daemon=True
        ).start()

    def _reconnect_after_delay(self, tunnel_id, delay, status):
        """지연 시간 대기 후 재연결 시도 (별도 스레드에서 실행)

        _attempt_reconnect가 예약한 백오프 지연을 소비한 뒤 실제 재연결을 수행한다.
        """
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
            if status.state != TunnelState.CONNECTED:
                self._health_checker._reset_transport_failure(tunnel_id)
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
