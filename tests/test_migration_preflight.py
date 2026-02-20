"""
migration_preflight.py 단위 테스트

CheckSeverity, CheckResult, PreflightResult, PreflightChecker 검증.
DB 의존 메서드는 MagicMock으로 커넥터를 대체하여 격리.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.core.migration_preflight import (
    CheckSeverity,
    CheckResult,
    PreflightResult,
    PreflightChecker,
)


# ============================================================
# 헬퍼
# ============================================================
def make_connector(**kwargs):
    """기본값을 가진 Mock 커넥터 생성"""
    conn = MagicMock()
    conn.get_db_version.return_value = (8, 0, 32)
    conn.get_db_version_string.return_value = "8.0.32"
    conn.execute.return_value = []
    # 키워드로 개별 속성 오버라이드 가능
    for k, v in kwargs.items():
        setattr(conn, k, v)
    return conn


def make_checker(**kwargs):
    """Mock 커넥터를 사용하는 PreflightChecker"""
    conn = make_connector(**kwargs)
    return PreflightChecker(conn), conn


# ============================================================
# CheckSeverity 열거형 테스트
# ============================================================
class TestCheckSeverity:
    def test_values(self):
        assert CheckSeverity.ERROR.value == "error"
        assert CheckSeverity.WARNING.value == "warning"
        assert CheckSeverity.INFO.value == "info"

    def test_all_distinct(self):
        vals = [s.value for s in CheckSeverity]
        assert len(set(vals)) == len(vals)


# ============================================================
# CheckResult 테스트
# ============================================================
class TestCheckResult:
    def test_severity_str_error(self):
        cr = CheckResult(
            name="x", passed=False,
            severity=CheckSeverity.ERROR, message="bad"
        )
        assert cr.severity_str == "error"

    def test_severity_str_warning(self):
        cr = CheckResult(
            name="x", passed=False,
            severity=CheckSeverity.WARNING, message="warn"
        )
        assert cr.severity_str == "warning"

    def test_severity_str_info(self):
        cr = CheckResult(
            name="x", passed=True,
            severity=CheckSeverity.INFO, message="ok"
        )
        assert cr.severity_str == "info"

    def test_details_optional(self):
        cr = CheckResult(name="x", passed=True, severity=CheckSeverity.INFO, message="ok")
        assert cr.details is None

    def test_with_details(self):
        cr = CheckResult(
            name="x", passed=True,
            severity=CheckSeverity.INFO, message="ok", details="extra info"
        )
        assert cr.details == "extra info"


# ============================================================
# PreflightResult 테스트
# ============================================================
class TestPreflightResult:
    def _make_check(self, passed, severity):
        return CheckResult(
            name="check", passed=passed, severity=severity, message="msg"
        )

    def test_error_count_empty(self):
        r = PreflightResult(passed=True)
        assert r.error_count == 0

    def test_error_count_counts_errors(self):
        r = PreflightResult(passed=False, checks=[
            self._make_check(False, CheckSeverity.ERROR),
            self._make_check(False, CheckSeverity.ERROR),
            self._make_check(False, CheckSeverity.WARNING),
            self._make_check(True, CheckSeverity.INFO),
        ])
        assert r.error_count == 2

    def test_error_count_passed_error_not_counted(self):
        # passed=True이지만 severity=ERROR인 경우는 카운트 안 됨
        r = PreflightResult(passed=True, checks=[
            self._make_check(True, CheckSeverity.ERROR),
        ])
        assert r.error_count == 0

    def test_warning_count_all_severities(self):
        r = PreflightResult(passed=True, checks=[
            self._make_check(True, CheckSeverity.WARNING),
            self._make_check(False, CheckSeverity.WARNING),
            self._make_check(True, CheckSeverity.INFO),
            self._make_check(False, CheckSeverity.ERROR),
        ])
        assert r.warning_count == 2

    def test_get_summary_passed(self):
        r = PreflightResult(passed=True, checks=[
            self._make_check(True, CheckSeverity.INFO),
            self._make_check(True, CheckSeverity.WARNING),
        ])
        summary = r.get_summary()
        assert "통과" in summary
        assert "2" in summary  # 총 2개 검사

    def test_get_summary_failed(self):
        r = PreflightResult(
            passed=False,
            checks=[
                self._make_check(False, CheckSeverity.ERROR),
                self._make_check(False, CheckSeverity.WARNING),
            ],
            errors=["error1"],
        )
        summary = r.get_summary()
        assert "실패" in summary

    def test_defaults(self):
        r = PreflightResult(passed=True)
        assert r.checks == []
        assert r.warnings == []
        assert r.errors == []
        assert r.estimated_time == timedelta(seconds=0)


# ============================================================
# PreflightChecker - _parse_grants
# ============================================================
class TestParseGrants:
    @pytest.fixture
    def checker(self):
        checker, _ = make_checker()
        return checker

    def _grant(self, s):
        """GRANT 결과 형식으로 래핑"""
        return [{"Grants for current_user()": s}]

    def test_all_privileges_global(self, checker):
        grants = self._grant("GRANT ALL PRIVILEGES ON *.* TO 'user'@'localhost'")
        result = checker._parse_grants(grants, "testdb")
        # ALL PRIVILEGES → REQUIRED_PRIVILEGES 전부 포함
        assert checker.REQUIRED_PRIVILEGES.issubset(result)

    def test_global_specific_privs(self, checker):
        grants = self._grant("GRANT SELECT, INSERT, ALTER ON *.* TO 'user'@'localhost'")
        result = checker._parse_grants(grants, "testdb")
        assert "SELECT" in result
        assert "INSERT" in result
        assert "ALTER" in result

    def test_schema_specific_privs(self, checker):
        grants = self._grant("GRANT SELECT, UPDATE ON `testdb`.* TO 'user'@'localhost'")
        result = checker._parse_grants(grants, "testdb")
        assert "SELECT" in result
        assert "UPDATE" in result

    def test_schema_specific_no_match(self, checker):
        grants = self._grant("GRANT SELECT ON `otherdb`.* TO 'user'@'localhost'")
        result = checker._parse_grants(grants, "testdb")
        # 다른 스키마 권한은 포함 안 됨
        assert "SELECT" not in result

    def test_empty_grants(self, checker):
        result = checker._parse_grants([], "testdb")
        assert result == set()

    def test_empty_grant_row(self, checker):
        result = checker._parse_grants([{}], "testdb")
        assert result == set()

    def test_multiple_grants(self, checker):
        grants = [
            {"Grants": "GRANT SELECT ON *.* TO 'u'@'%'"},
            {"Grants": "GRANT ALTER ON `testdb`.* TO 'u'@'%'"},
        ]
        result = checker._parse_grants(grants, "testdb")
        assert "SELECT" in result
        assert "ALTER" in result


# ============================================================
# PreflightChecker - check_backup_status (DB 무관)
# ============================================================
class TestCheckBackupStatus:
    @pytest.fixture
    def checker(self):
        checker, _ = make_checker()
        return checker

    def test_confirmed(self, checker):
        result = checker.check_backup_status(confirmed=True)
        assert result.passed is True
        assert result.severity == CheckSeverity.INFO
        assert "확인" in result.message

    def test_not_confirmed(self, checker):
        result = checker.check_backup_status(confirmed=False)
        assert result.passed is False
        assert result.severity == CheckSeverity.WARNING
        assert "미확인" in result.message

    def test_default_is_not_confirmed(self, checker):
        result = checker.check_backup_status()
        assert result.passed is False


# ============================================================
# PreflightChecker - estimate_time
# ============================================================
class TestEstimateTime:
    @pytest.fixture
    def checker(self):
        checker, _ = make_checker()
        return checker

    def test_minimum_30_seconds(self, checker):
        result = checker.estimate_time(0, 0)
        assert result.total_seconds() >= 30

    def test_scales_with_issues(self, checker):
        t1 = checker.estimate_time(10, 0)
        t2 = checker.estimate_time(20, 0)
        assert t2 > t1

    def test_large_tables_add_time(self, checker):
        t_no_large = checker.estimate_time(5, 0)
        t_with_large = checker.estimate_time(5, 3)
        assert t_with_large > t_no_large

    def test_formula(self, checker):
        # 10 issues × 5s + 2 large tables × 30s = 110s
        result = checker.estimate_time(10, 2)
        assert result.total_seconds() == 110

    def test_minimum_when_formula_below_30(self, checker):
        # 1 issue × 5s = 5s < 30s minimum
        result = checker.estimate_time(1, 0)
        assert result.total_seconds() == 30

    def test_returns_timedelta(self, checker):
        result = checker.estimate_time(5, 1)
        assert isinstance(result, timedelta)


# ============================================================
# PreflightChecker - check_mysql_version
# ============================================================
class TestCheckMysqlVersion:
    def test_version_8_0(self):
        checker, conn = make_checker()
        conn.get_db_version.return_value = (8, 0, 32)
        conn.get_db_version_string.return_value = "8.0.32"

        result = checker.check_mysql_version()
        assert result.passed is True
        assert result.severity == CheckSeverity.INFO
        assert "8.0.32" in result.message
        assert "마이그레이션 대상" in (result.details or "")

    def test_version_8_4(self):
        checker, conn = make_checker()
        conn.get_db_version.return_value = (8, 4, 0)
        conn.get_db_version_string.return_value = "8.4.0"

        result = checker.check_mysql_version()
        assert result.passed is True
        assert "이미" in (result.details or "")

    def test_version_8_1(self):
        """8.1 ~ 8.3: 기타 minor version"""
        checker, conn = make_checker()
        conn.get_db_version.return_value = (8, 1, 0)
        conn.get_db_version_string.return_value = "8.1.0"

        result = checker.check_mysql_version()
        assert result.passed is True

    def test_version_below_8(self):
        checker, conn = make_checker()
        conn.get_db_version.return_value = (5, 7, 40)
        conn.get_db_version_string.return_value = "5.7.40"

        result = checker.check_mysql_version()
        assert result.passed is False
        assert result.severity == CheckSeverity.WARNING

    def test_version_exception(self):
        checker, conn = make_checker()
        conn.get_db_version.side_effect = Exception("DB error")

        result = checker.check_mysql_version()
        assert result.passed is True  # 실패해도 경고만
        assert result.severity == CheckSeverity.WARNING
        assert "실패" in result.message


# ============================================================
# PreflightChecker - check_permissions
# ============================================================
class TestCheckPermissions:
    def test_all_privileges(self):
        checker, conn = make_checker()
        conn.execute.return_value = [
            {"Grants for current_user()": "GRANT ALL PRIVILEGES ON *.* TO 'u'@'%'"}
        ]
        result = checker.check_permissions("testdb")
        assert result.passed is True
        assert result.severity == CheckSeverity.INFO

    def test_missing_privileges(self):
        checker, conn = make_checker()
        conn.execute.return_value = [
            {"Grants for current_user()": "GRANT SELECT ON *.* TO 'u'@'%'"}
        ]
        result = checker.check_permissions("testdb")
        assert result.passed is False
        assert result.severity == CheckSeverity.ERROR
        assert "권한 부족" in result.message

    def test_empty_grants(self):
        checker, conn = make_checker()
        conn.execute.return_value = []

        result = checker.check_permissions("testdb")
        assert result.passed is False
        assert result.severity == CheckSeverity.ERROR

    def test_exception_handling(self):
        checker, conn = make_checker()
        conn.execute.side_effect = Exception("connection lost")

        result = checker.check_permissions("testdb")
        assert result.passed is False
        assert result.severity == CheckSeverity.ERROR
        assert "실패" in result.message


# ============================================================
# PreflightChecker - check_disk_space
# ============================================================
class TestCheckDiskSpace:
    def test_empty_schema(self):
        checker, conn = make_checker()
        conn.execute.return_value = [{"size_mb": 0}]

        result = checker.check_disk_space("testdb")
        assert result.passed is True
        assert "비어있거나" in result.message

    def test_small_schema(self):
        checker, conn = make_checker()
        conn.execute.return_value = [{"size_mb": 100.0}]

        result = checker.check_disk_space("testdb")
        assert result.passed is True
        assert result.severity == CheckSeverity.INFO  # 1024 MB 이하

    def test_large_schema_warning(self):
        checker, conn = make_checker()
        conn.execute.return_value = [{"size_mb": 2000.0}]

        result = checker.check_disk_space("testdb")
        assert result.passed is True
        assert result.severity == CheckSeverity.WARNING  # 1024 MB 초과

    def test_none_size(self):
        checker, conn = make_checker()
        conn.execute.return_value = [{"size_mb": None}]

        result = checker.check_disk_space("testdb")
        assert result.passed is True

    def test_exception_handling(self):
        checker, conn = make_checker()
        conn.execute.side_effect = Exception("query failed")

        result = checker.check_disk_space("testdb")
        assert result.passed is True  # 예외 시 경고만
        assert result.severity == CheckSeverity.WARNING


# ============================================================
# PreflightChecker - check_active_connections
# ============================================================
class TestCheckActiveConnections:
    def test_no_other_connections(self):
        checker, conn = make_checker()
        conn.execute.return_value = [
            {"db": "testdb", "Command": "Sleep", "User": "app"}
        ]
        result = checker.check_active_connections("testdb")
        assert result.passed is True
        assert result.severity == CheckSeverity.INFO

    def test_active_connections(self):
        checker, conn = make_checker()
        conn.execute.return_value = [
            {"db": "testdb", "Command": "Query", "User": "app"},
            {"db": "testdb", "Command": "Execute", "User": "worker"},
        ]
        result = checker.check_active_connections("testdb")
        assert result.passed is False
        assert result.severity == CheckSeverity.WARNING
        assert "2" in result.message

    def test_other_schema_ignored(self):
        checker, conn = make_checker()
        conn.execute.return_value = [
            {"db": "otherdb", "Command": "Query", "User": "someone"},
        ]
        result = checker.check_active_connections("testdb")
        assert result.passed is True

    def test_empty_process_list(self):
        checker, conn = make_checker()
        conn.execute.return_value = []

        result = checker.check_active_connections("testdb")
        assert result.passed is True

    def test_exception_handling(self):
        checker, conn = make_checker()
        conn.execute.side_effect = Exception("access denied")

        result = checker.check_active_connections("testdb")
        assert result.passed is True  # 예외 시 경고
        assert result.severity == CheckSeverity.WARNING

    def test_details_shows_max_5(self):
        checker, conn = make_checker()
        conn.execute.return_value = [
            {"db": "testdb", "Command": "Query", "User": f"u{i}"}
            for i in range(10)
        ]
        result = checker.check_active_connections("testdb")
        # details에 최대 5개 표시
        if result.details:
            lines = [l for l in result.details.split("\n") if l.strip().startswith("User:")]
            assert len(lines) <= 5


# ============================================================
# PreflightChecker - set_progress_callback / _log
# ============================================================
class TestProgressCallback:
    def test_no_callback_by_default(self):
        checker, _ = make_checker()
        assert checker._progress_callback is None

    def test_set_callback(self):
        checker, _ = make_checker()
        cb = MagicMock()
        checker.set_progress_callback(cb)
        assert checker._progress_callback is cb

    def test_log_calls_callback(self):
        checker, _ = make_checker()
        cb = MagicMock()
        checker.set_progress_callback(cb)
        checker._log("test message")
        cb.assert_called_once_with("test message")

    def test_log_without_callback_no_error(self):
        checker, _ = make_checker()
        checker._log("no callback")  # 오류 없어야 함


# ============================================================
# PreflightChecker - check_all 통합 테스트
# ============================================================
class TestCheckAll:
    def _setup_passing_connector(self):
        conn = MagicMock()
        # get_db_version: 8.0.32
        conn.get_db_version.return_value = (8, 0, 32)
        conn.get_db_version_string.return_value = "8.0.32"
        # execute: 첫 호출(SHOW GRANTS) → ALL PRIVILEGES
        # 두 번째 호출(schema size) → 100MB
        # 세 번째 호출(SHOW PROCESSLIST) → 빈 목록
        def mock_execute(query, *args, **kwargs):
            q = query.strip().upper()
            if "SHOW GRANTS" in q:
                return [{"Grants": "GRANT ALL PRIVILEGES ON *.* TO 'u'@'%'"}]
            elif "SHOW PROCESSLIST" in q:
                return []
            elif "INFORMATION_SCHEMA" in q or "tables" in q.lower():
                return [{"size_mb": 100.0}]
            return []
        conn.execute.side_effect = mock_execute
        return conn

    def test_check_all_all_pass(self):
        conn = self._setup_passing_connector()
        checker = PreflightChecker(conn)

        result = checker.check_all("testdb", backup_confirmed=True)

        assert result.passed is True
        assert len(result.checks) == 5  # 5가지 검사
        assert result.errors == []

    def test_check_all_backup_not_confirmed(self):
        conn = self._setup_passing_connector()
        checker = PreflightChecker(conn)

        result = checker.check_all("testdb", backup_confirmed=False)

        assert result.passed is True  # 백업 미확인은 WARNING이므로 전체 passed=True
        assert len(result.warnings) > 0

    def test_check_all_missing_privs_fails(self):
        conn = MagicMock()
        conn.get_db_version.return_value = (8, 0, 32)
        conn.get_db_version_string.return_value = "8.0.32"

        def mock_execute(query, *args, **kwargs):
            q = query.strip().upper()
            if "SHOW GRANTS" in q:
                return [{"Grants": "GRANT SELECT ON *.* TO 'u'@'%'"}]  # 권한 부족
            elif "SHOW PROCESSLIST" in q:
                return []
            else:
                return [{"size_mb": 50.0}]
        conn.execute.side_effect = mock_execute

        checker = PreflightChecker(conn)
        result = checker.check_all("testdb", backup_confirmed=True)

        assert result.passed is False
        assert len(result.errors) > 0

    def test_check_all_calls_progress_callback(self):
        conn = self._setup_passing_connector()
        checker = PreflightChecker(conn)

        logs = []
        checker.set_progress_callback(lambda msg: logs.append(msg))
        checker.check_all("testdb", backup_confirmed=True)

        assert len(logs) > 0
        assert any("Pre-flight" in log for log in logs)

    def test_check_all_returns_5_checks(self):
        conn = self._setup_passing_connector()
        checker = PreflightChecker(conn)

        result = checker.check_all("testdb", backup_confirmed=True)
        assert len(result.checks) == 5


# ============================================================
# PreflightChecker - _get_schema_size_mb
# ============================================================
class TestGetSchemaSizeMb:
    def test_returns_float(self):
        checker, conn = make_checker()
        conn.execute.return_value = [{"size_mb": 512.5}]

        result = checker._get_schema_size_mb("testdb")
        assert result == 512.5

    def test_returns_zero_when_empty(self):
        checker, conn = make_checker()
        conn.execute.return_value = []

        result = checker._get_schema_size_mb("testdb")
        assert result == 0.0

    def test_returns_zero_when_none(self):
        checker, conn = make_checker()
        conn.execute.return_value = [{"size_mb": None}]

        result = checker._get_schema_size_mb("testdb")
        assert result == 0.0


# ============================================================
# 미커버 경로 추가 테스트
# ============================================================
class TestUncoveredPaths:
    # --- check_all line 122-126: disk_check ERROR 분기 ---
    def test_check_all_disk_error_fails(self):
        """disk_check가 ERROR severity로 실패하면 전체 passed=False"""
        conn = MagicMock()
        conn.get_db_version.return_value = (8, 0, 32)
        conn.get_db_version_string.return_value = "8.0.32"
        conn.execute.return_value = []
        checker = PreflightChecker(conn)

        with patch.object(checker, "check_disk_space", return_value=CheckResult(
            name="disk", passed=False, severity=CheckSeverity.ERROR, message="disk full"
        )):
            result = checker.check_all("testdb", backup_confirmed=True)

        assert result.passed is False
        assert "disk full" in result.errors

    # --- check_all line 126: disk_check WARNING 분기 ---
    def test_check_all_disk_warning_appends_to_warnings(self):
        """disk_check가 WARNING으로 실패하면 warnings에 추가 (passed 유지)"""
        conn = MagicMock()
        conn.get_db_version.return_value = (8, 0, 32)
        conn.get_db_version_string.return_value = "8.0.32"
        conn.execute.return_value = []
        checker = PreflightChecker(conn)

        perm_pass = CheckResult(name="perm", passed=True, severity=CheckSeverity.INFO, message="ok")
        disk_warn = CheckResult(name="disk", passed=False, severity=CheckSeverity.WARNING, message="disk low")
        with patch.object(checker, "check_permissions", return_value=perm_pass), \
             patch.object(checker, "check_disk_space", return_value=disk_warn):
            result = checker.check_all("testdb", backup_confirmed=True)

        assert result.passed is True
        assert "disk low" in result.warnings

    # --- check_all line 133: conn_check 실패 분기 ---
    def test_check_all_active_connections_warning(self):
        """conn_check가 실패하면 warnings에 추가"""
        conn = MagicMock()
        conn.get_db_version.return_value = (8, 0, 32)
        conn.get_db_version_string.return_value = "8.0.32"
        conn.execute.return_value = []
        checker = PreflightChecker(conn)

        with patch.object(checker, "check_active_connections", return_value=CheckResult(
            name="conn", passed=False, severity=CheckSeverity.WARNING, message="3 active"
        )):
            result = checker.check_all("testdb", backup_confirmed=True)

        assert "3 active" in result.warnings

    # --- check_all line 147-151: version_check ERROR 분기 ---
    def test_check_all_version_error_fails(self):
        """version_check가 ERROR severity로 실패하면 전체 passed=False"""
        conn = MagicMock()
        conn.get_db_version.return_value = (8, 0, 32)
        conn.get_db_version_string.return_value = "8.0.32"
        conn.execute.return_value = []
        checker = PreflightChecker(conn)

        with patch.object(checker, "check_mysql_version", return_value=CheckResult(
            name="ver", passed=False, severity=CheckSeverity.ERROR, message="unsupported"
        )):
            result = checker.check_all("testdb", backup_confirmed=True)

        assert result.passed is False
        assert "unsupported" in result.errors

    # --- check_all line 151: version_check WARNING 분기 ---
    def test_check_all_version_warning_appends(self):
        """version_check가 WARNING이면 warnings에만 추가"""
        conn = MagicMock()
        conn.get_db_version.return_value = (8, 0, 32)
        conn.get_db_version_string.return_value = "8.0.32"
        conn.execute.return_value = []
        checker = PreflightChecker(conn)

        perm_pass = CheckResult(name="perm", passed=True, severity=CheckSeverity.INFO, message="ok")
        ver_warn = CheckResult(name="ver", passed=False, severity=CheckSeverity.WARNING, message="old minor")
        with patch.object(checker, "check_permissions", return_value=perm_pass), \
             patch.object(checker, "check_mysql_version", return_value=ver_warn):
            result = checker.check_all("testdb", backup_confirmed=True)

        assert result.passed is True
        assert "old minor" in result.warnings

    # --- line 482-492: get_large_tables ---
    def test_get_large_tables_returns_results(self):
        """get_large_tables가 Mock 커넥터에서 대용량 테이블 반환"""
        checker, conn = make_checker()
        conn.execute.return_value = [
            {"table_name": "events", "table_rows": 5000000, "size_mb": 4096.0},
            {"table_name": "logs", "table_rows": 2000000, "size_mb": 1024.0},
        ]
        result = checker.get_large_tables("testdb")
        assert len(result) == 2
        assert result[0]["table_name"] == "events"

    def test_get_large_tables_empty(self):
        checker, conn = make_checker()
        conn.execute.return_value = []
        result = checker.get_large_tables("testdb")
        assert result == []
