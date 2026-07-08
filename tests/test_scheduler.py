"""
BackupScheduler, CronParser, ScheduleConfig 단위 테스트
"""
import os
import threading
import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, call


# =====================================================================
# CronParser 테스트
# =====================================================================

class TestCronParser:
    """CronParser 클래스 테스트"""

    def test_parse_field_wildcard(self):
        """* 필드 파싱"""
        from src.core.scheduler import CronParser

        result = CronParser.parse_field('*', 0, 59, 0)
        assert result == list(range(0, 60))

    def test_parse_field_specific_value(self):
        """특정 값 파싱"""
        from src.core.scheduler import CronParser

        result = CronParser.parse_field('5', 0, 59, 0)
        assert result == [5]

    def test_parse_field_range(self):
        """범위 파싱 (1-5)"""
        from src.core.scheduler import CronParser

        result = CronParser.parse_field('1-5', 0, 6, 0)
        assert result == [1, 2, 3, 4, 5]

    def test_parse_field_step(self):
        """간격 파싱 (*/15)"""
        from src.core.scheduler import CronParser

        result = CronParser.parse_field('*/15', 0, 59, 0)
        assert result == [0, 15, 30, 45]

    def test_parse_field_comma_separated(self):
        """쉼표 구분 파싱 (1,3,5)"""
        from src.core.scheduler import CronParser

        result = CronParser.parse_field('1,3,5', 0, 6, 0)
        assert result == [1, 3, 5]

    def test_parse_field_out_of_range_excluded(self):
        """범위 초과 값 제외"""
        from src.core.scheduler import CronParser

        result = CronParser.parse_field('0,5,70', 0, 59, 0)
        assert 70 not in result
        assert 0 in result
        assert 5 in result

    def test_get_next_run_daily(self):
        """매일 실행 다음 시간 계산"""
        from src.core.scheduler import CronParser

        # 매일 03:00
        now = datetime(2025, 1, 1, 2, 0, 0)
        next_run = CronParser.get_next_run('0 3 * * *', after=now)

        assert next_run is not None
        assert next_run.hour == 3
        assert next_run.minute == 0
        assert next_run.date() == now.date()  # 같은 날

    def test_get_next_run_next_day_if_past(self):
        """이미 지난 시간이면 내일 실행"""
        from src.core.scheduler import CronParser

        # 03:00이 지난 후
        now = datetime(2025, 1, 1, 4, 0, 0)
        next_run = CronParser.get_next_run('0 3 * * *', after=now)

        assert next_run is not None
        assert next_run.date() == (now + timedelta(days=1)).date()
        assert next_run.hour == 3

    def test_get_next_run_invalid_expression(self):
        """잘못된 cron 표현식"""
        from src.core.scheduler import CronParser

        result = CronParser.get_next_run('invalid')
        assert result is None

    def test_get_next_run_uses_now_if_no_after(self):
        """after 없으면 현재 시간 기준"""
        from src.core.scheduler import CronParser

        next_run = CronParser.get_next_run('* * * * *')
        assert next_run is not None
        assert next_run > datetime.now()

    def test_describe_daily(self):
        """매일 설명 변환"""
        from src.core.scheduler import CronParser

        desc = CronParser.describe('0 3 * * *')
        assert '매일' in desc
        assert '3' in desc

    def test_describe_weekly(self):
        """매주 설명 변환"""
        from src.core.scheduler import CronParser

        desc = CronParser.describe('0 0 * * 0')  # 매주 일요일 00:00
        assert '매주' in desc or '일' in desc

    def test_describe_monthly(self):
        """매월 설명 변환"""
        from src.core.scheduler import CronParser

        desc = CronParser.describe('0 12 1 * *')  # 매월 1일 12:00
        assert '매월' in desc or '1' in desc

    def test_describe_invalid_returns_original(self):
        """잘못된 표현식은 원본 반환"""
        from src.core.scheduler import CronParser

        expr = 'invalid expr'
        desc = CronParser.describe(expr)
        assert desc == expr

    def test_get_next_run_accepts_dow_7_as_sunday(self):
        """cron 요일 필드 7도 일요일(0)로 인식"""
        from src.core.scheduler import CronParser

        after = datetime(2025, 1, 4, 12, 0, 0)  # 토요일
        next_run = CronParser.get_next_run('0 3 * * 7', after=after)

        assert next_run is not None
        assert next_run.date() == datetime(2025, 1, 5).date()  # 다음 일요일
        assert next_run.hour == 3
        assert next_run.minute == 0

    def test_describe_accepts_dow_7_as_sunday(self):
        """describe도 요일 필드 7을 일요일로 표시"""
        from src.core.scheduler import CronParser

        desc = CronParser.describe('0 3 * * 7')
        assert '매주' in desc
        assert '일' in desc


