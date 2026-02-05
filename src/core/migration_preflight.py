"""
MySQL 8.0 → 8.4 마이그레이션 Pre-flight Check 시스템

마이그레이션 실행 전 사전 검증을 수행하여 안전한 마이그레이션을 보장합니다.
- 권한 검사 (ALTER, UPDATE, DELETE)
- 디스크 공간 확인
- 활성 연결 확인
- 백업 상태 확인
- 예상 시간 계산
"""
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional, Set, Callable
from enum import Enum

from src.core.db_connector import MySQLConnector


class CheckSeverity(Enum):
    """검사 결과 심각도"""
    ERROR = "error"      # 마이그레이션 불가
    WARNING = "warning"  # 주의 필요
    INFO = "info"        # 정보성


@dataclass
class CheckResult:
    """개별 검사 결과"""
    name: str
    passed: bool
    severity: CheckSeverity
    message: str
    details: Optional[str] = None

    @property
    def severity_str(self) -> str:
        return self.severity.value


@dataclass
class PreflightResult:
    """Pre-flight 전체 결과"""
    passed: bool
    checks: List[CheckResult] = field(default_factory=list)
    estimated_time: timedelta = field(default_factory=lambda: timedelta(seconds=0))
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len([c for c in self.checks if not c.passed and c.severity == CheckSeverity.ERROR])

    @property
    def warning_count(self) -> int:
        return len([c for c in self.checks if c.severity == CheckSeverity.WARNING])

    def get_summary(self) -> str:
        """결과 요약 문자열 반환"""
        if self.passed:
            return f"✅ Pre-flight 통과 ({len(self.checks)}개 검사, 경고 {self.warning_count}개)"
        else:
            return f"❌ Pre-flight 실패 (오류 {self.error_count}개, 경고 {self.warning_count}개)"


