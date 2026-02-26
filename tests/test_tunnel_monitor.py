"""
TunnelMonitor 단위 테스트
"""
import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, call


# =====================================================================
# TunnelStatus 테스트
# =====================================================================

class TestTunnelStatus:
    """TunnelStatus 데이터클래스 테스트"""

    def test_get_connection_duration_connected(self):
        """연결 중인 경우 지속 시간 반환"""
        from src.core.tunnel_monitor import TunnelStatus, TunnelState

        status = TunnelStatus(tunnel_id='t1')
        status.state = TunnelState.CONNECTED
        status.connected_at = datetime.now() - timedelta(seconds=60)

        duration = status.get_connection_duration()
        assert duration is not None
        assert duration >= 59  # 약 60초

    def test_get_connection_duration_disconnected(self):
        """연결 안 된 경우 None 반환"""
        from src.core.tunnel_monitor import TunnelStatus, TunnelState

        status = TunnelStatus(tunnel_id='t1')
        status.state = TunnelState.DISCONNECTED

        assert status.get_connection_duration() is None

    def test_get_average_latency_empty(self):
        """Latency 히스토리 없을 때 None 반환"""
        from src.core.tunnel_monitor import TunnelStatus

        status = TunnelStatus(tunnel_id='t1')
        assert status.get_average_latency() is None

    def test_get_average_latency_with_data(self):
        """Latency 평균 계산 확인"""
        from src.core.tunnel_monitor import TunnelStatus

        status = TunnelStatus(tunnel_id='t1')
        status.latency_history = [10.0, 20.0, 30.0]

        avg = status.get_average_latency()
        assert avg == 20.0

    def test_get_average_latency_recent_10(self):
        """최근 10개만 사용하여 평균 계산"""
        from src.core.tunnel_monitor import TunnelStatus

        status = TunnelStatus(tunnel_id='t1')
        # 15개 추가 (최근 10개: 6~15)
        status.latency_history = list(range(1, 16))

        avg = status.get_average_latency()
        # 6+7+8+9+10+11+12+13+14+15 = 105, / 10 = 10.5
        assert avg == 10.5

    def test_format_duration_disconnected(self):
        """연결 안 된 경우 '-' 반환"""
        from src.core.tunnel_monitor import TunnelStatus, TunnelState

        status = TunnelStatus(tunnel_id='t1')
        status.state = TunnelState.DISCONNECTED
        assert status.format_duration() == '-'

    def test_format_duration_minutes_seconds(self):
        """분:초 형식 포맷팅"""
        from src.core.tunnel_monitor import TunnelStatus, TunnelState

        status = TunnelStatus(tunnel_id='t1')
        status.state = TunnelState.CONNECTED
        status.connected_at = datetime.now() - timedelta(seconds=90)

        duration = status.format_duration()
        # 01:30 형식이어야 함
        assert ':' in duration

    def test_format_duration_hours(self):
        """시:분:초 형식 포맷팅 (1시간 이상)"""
        from src.core.tunnel_monitor import TunnelStatus, TunnelState

        status = TunnelStatus(tunnel_id='t1')
        status.state = TunnelState.CONNECTED
        status.connected_at = datetime.now() - timedelta(hours=2, minutes=5)

        duration = status.format_duration()
        parts = duration.split(':')
        assert len(parts) == 3
        assert int(parts[0]) >= 2


# =====================================================================
# TunnelMonitor 테스트
# =====================================================================