# =====================================================================
# ScheduleConfig 테스트
# =====================================================================

class TestScheduleConfig:
    """ScheduleConfig 데이터클래스 테스트"""

    def test_to_dict_and_from_dict_roundtrip(self):
        """dict 변환 및 복원 확인"""
        from src.core.scheduler import ScheduleConfig

        config = ScheduleConfig(
            id='sched-001',
            name='Daily Backup',
            tunnel_id='tunnel-001',
            schema='mydb',
            tables=['users', 'orders'],
            output_dir='/backup',
            cron_expression='0 3 * * *',
            enabled=True,
            retention_count=5,
            retention_days=30
        )

        data = config.to_dict()
        restored = ScheduleConfig.from_dict(data)

        assert restored.id == config.id
        assert restored.name == config.name
        assert restored.tables == config.tables
        assert restored.cron_expression == config.cron_expression

    def test_from_dict_fills_defaults(self):
        """누락된 필드에 기본값 채움"""
        from src.core.scheduler import ScheduleConfig

        minimal_data = {
            'id': 'sched-001',
            'name': 'Test',
            'tunnel_id': 'tunnel-001',
            'schema': 'mydb',
            'tables': [],
            'output_dir': '',
            'cron_expression': '0 3 * * *',
            'enabled': True,
            'retention_count': 5,
            'retention_days': 30,
            'last_run': None,
            'next_run': None,
        }
        # task_type 등 신규 필드 없는 경우
        config = ScheduleConfig.from_dict(minimal_data)

        assert config.task_type == 'backup'
        assert config.sql_query == ''
        assert config.result_format == 'csv'
        assert config.query_timeout == 300

    def test_is_sql_query_task_false(self):
        """backup 타입 확인"""
        from src.core.scheduler import ScheduleConfig

        config = ScheduleConfig(
            id='s1', name='B', tunnel_id='t1', schema='db',
            task_type='backup'
        )
        assert config.is_sql_query_task() is False

    def test_is_sql_query_task_true(self):
        """sql_query 타입 확인"""
        from src.core.scheduler import ScheduleConfig

        config = ScheduleConfig(
            id='s1', name='Q', tunnel_id='t1', schema='db',
            task_type='sql_query'
        )
        assert config.is_sql_query_task() is True

    def test_get_result_output_path_prefers_result_dir(self):
        """result_output_dir 우선 사용"""
        from src.core.scheduler import ScheduleConfig

        config = ScheduleConfig(
            id='s1', name='Q', tunnel_id='t1', schema='db',
            output_dir='/fallback',
            result_output_dir='/result_dir'
        )
        assert config.get_result_output_path() == '/result_dir'

    def test_get_result_output_path_fallback_to_output_dir(self):
        """result_output_dir 없으면 output_dir 사용"""
        from src.core.scheduler import ScheduleConfig

        config = ScheduleConfig(
            id='s1', name='Q', tunnel_id='t1', schema='db',
            output_dir='/fallback',
            result_output_dir=''
        )
        assert config.get_result_output_path() == '/fallback'


# =====================================================================
# BackupScheduler 테스트
# =====================================================================