class PreflightChecker:
    """마이그레이션 Pre-flight 검사기"""

    # 필요한 권한 목록
    REQUIRED_PRIVILEGES = {'ALTER', 'UPDATE', 'DELETE', 'SELECT', 'INSERT'}

    # 이슈당 평균 처리 시간 (초)
    AVERAGE_TIME_PER_ISSUE = 5

    # 대용량 테이블 기준 (행)
    LARGE_TABLE_THRESHOLD = 100000

    def __init__(self, connector: MySQLConnector):
        """
        Args:
            connector: MySQL 연결 객체
        """
        self.connector = connector
        self._progress_callback: Optional[Callable[[str], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def _log(self, message: str):
        """진행 상황 로깅"""
        if self._progress_callback:
            self._progress_callback(message)

    def check_all(self, schema: str, backup_confirmed: bool = False) -> PreflightResult:
        """
        전체 Pre-flight 검사 실행

        Args:
            schema: 검사할 스키마명
            backup_confirmed: 백업 완료 확인 여부 (UI에서 확인 후 전달)

        Returns:
            PreflightResult
        """
        self._log("🔍 Pre-flight 검사 시작...")

        result = PreflightResult(passed=True)

        # 1. 권한 검사 (필수)
        self._log("  [1/5] 권한 검사 중...")
        perm_check = self.check_permissions(schema)
        result.checks.append(perm_check)
        if not perm_check.passed and perm_check.severity == CheckSeverity.ERROR:
            result.passed = False
            result.errors.append(perm_check.message)

        # 2. 디스크 공간 검사
        self._log("  [2/5] 디스크 공간 검사 중...")
        disk_check = self.check_disk_space(schema)
        result.checks.append(disk_check)
        if not disk_check.passed:
            if disk_check.severity == CheckSeverity.ERROR:
                result.passed = False
                result.errors.append(disk_check.message)
            else:
                result.warnings.append(disk_check.message)

        # 3. 활성 연결 검사
        self._log("  [3/5] 활성 연결 검사 중...")
        conn_check = self.check_active_connections(schema)
        result.checks.append(conn_check)
        if not conn_check.passed:
            result.warnings.append(conn_check.message)

        # 4. 백업 상태 확인
        self._log("  [4/5] 백업 상태 확인 중...")
        backup_check = self.check_backup_status(backup_confirmed)
        result.checks.append(backup_check)
        if not backup_check.passed:
            result.warnings.append(backup_check.message)

        # 5. MySQL 버전 확인
        self._log("  [5/5] MySQL 버전 확인 중...")
        version_check = self.check_mysql_version()
        result.checks.append(version_check)
        if not version_check.passed:
            if version_check.severity == CheckSeverity.ERROR:
                result.passed = False
                result.errors.append(version_check.message)
            else:
                result.warnings.append(version_check.message)

        self._log(f"✅ Pre-flight 검사 완료: {'통과' if result.passed else '실패'}")

        return result

    def check_permissions(self, schema: str) -> CheckResult:
        """
        권한 검사 - ALTER, UPDATE, DELETE 등 필요한 권한 확인

        Args:
            schema: 검사할 스키마명

        Returns:
            CheckResult
        """
        try:
            # 현재 사용자 권한 조회
            grants = self.connector.execute("SHOW GRANTS FOR CURRENT_USER()")

            if not grants:
                return CheckResult(
                    name="권한 검사",
                    passed=False,
                    severity=CheckSeverity.ERROR,
                    message="권한 정보를 조회할 수 없습니다.",
                    details="SHOW GRANTS 쿼리 결과 없음"
                )

            # GRANT 결과 파싱
            user_privileges = self._parse_grants(grants, schema)

            # 필요한 권한 확인
            missing = self.REQUIRED_PRIVILEGES - user_privileges

            if not missing:
                return CheckResult(
                    name="권한 검사",
                    passed=True,
                    severity=CheckSeverity.INFO,
                    message="필요한 모든 권한 보유",
                    details=f"보유 권한: {', '.join(sorted(user_privileges))}"
                )

            # 부족한 권한이 있는 경우
            return CheckResult(
                name="권한 검사",
                passed=False,
                severity=CheckSeverity.ERROR,
                message=f"권한 부족: {', '.join(sorted(missing))}",
                details=f"스키마 '{schema}'에 대한 권한이 필요합니다."
            )

        except Exception as e:
            return CheckResult(
                name="권한 검사",
                passed=False,
                severity=CheckSeverity.ERROR,
                message=f"권한 검사 실패: {str(e)}",
                details=None
            )

    def check_disk_space(self, schema: str) -> CheckResult:
        """
        디스크 공간 검사 - 스키마 크기의 2배 여유 공간 필요

        Args:
            schema: 검사할 스키마명

        Returns:
            CheckResult
        """
        try:
            # 스키마 크기 조회 (MB)
            schema_size_mb = self._get_schema_size_mb(schema)

            if schema_size_mb == 0:
                return CheckResult(
                    name="디스크 공간 검사",
                    passed=True,
                    severity=CheckSeverity.INFO,
                    message="스키마가 비어있거나 크기를 확인할 수 없습니다.",
                    details=None
                )

            # 권장 여유 공간 (스키마 크기 × 2)
            recommended_mb = schema_size_mb * 2

            # 실제 여유 공간 확인은 OS 레벨이므로 권장 사항만 안내
            return CheckResult(
                name="디스크 공간 검사",
                passed=True,
                severity=CheckSeverity.WARNING if schema_size_mb > 1024 else CheckSeverity.INFO,
                message=f"스키마 크기: {schema_size_mb:.1f} MB",
                details=f"권장 여유 공간: {recommended_mb:.1f} MB 이상 (ALTER TABLE 작업 시 임시 공간 필요)"
            )

        except Exception as e:
            return CheckResult(
                name="디스크 공간 검사",
                passed=True,
                severity=CheckSeverity.WARNING,
                message=f"디스크 공간 확인 실패: {str(e)}",
                details="수동으로 여유 공간을 확인하세요."
            )

    def check_active_connections(self, schema: str) -> CheckResult:
        """
        활성 연결 검사 - 동일 스키마 사용 중인 연결 확인

        Args:
            schema: 검사할 스키마명

        Returns:
            CheckResult
        """
        try:
            # 프로세스 목록 조회
            processes = self.connector.execute("SHOW PROCESSLIST")

            # 동일 스키마 사용 중인 연결 (현재 연결 제외)
            other_connections = [
                p for p in processes
                if p.get('db') == schema and p.get('Command') != 'Sleep'
            ]

            if not other_connections:
                return CheckResult(
                    name="활성 연결 검사",
                    passed=True,
                    severity=CheckSeverity.INFO,
                    message="다른 활성 연결 없음",
                    details=f"스키마 '{schema}'를 사용 중인 다른 세션이 없습니다."
                )

            # 활성 연결이 있는 경우
            conn_info = [
                f"User: {p.get('User', 'N/A')}, Command: {p.get('Command', 'N/A')}"
                for p in other_connections[:5]  # 최대 5개만 표시
            ]

            return CheckResult(
                name="활성 연결 검사",
                passed=False,
                severity=CheckSeverity.WARNING,
                message=f"활성 연결 {len(other_connections)}개 발견",
                details=f"다른 세션이 작업 중입니다:\n" + "\n".join(conn_info)
            )

        except Exception as e:
            return CheckResult(
                name="활성 연결 검사",
                passed=True,
                severity=CheckSeverity.WARNING,
                message=f"연결 확인 실패: {str(e)}",
                details=None
            )

    def check_backup_status(self, confirmed: bool = False) -> CheckResult:
        """
        백업 상태 확인 - 사용자 확인 필요

        Args:
            confirmed: 사용자가 백업 완료를 확인했는지 여부

        Returns:
            CheckResult
        """
        if confirmed:
            return CheckResult(
                name="백업 상태 확인",
                passed=True,
                severity=CheckSeverity.INFO,
                message="백업 완료 확인됨",
                details="사용자가 백업 완료를 확인했습니다."
            )
        else:
            return CheckResult(
                name="백업 상태 확인",
                passed=False,
                severity=CheckSeverity.WARNING,
                message="백업 상태 미확인",
                details="마이그레이션 전 데이터베이스 백업을 권장합니다."
            )

    def check_mysql_version(self) -> CheckResult:
        """
        MySQL 버전 확인 - 8.0.x 버전인지 확인

        Returns:
            CheckResult
        """
        try:
            version = self.connector.get_db_version()
            version_str = self.connector.get_db_version_string()

            major, minor, patch = version

            if major == 8 and minor == 0:
                return CheckResult(
                    name="MySQL 버전 확인",
                    passed=True,
                    severity=CheckSeverity.INFO,
                    message=f"MySQL {version_str}",
                    details="MySQL 8.0.x → 8.4.x 마이그레이션 대상 버전입니다."
                )
            elif major == 8 and minor >= 4:
                return CheckResult(
                    name="MySQL 버전 확인",
                    passed=True,
                    severity=CheckSeverity.INFO,
                    message=f"MySQL {version_str}",
                    details="이미 MySQL 8.4+ 버전입니다. 호환성 검사만 수행됩니다."
                )
            elif major < 8:
                return CheckResult(
                    name="MySQL 버전 확인",
                    passed=False,
                    severity=CheckSeverity.WARNING,
                    message=f"MySQL {version_str}",
                    details="MySQL 8.0 미만 버전입니다. 먼저 8.0으로 업그레이드하세요."
                )
            else:
                return CheckResult(
                    name="MySQL 버전 확인",
                    passed=True,
                    severity=CheckSeverity.INFO,
                    message=f"MySQL {version_str}",
                    details=None
                )

        except Exception as e:
            return CheckResult(
                name="MySQL 버전 확인",
                passed=True,
                severity=CheckSeverity.WARNING,
                message=f"버전 확인 실패: {str(e)}",
                details=None
            )

    def estimate_time(self, issue_count: int, large_table_count: int = 0) -> timedelta:
        """
        예상 마이그레이션 시간 계산

        Args:
            issue_count: 발견된 이슈 수
            large_table_count: 대용량 테이블 수

        Returns:
            예상 소요 시간
        """
        # 기본 시간: 이슈당 평균 시간
        base_seconds = issue_count * self.AVERAGE_TIME_PER_ISSUE

        # 대용량 테이블 보정 (테이블당 30초 추가)
        large_table_seconds = large_table_count * 30

        # 최소 시간: 30초
        total_seconds = max(30, base_seconds + large_table_seconds)

        return timedelta(seconds=total_seconds)

    def _parse_grants(self, grants: List[dict], schema: str) -> Set[str]:
        """
        GRANT 결과에서 권한 추출

        Args:
            grants: SHOW GRANTS 결과
            schema: 대상 스키마명

        Returns:
            권한 집합
        """
        privileges = set()

        for grant in grants:
            # GRANT 문 추출 (첫 번째 컬럼 값)
            grant_str = list(grant.values())[0] if grant else ""
            grant_upper = grant_str.upper()

            # ALL PRIVILEGES 확인
            if 'ALL PRIVILEGES' in grant_upper:
                privileges.update(self.REQUIRED_PRIVILEGES)
                continue

            # 전역 권한 확인 (*.*)
            if 'ON *.*' in grant_upper:
                for priv in self.REQUIRED_PRIVILEGES:
                    if priv in grant_upper:
                        privileges.add(priv)
                continue

            # 스키마 특정 권한 확인
            if f'ON `{schema}`.' in grant_str or f'ON {schema}.' in grant_str:
                for priv in self.REQUIRED_PRIVILEGES:
                    if priv in grant_upper:
                        privileges.add(priv)

        return privileges

    def _get_schema_size_mb(self, schema: str) -> float:
        """
        스키마 총 크기 조회 (MB)

        Args:
            schema: 스키마명

        Returns:
            크기 (MB)
        """
        query = """
        SELECT COALESCE(SUM(data_length + index_length), 0) / 1024 / 1024 as size_mb
        FROM information_schema.tables
        WHERE table_schema = %s
        """
        result = self.connector.execute(query, (schema,))

        if result and result[0].get('size_mb') is not None:
            return float(result[0]['size_mb'])
        return 0.0

    def get_large_tables(self, schema: str) -> List[dict]:
        """
        대용량 테이블 목록 조회

        Args:
            schema: 스키마명

        Returns:
            대용량 테이블 정보 목록
        """
        query = """
        SELECT
            table_name,
            table_rows,
            ROUND((data_length + index_length) / 1024 / 1024, 2) as size_mb
        FROM information_schema.tables
        WHERE table_schema = %s
            AND table_rows > %s
        ORDER BY table_rows DESC
        """
        return self.connector.execute(query, (schema, self.LARGE_TABLE_THRESHOLD))
