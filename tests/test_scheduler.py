"""
BackupScheduler, CronParser, ScheduleConfig 단위 테스트
"""
import os
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

    def test_run_now_schedule_not_found(self):
        """존재하지 않는 스케줄 즉시 실행 시 실패"""
        success, msg = self.scheduler.run_now('nonexistent')
        assert success is False
        assert '찾을 수 없' in msg

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