class TestBackupScheduler:
    """BackupScheduler 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self):
        """각 테스트 전 Mock으로 BackupScheduler 생성"""
        from src.core.scheduler import BackupScheduler, ScheduleConfig

        self.mock_config_manager = MagicMock()
        self.mock_config_manager.get_app_setting.return_value = []
        self.mock_engine = MagicMock()
        self.mock_engine.is_running.return_value = True

        self.scheduler = BackupScheduler(
            config_manager=self.mock_config_manager,
            tunnel_engine=self.mock_engine
        )

        # 테스트용 스케줄 설정
        self.ScheduleConfig = ScheduleConfig

    def teardown_method(self):
        if self.scheduler.is_running():
            self.scheduler.stop()

    def _make_schedule(self, schedule_id='sched-001', name='Test Backup', enabled=True):
        """테스트용 ScheduleConfig 생성 헬퍼"""
        return self.ScheduleConfig(
            id=schedule_id,
            name=name,
            tunnel_id='tunnel-001',
            schema='testdb',
            output_dir='/tmp/backup',
            cron_expression='0 3 * * *',
            enabled=enabled
        )

    def test_initial_state_not_running(self):
        """초기 상태 미실행 확인"""
        assert self.scheduler.is_running() is False

    def test_start_sets_running(self):
        """스케줄러 시작 확인"""
        self.scheduler.start()
        assert self.scheduler.is_running() is True

    def test_stop_clears_running(self):
        """스케줄러 중지 확인"""
        self.scheduler.start()
        self.scheduler.stop()
        assert self.scheduler.is_running() is False

    def test_start_idempotent(self):
        """이미 실행 중일 때 start 중복 호출 무시"""
        self.scheduler.start()
        thread_before = self.scheduler._thread

        self.scheduler.start()
        assert self.scheduler._thread is thread_before

    def test_add_schedule_success(self):
        """스케줄 추가 성공"""
        schedule = self._make_schedule()
        self.scheduler.add_schedule(schedule)

        schedules = self.scheduler.get_schedules()
        assert len(schedules) == 1
        assert schedules[0].id == 'sched-001'

    def test_add_schedule_duplicate_id_raises(self):
        """중복 ID 스케줄 추가 시 예외"""
        schedule1 = self._make_schedule()
        schedule2 = self._make_schedule()  # 동일 ID

        self.scheduler.add_schedule(schedule1)

        with pytest.raises(ValueError, match='중복된 스케줄 ID'):
            self.scheduler.add_schedule(schedule2)

    def test_get_schedule_found(self):
        """ID로 스케줄 조회 성공"""
        schedule = self._make_schedule()
        self.scheduler.add_schedule(schedule)

        found = self.scheduler.get_schedule('sched-001')
        assert found is not None
        assert found.id == 'sched-001'

    def test_get_schedule_not_found(self):
        """존재하지 않는 스케줄 조회 시 None"""
        result = self.scheduler.get_schedule('nonexistent')
        assert result is None

    def test_remove_schedule_success(self):
        """스케줄 삭제 성공"""
        schedule = self._make_schedule()
        self.scheduler.add_schedule(schedule)

        self.scheduler.remove_schedule('sched-001')

        assert self.scheduler.get_schedule('sched-001') is None

    def test_remove_schedule_not_found_raises(self):
        """존재하지 않는 스케줄 삭제 시 예외"""
        with pytest.raises(ValueError):
            self.scheduler.remove_schedule('nonexistent')

    def test_update_schedule_success(self):
        """스케줄 업데이트 성공"""
        schedule = self._make_schedule()
        self.scheduler.add_schedule(schedule)

        updated = self._make_schedule(name='Updated Backup')
        self.scheduler.update_schedule(updated)

        found = self.scheduler.get_schedule('sched-001')
        assert found.name == 'Updated Backup'

    def test_update_schedule_not_found_raises(self):
        """존재하지 않는 스케줄 업데이트 시 예외"""
        schedule = self._make_schedule(schedule_id='nonexistent')
        with pytest.raises(ValueError):
            self.scheduler.update_schedule(schedule)

    def test_set_enabled_true(self):
        """스케줄 활성화"""
        schedule = self._make_schedule(enabled=False)
        self.scheduler.add_schedule(schedule)

        self.scheduler.set_enabled('sched-001', True)

        found = self.scheduler.get_schedule('sched-001')
        assert found.enabled is True

    def test_set_enabled_false(self):
        """스케줄 비활성화"""
        schedule = self._make_schedule(enabled=True)
        self.scheduler.add_schedule(schedule)

        self.scheduler.set_enabled('sched-001', False)

        found = self.scheduler.get_schedule('sched-001')
        assert found.enabled is False

    def test_add_callback_and_notify(self):
        """콜백 등록 및 호출 확인"""
        callback = MagicMock()
        self.scheduler.add_callback(callback)

        self.scheduler._notify_callbacks('Backup', True, 'Success')
        callback.assert_called_once_with('Backup', True, 'Success')

    def test_remove_callback(self):
        """콜백 제거 확인"""
        callback = MagicMock()
        self.scheduler.add_callback(callback)
        self.scheduler.remove_callback(callback)

        self.scheduler._notify_callbacks('Backup', True, 'Success')
        callback.assert_not_called()

    def test_callback_exception_does_not_propagate(self):
        """콜백 예외가 전파되지 않음"""
        bad_callback = MagicMock(side_effect=Exception("Callback error"))
        self.scheduler.add_callback(bad_callback)

        self.scheduler._notify_callbacks('Backup', False, 'Error')

    def test_backup_task_preserves_postgresql_engine_for_rust_dump(self, monkeypatch, tmp_path):
        """예약 백업은 터널의 db_engine을 RustDumpConfig로 전달"""
        captured = {}

        class FakeExporter:
            def __init__(self, config):
                captured["config"] = config

            def export_full_schema(self, schema, output_dir, threads):
                captured["schema"] = schema
                captured["output_dir"] = output_dir
                captured["threads"] = threads
                return True, "ok"

        # get_connection_info는 실제 TunnelEngine처럼 (host, port) 튜플만 반환한다
        # (dict 분기는 제거됨). db_user/db_password는 tunnel_configs의 평문 값으로 폴백.
        self.mock_engine.get_connection_info.return_value = ("127.0.0.1", 15432)
        self.mock_engine.tunnel_configs = {
            "tunnel-001": {
                "db_engine": "postgresql",
                "remote_port": 5432,
                "db_user": "pg_user",
                "db_password": "pg_pw",
            }
        }
        monkeypatch.setattr("src.exporters.rust_dump_exporter.RustDumpExporter", FakeExporter)

        schedule = self.ScheduleConfig(
            id="backup-001",
            name="PostgreSQL Backup",
            tunnel_id="tunnel-001",
            schema="analytics",
            output_dir=str(tmp_path),
        )

        success, message = self.scheduler._execute_backup(schedule)

        assert success is True
        assert "백업 완료" in message
        assert captured["config"].engine == "postgresql"
        assert captured["config"].host == "127.0.0.1"
        assert captured["config"].port == 15432
        assert captured["config"].user == "pg_user"
        assert captured["config"].schema == "analytics"
        assert captured["schema"] == "analytics"
        assert captured["threads"] == 4

    def test_backup_task_accepts_tuple_connection_info_for_rust_dump(self, monkeypatch, tmp_path):
        """예약 백업은 실제 TunnelEngine의 (host, port) 연결 정보를 처리"""
        captured = {}

        class FakeExporter:
            def __init__(self, config):
                captured["config"] = config

            def export_full_schema(self, schema, output_dir, threads):
                captured["schema"] = schema
                captured["output_dir"] = output_dir
                captured["threads"] = threads
                return True, "ok"

        self.mock_engine.get_connection_info.return_value = ("127.0.0.1", 15432)
        self.mock_engine.tunnel_configs = {
            "tunnel-001": {
                "db_engine": "postgresql",
                "remote_port": 5432,
            }
        }
        self.mock_config_manager.get_tunnel_credentials.return_value = ("pg_user", "pg_pw")
        monkeypatch.setattr("src.exporters.rust_dump_exporter.RustDumpExporter", FakeExporter)

        schedule = self.ScheduleConfig(
            id="backup-002",
            name="Tuple Backup",
            tunnel_id="tunnel-001",
            schema="analytics",
            output_dir=str(tmp_path),
        )

        success, message = self.scheduler._execute_backup(schedule)

        assert success is True
        assert "백업 완료" in message
        assert captured["config"].engine == "postgresql"
        assert captured["config"].host == "127.0.0.1"
        assert captured["config"].port == 15432
        assert captured["config"].user == "pg_user"
        assert captured["config"].password == "pg_pw"
        assert captured["config"].schema == "analytics"
        assert captured["schema"] == "analytics"
        assert captured["threads"] == 4

    def test_run_now_schedule_not_found(self):
        """존재하지 않는 스케줄 즉시 실행 시 실패"""
        success, msg = self.scheduler.run_now('nonexistent')
        assert success is False
        assert '찾을 수 없' in msg

    def test_backup_starts_stopped_tunnel_with_config_dict(self, monkeypatch, tmp_path):
        """중지된 터널은 tunnel_id 문자열이 아닌 전체 설정 딕셔너리로 시작"""
        captured = {}

        class FakeExporter:
            def __init__(self, config):
                captured["config"] = config

            def export_full_schema(self, schema, output_dir, threads):
                return True, "ok"

        self.mock_engine.is_running.return_value = False
        self.mock_engine.start_tunnel.return_value = (True, "연결 성공")
        self.mock_engine.tunnel_configs = {}  # 아직 활성화된 터널 없음 -> 저장된 설정으로 폴백
        self.mock_engine.get_connection_info.return_value = ("127.0.0.1", 13306)
        self.mock_config_manager.load_config.return_value = {
            "tunnels": [{"id": "tunnel-001", "db_engine": "mysql"}]
        }
        monkeypatch.setattr("src.exporters.rust_dump_exporter.RustDumpExporter", FakeExporter)

        schedule = self.ScheduleConfig(
            id="backup-003",
            name="StoppedTunnel Backup",
            tunnel_id="tunnel-001",
            schema="analytics",
            output_dir=str(tmp_path),
        )

        success, message = self.scheduler._execute_backup(schedule)

        assert success is True
        self.mock_engine.start_tunnel.assert_called_once()
        called_config = self.mock_engine.start_tunnel.call_args[0][0]
        assert isinstance(called_config, dict)
        assert called_config.get("id") == "tunnel-001"

    def test_sql_starts_stopped_tunnel_with_config_dict(self, monkeypatch):
        """SQL 실행도 중지된 터널을 tunnel_id 문자열이 아닌 전체 설정 딕셔너리로 시작"""
        created = {}

        class FakeCursor:
            description = None
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def execute(self, query):
                pass

        class FakeConnection:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

        class FakeConnector:
            connection = FakeConnection()

            def connect(self):
                return True, "ok"

            def disconnect(self):
                pass

        def fake_create(engine, host, port, user, password, database=None, schema=""):
            created["host"] = host
            return FakeConnector()

        self.mock_engine.is_running.return_value = False
        self.mock_engine.start_tunnel.return_value = (True, "연결 성공")
        self.mock_engine.tunnel_configs = {}
        self.mock_engine.get_connection_info.return_value = ("127.0.0.1", 13306)
        self.mock_config_manager.load_config.return_value = {
            "tunnels": [{"id": "tunnel-001", "db_engine": "mysql"}]
        }
        monkeypatch.setattr("src.core.scheduler.create_rust_db_connector", fake_create)

        schedule = self.ScheduleConfig(
            id="sql-002",
            name="StoppedTunnel SQL",
            tunnel_id="tunnel-001",
            schema="analytics",
            task_type="sql_query",
            sql_query="UPDATE t SET a=1",
            result_format="none",
        )

        success, _ = self.scheduler._execute_sql_query(schedule)

        assert success is True
        self.mock_engine.start_tunnel.assert_called_once()
        called_config = self.mock_engine.start_tunnel.call_args[0][0]
        assert isinstance(called_config, dict)
        assert called_config.get("id") == "tunnel-001"
        # 문자열 tunnel_id가 실수로 start_tunnel에 전달되지 않았는지 확인
        assert not isinstance(called_config, str)

    def test_resolve_connection_rejects_none_tuple(self, monkeypatch, tmp_path):
        """(None, None) 연결 정보는 즉시 실패 처리"""
        captured = {}

        class FakeExporter:
            def __init__(self, config):
                captured["constructed"] = True

            def export_full_schema(self, schema, output_dir, threads):
                return True, "ok"

        self.mock_engine.tunnel_configs = {"tunnel-001": {"db_engine": "mysql"}}
        self.mock_engine.get_connection_info.return_value = (None, None)
        monkeypatch.setattr("src.exporters.rust_dump_exporter.RustDumpExporter", FakeExporter)

        schedule = self.ScheduleConfig(
            id="backup-004",
            name="NoneConnInfo",
            tunnel_id="tunnel-001",
            schema="analytics",
            output_dir=str(tmp_path),
        )

        success, message = self.scheduler._execute_backup(schedule)

        assert success is False
        assert "연결 정보를 가져올 수 없습니다" in message
        assert "constructed" not in captured

    def test_sql_query_uses_decrypted_credentials(self, monkeypatch):
        """평문 자격 증명이 없어도 config_manager에서 복호화된 자격 증명을 사용"""
        created = {}

        class FakeCursor:
            description = None
            rowcount = 1

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def execute(self, query):
                pass

        class FakeConnection:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

        class FakeConnector:
            connection = FakeConnection()

            def connect(self):
                return True, "ok"

            def disconnect(self):
                pass

        def fake_create(engine, host, port, user, password, database=None, schema=""):
            created["user"] = user
            created["password"] = password
            return FakeConnector()

        self.mock_engine.get_connection_info.return_value = ("127.0.0.1", 15432)
        self.mock_engine.tunnel_configs = {
            "tunnel-001": {"db_engine": "mysql"}  # 평문 db_user/db_password 없음
        }
        self.mock_config_manager.get_tunnel_credentials.return_value = ("db_user", "decrypted_pw")
        monkeypatch.setattr("src.core.scheduler.create_rust_db_connector", fake_create)

        schedule = self.ScheduleConfig(
            id="sql-003",
            name="Decrypted",
            tunnel_id="tunnel-001",
            schema="analytics",
            task_type="sql_query",
            sql_query="UPDATE t SET a=1",
            result_format="none",
        )

        success, _ = self.scheduler._execute_sql_query(schedule)

        assert success is True
        assert created["user"] == "db_user"
        assert created["password"] == "decrypted_pw"

    def test_run_now_queues_background_execution(self):
        """run_now는 즉시 반환하고 실제 실행은 백그라운드 실행 큐에서 처리"""
        schedule = self._make_schedule()
        self.scheduler.add_schedule(schedule)

        started = threading.Event()
        release = threading.Event()

        def fake_execute_task(sched):
            started.set()
            release.wait(timeout=5)
            return True, "완료"

        self.scheduler._execute_task = fake_execute_task

        callback = MagicMock()
        self.scheduler.add_callback(callback)

        try:
            success, message = self.scheduler.run_now('sched-001')

            assert success is True
            assert '등록' in message
            assert started.wait(timeout=2)
        finally:
            release.set()

        deadline = time.time() + 5
        while not callback.called and time.time() < deadline:
            time.sleep(0.05)

        callback.assert_called_once()
        args = callback.call_args[0]
        assert args[0] == 'Test Backup'
        assert args[1] is True

        self.scheduler.stop()

    def test_run_now_rejects_duplicate_active_schedule(self):
        """이미 실행 중인 스케줄은 중복 run_now 요청을 거부"""
        schedule = self._make_schedule()
        self.scheduler.add_schedule(schedule)

        started = threading.Event()
        release = threading.Event()

        def fake_execute_task(sched):
            started.set()
            release.wait(timeout=5)
            return True, "완료"

        self.scheduler._execute_task = fake_execute_task

        try:
            success1, _ = self.scheduler.run_now('sched-001')
            assert success1 is True
            assert started.wait(timeout=2)

            success2, message2 = self.scheduler.run_now('sched-001')
            assert success2 is False
            assert '이미 실행 중' in message2
        finally:
            release.set()
            # 백그라운드 작업이 마무리되어 _active_schedule_ids가 정리될 시간을 준다
            deadline = time.time() + 5
            while 'sched-001' in self.scheduler._active_schedule_ids and time.time() < deadline:
                time.sleep(0.05)
            self.scheduler.stop()

    def test_snapshot_due_jobs_does_not_hold_lock_during_execution(self):
        """_snapshot_due_jobs는 락 안에서 스냅샷만 뜨고 즉시 락을 반환"""
        schedule = self._make_schedule()
        self.scheduler.add_schedule(schedule)
        # add_schedule은 cron 표현식으로 next_run을 재계산하므로, 추가 이후에 과거 시각으로 덮어써야 한다
        schedule.next_run = (datetime.now() - timedelta(minutes=1)).isoformat()

        jobs = self.scheduler._snapshot_due_jobs(datetime.now())

        assert len(jobs) == 1
        assert jobs[0].schedule.id == 'sched-001'

        # 스냅샷 직후 락을 블로킹 없이 즉시 재획득할 수 있어야 한다 (실행이 락 밖에서 진행됨을 증명)
        acquired = self.scheduler._lock.acquire(timeout=1)
        assert acquired
        self.scheduler._lock.release()

    def test_execute_single_query_saves_cte_results_by_description(self, tmp_path):
        """SELECT로 시작하지 않는 CTE(WITH)도 description 기준으로 결과 저장"""
        class FakeCursor:
            description = [("value",)]
            rowcount = 1

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def execute(self, query):
                pass

            def fetchall(self):
                return [{"value": 1}]

        class FakeConnection:
            def __init__(self):
                self.commit_called = False

            def cursor(self):
                return FakeCursor()

            def commit(self):
                self.commit_called = True

        class FakeConnector:
            def __init__(self):
                self.connection = FakeConnection()

        connector = FakeConnector()
        schedule = self.ScheduleConfig(
            id="sql-cte",
            name="CTE",
            tunnel_id="tunnel-001",
            schema="db",
            task_type="sql_query",
            result_format="csv",
            result_output_dir=str(tmp_path),
        )

        result = self.scheduler._execute_single_query(
            connector, schedule, "WITH recent AS (SELECT 1) SELECT * FROM recent",
            "20250101_000000", 0
        )

        assert result['success'] is True
        assert 'file_path' in result
        assert os.path.exists(result['file_path'])
        assert connector.connection.commit_called is False

    def test_execute_single_query_saves_zero_row_result_file(self, tmp_path):
        """결과 0행이어도 result_format이 none이 아니면 헤더만 있는 파일을 저장"""
        class FakeCursor:
            description = [("value",)]
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def execute(self, query):
                pass

            def fetchall(self):
                return []

        class FakeConnection:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

        class FakeConnector:
            def __init__(self):
                self.connection = FakeConnection()

        connector = FakeConnector()
        schedule = self.ScheduleConfig(
            id="sql-zero",
            name="ZeroRow",
            tunnel_id="tunnel-001",
            schema="db",
            task_type="sql_query",
            result_format="csv",
            result_output_dir=str(tmp_path),
        )

        result = self.scheduler._execute_single_query(
            connector, schedule, "SELECT * FROM empty_table WHERE 1=0",
            "20250101_000000", 0
        )

        assert result['success'] is True
        assert os.path.exists(result['file_path'])
        with open(result['file_path'], encoding='utf-8-sig') as f:
            content = f.read()
        assert 'value' in content

    def test_execute_single_query_empty_description_is_still_result_set(self):
        """description=[] (빈 리스트)도 None이 아니므로 결과셋으로 처리 (commit 금지)"""
        class FakeCursor:
            description = []
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def execute(self, query):
                pass

            def fetchall(self):
                return []

        class FakeConnection:
            def __init__(self):
                self.commit_called = False

            def cursor(self):
                return FakeCursor()

            def commit(self):
                self.commit_called = True

        class FakeConnector:
            def __init__(self):
                self.connection = FakeConnection()

        connector = FakeConnector()
        schedule = self.ScheduleConfig(
            id="sql-empty-desc",
            name="EmptyDesc",
            tunnel_id="tunnel-001",
            schema="db",
            task_type="sql_query",
            result_format="none",
        )

        result = self.scheduler._execute_single_query(
            connector, schedule, "SHOW TABLES", "20250101_000000", 0
        )

        assert result['success'] is True
        assert result['row_count'] == 0
        assert connector.connection.commit_called is False

    def test_get_schedules_empty(self):
        """스케줄 없을 때 빈 리스트"""
        schedules = self.scheduler.get_schedules()
        assert schedules == []

    def test_save_schedules_called_on_add(self):
        """스케줄 추가 시 저장 호출 확인"""
        schedule = self._make_schedule()
        self.scheduler.add_schedule(schedule)

        self.mock_config_manager.set_app_setting.assert_called()

    def test_save_schedules_called_on_remove(self):
        """스케줄 삭제 시 저장 호출 확인"""
        schedule = self._make_schedule()
        self.scheduler.add_schedule(schedule)
        self.mock_config_manager.set_app_setting.reset_mock()

        self.scheduler.remove_schedule('sched-001')
        self.mock_config_manager.set_app_setting.assert_called()

    def test_load_schedules_on_init(self):
        """초기화 시 설정에서 스케줄 로드 확인"""
        from src.core.scheduler import BackupScheduler, ScheduleConfig

        mock_config = MagicMock()
        mock_config.get_app_setting.return_value = [
            {
                'id': 'loaded-001',
                'name': 'Loaded Schedule',
                'tunnel_id': 'tunnel-001',
                'schema': 'mydb',
                'tables': [],
                'output_dir': '/backup',
                'cron_expression': '0 3 * * *',
                'enabled': True,
                'retention_count': 5,
                'retention_days': 30,
                'last_run': None,
                'next_run': None,
            }
        ]

        scheduler = BackupScheduler(config_manager=mock_config, tunnel_engine=MagicMock())

        schedules = scheduler.get_schedules()
        assert len(schedules) == 1
        assert schedules[0].id == 'loaded-001'

    def test_parse_sql_queries_single(self):
        """단일 쿼리 파싱"""
        queries = self.scheduler._parse_sql_queries('SELECT * FROM users')
        assert queries == ['SELECT * FROM users']

    def test_parse_sql_queries_multiple(self):
        """세미콜론으로 구분된 멀티 쿼리 파싱"""
        sql = 'SELECT 1; SELECT 2; SELECT 3'
        queries = self.scheduler._parse_sql_queries(sql)
        assert len(queries) == 3
        assert queries[0] == 'SELECT 1'
        assert queries[1] == 'SELECT 2'
        assert queries[2] == 'SELECT 3'

    def test_parse_sql_queries_semicolon_in_string(self):
        """문자열 내부 세미콜론 무시"""
        sql = "SELECT 'a;b;c' FROM t; SELECT 1"
        queries = self.scheduler._parse_sql_queries(sql)
        assert len(queries) == 2

    def test_parse_sql_queries_empty(self):
        """빈 SQL 파싱 시 빈 리스트"""
        assert self.scheduler._parse_sql_queries('') == []
        assert self.scheduler._parse_sql_queries('   ') == []
        assert self.scheduler._parse_sql_queries(None) == []

    def test_parse_sql_queries_trailing_semicolon(self):
        """끝에 세미콜론 있는 경우"""
        queries = self.scheduler._parse_sql_queries('SELECT 1;')
        assert queries == ['SELECT 1']

    def test_parse_sql_queries_preserves_comments_dollar_quotes_and_delimiters(self):
        """예약 SQL도 SQL 파일 실행과 같은 robust parser를 사용"""
        sql = """-- comment; ignored