class TestTunnelMonitor:
    """TunnelMonitor 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self):
        """각 테스트 전 TunnelMonitor 생성"""
        from src.core.tunnel_monitor import TunnelMonitor

        self.mock_engine = MagicMock()
        self.mock_engine.active_tunnels = {}
        self.mock_engine.is_running.return_value = False
        self.mock_engine.tunnel_configs = {}

        self.monitor = TunnelMonitor(
            tunnel_engine=self.mock_engine,
            config_manager=None,
            max_events=50
        )
        yield
        # 모니터링 빠른 정리 (thread.join 대기 최소화)
        if self.monitor.is_running():
            self.monitor._running = False
            self.monitor._stop_event.set()
            if self.monitor._thread and self.monitor._thread.is_alive():
                self.monitor._thread.join(timeout=1)
            self.monitor._thread = None

    def test_initial_state_not_running(self):
        """초기 상태 미실행 확인"""
        assert self.monitor.is_running() is False

    def test_start_monitoring_sets_running(self):
        """모니터링 시작 확인"""
        self.monitor.start_monitoring(interval=60)
        assert self.monitor.is_running() is True

    def test_stop_monitoring_clears_running(self):
        """모니터링 중지 확인"""
        self.monitor.start_monitoring(interval=60)
        self.monitor.stop_monitoring()
        assert self.monitor.is_running() is False

    def test_start_monitoring_idempotent(self):
        """이미 실행 중일 때 start 호출 무시"""
        self.monitor.start_monitoring(interval=60)
        thread_before = self.monitor._thread

        self.monitor.start_monitoring(interval=60)  # 두 번째 호출
        assert self.monitor._thread is thread_before

    def test_get_status_creates_new_entry(self):
        """존재하지 않는 터널 상태 조회 시 새 항목 생성"""
        from src.core.tunnel_monitor import TunnelState

        status = self.monitor.get_status('new_tunnel')
        assert status.tunnel_id == 'new_tunnel'
        assert status.state == TunnelState.DISCONNECTED

    def test_get_all_statuses_empty(self):
        """초기 상태 조회 시 빈 딕셔너리"""
        result = self.monitor.get_all_statuses()
        assert isinstance(result, dict)

    def test_add_callback_and_notify(self):
        """콜백 등록 및 호출 확인"""
        from src.core.tunnel_monitor import TunnelStatus

        callback = MagicMock()
        self.monitor.add_callback(callback)

        mock_status = MagicMock(spec=TunnelStatus)
        self.monitor._notify_callbacks('tunnel1', mock_status)

        callback.assert_called_once_with('tunnel1', mock_status)

    def test_remove_callback(self):
        """콜백 제거 확인"""
        callback = MagicMock()
        self.monitor.add_callback(callback)
        self.monitor.remove_callback(callback)

        self.monitor._notify_callbacks('tunnel1', MagicMock())
        callback.assert_not_called()

    def test_remove_nonexistent_callback(self):
        """존재하지 않는 콜백 제거 시 예외 없음"""
        callback = MagicMock()
        # 추가하지 않고 제거 시도
        self.monitor.remove_callback(callback)

    def test_callback_exception_does_not_propagate(self):
        """콜백 예외가 전파되지 않음을 확인"""
        bad_callback = MagicMock(side_effect=Exception("Callback error"))
        self.monitor.add_callback(bad_callback)

        # 예외 없이 통과해야 함
        self.monitor._notify_callbacks('tunnel1', MagicMock())

    def test_set_auto_reconnect_enabled(self):
        """자동 재연결 활성화 설정"""
        self.monitor.set_auto_reconnect(True)
        assert self.monitor.is_auto_reconnect_enabled() is True

    def test_set_auto_reconnect_disabled(self):
        """자동 재연결 비활성화 설정"""
        self.monitor.set_auto_reconnect(False)
        assert self.monitor.is_auto_reconnect_enabled() is False

    def test_set_max_reconnect_attempts(self):
        """최대 재연결 시도 횟수 설정"""
        self.monitor.set_max_reconnect_attempts(10)
        assert self.monitor.get_max_reconnect_attempts() == 10

    def test_set_max_reconnect_attempts_minimum_one(self):
        """최대 재연결 시도 횟수 최소 1 보장"""
        self.monitor.set_max_reconnect_attempts(0)
        assert self.monitor.get_max_reconnect_attempts() == 1

    def test_get_recent_events_all(self):
        """전체 최근 이벤트 조회"""
        self.monitor._add_event('t1', 'connected', '연결됨')
        self.monitor._add_event('t2', 'connected', '연결됨')
        self.monitor._add_event('t1', 'disconnected', '연결 끊김')

        events = self.monitor.get_recent_events()
        assert len(events) == 3

    def test_get_recent_events_filtered_by_tunnel(self):
        """특정 터널 이벤트 조회"""
        self.monitor._add_event('t1', 'connected', '연결됨')
        self.monitor._add_event('t2', 'connected', '연결됨')
        self.monitor._add_event('t1', 'disconnected', '연결 끊김')

        events = self.monitor.get_recent_events(tunnel_id='t1')
        assert len(events) == 2
        assert all(e.tunnel_id == 't1' for e in events)

    def test_get_recent_events_newest_first(self):
        """최신순 이벤트 정렬 확인"""
        for i in range(5):
            self.monitor._add_event('t1', 'info', f'event {i}')

        events = self.monitor.get_recent_events()
        # 최신순 정렬 확인 (내림차순)
        for i in range(len(events) - 1):
            assert events[i].timestamp >= events[i + 1].timestamp

    def test_get_recent_events_limit(self):
        """최대 반환 개수 제한"""
        for i in range(10):
            self.monitor._add_event('t1', 'info', f'event {i}')

        events = self.monitor.get_recent_events(limit=3)
        assert len(events) == 3

    def test_max_events_limit(self):
        """최대 이벤트 수 초과 시 오래된 이벤트 제거"""
        from src.core.tunnel_monitor import TunnelMonitor

        monitor = TunnelMonitor(self.mock_engine, max_events=5)

        for i in range(10):
            monitor._add_event('t1', 'info', f'event {i}')

        assert len(monitor._events) == 5

    def test_on_tunnel_connected(self):
        """터널 연결 이벤트 처리 확인"""
        from src.core.tunnel_monitor import TunnelState

        self.monitor.on_tunnel_connected('tunnel1')

        status = self.monitor.get_status('tunnel1')
        assert status.state == TunnelState.CONNECTED
        assert status.connected_at is not None
        assert status.reconnect_count == 0

    def test_on_tunnel_disconnected(self):
        """터널 연결 해제 이벤트 처리 확인"""
        from src.core.tunnel_monitor import TunnelState

        # 먼저 연결 상태로 설정
        self.monitor.on_tunnel_connected('tunnel1')
        # 연결 해제
        self.monitor.on_tunnel_disconnected('tunnel1')

        status = self.monitor.get_status('tunnel1')
        assert status.state == TunnelState.DISCONNECTED
        assert status.connected_at is None
        assert status.latency_ms is None

    def test_on_tunnel_disconnected_with_error(self):
        """에러 메시지와 함께 연결 해제 처리"""
        from src.core.tunnel_monitor import TunnelState

        self.monitor.on_tunnel_disconnected('tunnel1', error="Connection reset")

        status = self.monitor.get_status('tunnel1')
        assert status.state == TunnelState.DISCONNECTED
        assert status.error_message == "Connection reset"

    def test_config_manager_loads_settings(self):
        """config_manager에서 자동 재연결 설정 로드 확인"""
        from src.core.tunnel_monitor import TunnelMonitor

        mock_config = MagicMock()
        mock_config.get_app_setting.side_effect = lambda key, default: {
            'auto_reconnect': False,
            'max_reconnect_attempts': 3
        }.get(key, default)

        monitor = TunnelMonitor(self.mock_engine, config_manager=mock_config)

        assert monitor._auto_reconnect is False
        assert monitor._max_reconnect_attempts == 3

    def test_cleanup_health_connection(self):
        """특정 터널 health check 연결 정리 확인"""
        mock_conn = MagicMock()
        self.monitor._health_connections['tunnel1'] = mock_conn

        self.monitor._cleanup_health_connection('tunnel1')

        assert 'tunnel1' not in self.monitor._health_connections
        mock_conn.close.assert_called_once()

    def test_cleanup_all_health_connections(self):
        """모든 health check 연결 정리 확인"""
        for i in range(3):
            mock_conn = MagicMock()
            self.monitor._health_connections[f'tunnel{i}'] = mock_conn

        self.monitor._cleanup_all_health_connections()

        assert len(self.monitor._health_connections) == 0

    def test_measure_latency_no_config(self):
        """터널 설정 없을 때 latency -1 반환"""
        self.mock_engine.tunnel_configs = {}

        result = self.monitor._measure_latency('no_config_tunnel')
        assert result == -1

    def test_measure_latency_no_db_username(self):
        """DB 사용자명 없을 때 latency -1 반환"""
        self.mock_engine.tunnel_configs = {
            'tunnel1': {'db_username': '', 'connection_mode': 'ssh'}
        }

        result = self.monitor._measure_latency('tunnel1')
        assert result == -1

    def test_attempt_reconnect_exceeds_max(self):
        """최대 재연결 시도 초과 시 ERROR 상태로 전환"""
        from src.core.tunnel_monitor import TunnelStatus, TunnelState

        status = TunnelStatus(tunnel_id='tunnel1')
        status.reconnect_count = 5  # max=5 초과
        self.monitor._max_reconnect_attempts = 5
        self.monitor._statuses['tunnel1'] = status

        # sleep이 포함된 스레드 생성 없이 max 초과 분기만 테스트
        with patch('time.sleep'):
            self.monitor._attempt_reconnect('tunnel1')

        assert status.state == TunnelState.ERROR
        assert '최대' in status.error_message

    def test_set_auto_reconnect_updates_config_manager(self):
        """자동 재연결 설정 변경 시 config_manager 업데이트"""
        mock_config = MagicMock()
        from src.core.tunnel_monitor import TunnelMonitor
        monitor = TunnelMonitor(self.mock_engine, config_manager=mock_config)

        monitor.set_auto_reconnect(False)

        mock_config.set_app_setting.assert_called_with('auto_reconnect', False)
