"""
migration_preflight.py 단위 테스트

CheckSeverity, CheckResult, PreflightResult 검증.
PreflightChecker(원격 SQL 기반 사전 검사기)는 Rust DB Core로 이관되어
삭제되었다 — 이 모듈에는 Rust가 보낸 preflight 이벤트를 UI에 표현하기
위한 데이터클래스만 남아 있다.
"""
from src.core.migration_preflight import CheckSeverity, CheckResult, PreflightResult


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

    def test_defaults(self):
        r = PreflightResult(passed=True)
        assert r.checks == []
        assert r.warnings == []
        assert r.errors == []

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