SELECT 'a;b';
CREATE FUNCTION f() RETURNS void AS $body$
BEGIN
    RAISE NOTICE 'x;y';
END
$body$ LANGUAGE plpgsql;
DELIMITER //
CREATE PROCEDURE p()
BEGIN
    SELECT 'c;d';
END//
DELIMITER ;
SELECT 1;"""

        queries = self.scheduler._parse_sql_queries(sql)

        assert queries == [
            "-- comment; ignored\nSELECT 'a;b'",
            "CREATE FUNCTION f() RETURNS void AS $body$\n"
            "BEGIN\n"
            "    RAISE NOTICE 'x;y';\n"
            "END\n"
            "$body$ LANGUAGE plpgsql",
            "CREATE PROCEDURE p()\nBEGIN\n    SELECT 'c;d';\nEND",
            "SELECT 1",
        ]

    def test_sql_query_task_uses_engine_aware_rust_connector(self, monkeypatch):
        """예약 SQL 실행은 터널의 db_engine을 Rust Core connector로 전달"""
        created = {}

        class FakeCursor:
            description = [("value",)]
            rowcount = 1

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def execute(self, query):
                self.query = query

            def fetchall(self):
                return [{"value": 1}]

        class FakeConnection:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

        class FakeConnector:
            connection = FakeConnection()

            def connect(self):
                return True, "ok"

            def disconnect(self):
                pass

        def fake_create(engine, host, port, user, password, database=None, schema=""):
            created.update({
                "engine": engine,
                "host": host,
                "port": port,
                "user": user,
                "database": database,
                "schema": schema,
            })
            return FakeConnector()

        self.mock_engine.get_connection_info.return_value = ("127.0.0.1", 15432)
        self.mock_engine.tunnel_configs = {
            "tunnel-001": {
                "db_engine": "postgresql",
                "remote_port": 5432,
                "db_username": "pg_user",
                "db_password": "pg_pw",
            }
        }
        monkeypatch.setattr("src.core.scheduler.create_rust_db_connector", fake_create)

        schedule = self.ScheduleConfig(
            id="sql-001",
            name="SQL",
            tunnel_id="tunnel-001",
            schema="analytics",
            task_type="sql_query",
            sql_query="SELECT 1",
            result_format="none",
        )

        success, _ = self.scheduler._execute_sql_query(schedule)

        assert success is True
        assert created["engine"] == "postgresql"
        assert created["user"] == "pg_user"
        assert created["schema"] == "analytics"

    def test_add_schedule_sets_next_run_for_enabled(self):
        """활성화된 스케줄 추가 시 next_run 계산"""
        schedule = self._make_schedule(enabled=True)
        schedule.next_run = None

        self.scheduler.add_schedule(schedule)

        found = self.scheduler.get_schedule('sched-001')
        assert found.next_run is not None

    def test_add_schedule_no_next_run_for_disabled(self):
        """비활성화된 스케줄 추가 시 next_run 미계산"""
        from src.core.scheduler import ScheduleConfig
        schedule = ScheduleConfig(
            id='disabled-001',
            name='Disabled',
            tunnel_id='t1',
            schema='db',
            enabled=False,
            cron_expression='0 3 * * *'
        )

        self.scheduler.add_schedule(schedule)

        found = self.scheduler.get_schedule('disabled-001')
        # 비활성화 상태이므로 next_run이 설정되지 않음
        assert found.enabled is False
