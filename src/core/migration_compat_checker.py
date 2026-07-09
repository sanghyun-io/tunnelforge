"""
MySQL 8.0.x → 8.4.x Upgrade 호환성 검사기

INFORMATION_SCHEMA 를 스캔해 호환성 이슈(CompatibilityIssue)를 수집한다.
컬럼 스캔형 8개 검사는 선언형 CheckSpec + 단일 _run_column_scan 헬퍼로 통합했고,
형태가 다른 검사(charset/reserved_keywords/routines/sql_modes/auth_plugins/invalid_date)는
그대로 둔다.
"""
import re
from dataclasses import dataclass
from typing import List, Callable, Optional

from src.core.migration_constants import (
    ALL_REMOVED_FUNCTIONS,
    ALL_RESERVED_KEYWORDS,
    DEPRECATED_FUNCTIONS_84,
    OBSOLETE_SQL_MODES,
    IssueType,
    CompatibilityIssue,
    ENGINE_POLICIES,
)


# ============================================================
# 컬럼 스캔형 검사 선언 (CheckSpec + build_issue 팩토리)
# ============================================================
@dataclass
class _CheckSpec:
    """단일 INFORMATION_SCHEMA 쿼리 → 행 루프 → 요약 log 형태의 검사 선언"""
    start_log: str
    query: str
    build_issue: Callable[[str, dict], Optional[CompatibilityIssue]]
    summary_found: Callable[[int], str]
    summary_clean: str


def _issue_zerofill(schema: str, col: dict) -> Optional[CompatibilityIssue]:
    return CompatibilityIssue(
        issue_type=IssueType.ZEROFILL_USAGE,
        severity="warning",
        location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
        description=f"ZEROFILL 속성 사용: {col['COLUMN_TYPE']}",
        suggestion="ZEROFILL은 deprecated됨, 애플리케이션에서 LPAD() 등으로 처리 권장"
    )


def _issue_float_precision(schema: str, col: dict) -> Optional[CompatibilityIssue]:
    return CompatibilityIssue(
        issue_type=IssueType.FLOAT_PRECISION,
        severity="warning",
        location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
        description=f"FLOAT/DOUBLE 정밀도 구문 사용: {col['COLUMN_TYPE']}",
        suggestion="FLOAT(M,D) 구문은 deprecated됨, FLOAT 또는 DECIMAL(M,D) 사용 권장"
    )


def _issue_fk_name_length(schema: str, fk: dict) -> Optional[CompatibilityIssue]:
    return CompatibilityIssue(
        issue_type=IssueType.FK_NAME_LENGTH,
        severity="error",
        location=f"{schema}.{fk['TABLE_NAME']}.{fk['CONSTRAINT_NAME']}",
        description=f"FK 이름이 64자 초과: {len(fk['CONSTRAINT_NAME'])}자",
        suggestion="FK 이름을 64자 이하로 변경 필요 (8.4 제한)"
    )


def _issue_year2(schema: str, col: dict) -> Optional[CompatibilityIssue]:
    return CompatibilityIssue(
        issue_type=IssueType.YEAR2_TYPE,
        severity="error",
        location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
        description="YEAR(2) 타입 사용 - MySQL 8.0에서 제거됨",
        suggestion="YEAR(4) 또는 YEAR로 변경 필요",
        table_name=col['TABLE_NAME'],
        column_name=col['COLUMN_NAME'],
        fix_query=f"ALTER TABLE `{schema}`.`{col['TABLE_NAME']}` MODIFY `{col['COLUMN_NAME']}` YEAR;"
    )


def _issue_deprecated_engine(schema: str, table: dict) -> Optional[CompatibilityIssue]:
    engine = table['ENGINE']
    if engine not in ENGINE_POLICIES:
        return None
    policy = ENGINE_POLICIES[engine]
    return CompatibilityIssue(
        issue_type=IssueType.DEPRECATED_ENGINE,
        severity=policy['severity'],
        location=f"{schema}.{table['TABLE_NAME']}",
        description=f"deprecated 스토리지 엔진: {engine}",
        suggestion=policy['suggestion'],
        table_name=table['TABLE_NAME'],
        fix_query=f"ALTER TABLE `{schema}`.`{table['TABLE_NAME']}` ENGINE=InnoDB;" if engine != 'MEMORY' else None
    )


def _issue_enum_empty(schema: str, col: dict) -> Optional[CompatibilityIssue]:
    return CompatibilityIssue(
        issue_type=IssueType.ENUM_EMPTY_VALUE,
        severity="warning",
        location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
        description="ENUM에 빈 문자열('') 정의됨",
        suggestion="빈 문자열 대신 NULL 허용 또는 명시적 값 사용 권장",
        table_name=col['TABLE_NAME'],
        column_name=col['COLUMN_NAME']
    )


def _issue_timestamp_range(schema: str, col: dict) -> Optional[CompatibilityIssue]:
    return CompatibilityIssue(
        issue_type=IssueType.TIMESTAMP_RANGE,
        severity="warning",
        location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
        description="TIMESTAMP 컬럼은 2038년 범위 제한이 있습니다",
        suggestion="2038년 이후 값이 필요한 컬럼은 DATETIME으로 변경을 검토하세요",
        table_name=col['TABLE_NAME'],
        column_name=col['COLUMN_NAME'],
        fix_query=f"ALTER TABLE `{schema}`.`{col['TABLE_NAME']}` MODIFY `{col['COLUMN_NAME']}` DATETIME;"
    )


def _issue_int_display_width(schema: str, col: dict) -> Optional[CompatibilityIssue]:
    return CompatibilityIssue(
        issue_type=IssueType.INT_DISPLAY_WIDTH,
        severity="info",
        location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
        description=f"INT 표시 너비 사용: {col['COLUMN_TYPE']}",
        suggestion="표시 너비는 deprecated됨, 8.4에서 자동 무시됨 (영향 최소)"
    )


_ZEROFILL_SPEC = _CheckSpec(
    start_log="🔍 ZEROFILL 속성 확인 중...",
    query="""
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND COLUMN_TYPE LIKE '%%ZEROFILL%%'
        """,
    build_issue=_issue_zerofill,
    summary_found=lambda n: f"  ⚠️ ZEROFILL 사용 {n}개 발견",
    summary_clean="  ✅ ZEROFILL 사용 없음",
)

_FLOAT_PRECISION_SPEC = _CheckSpec(
    start_log="🔍 FLOAT/DOUBLE 정밀도 구문 확인 중...",
    query="""
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE IN ('float', 'double')
            AND COLUMN_TYPE REGEXP '^(float|double)\\\\([0-9]+,[0-9]+\\\\)'
        """,
    build_issue=_issue_float_precision,
    summary_found=lambda n: f"  ⚠️ FLOAT/DOUBLE 정밀도 구문 {n}개 발견",
    summary_clean="  ✅ FLOAT/DOUBLE 구문 정상",
)

_FK_NAME_LENGTH_SPEC = _CheckSpec(
    start_log="🔍 FK 이름 길이 확인 중...",
    query="""
        SELECT CONSTRAINT_NAME, TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
        WHERE TABLE_SCHEMA = %s
            AND CONSTRAINT_TYPE = 'FOREIGN KEY'
            AND LENGTH(CONSTRAINT_NAME) > 64
        """,
    build_issue=_issue_fk_name_length,
    summary_found=lambda n: f"  ⚠️ FK 이름 길이 초과 {n}개 발견",
    summary_clean="  ✅ FK 이름 길이 정상",
)

_YEAR2_SPEC = _CheckSpec(
    start_log="🔍 YEAR(2) 타입 확인 중...",
    query="""
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND COLUMN_TYPE = 'year(2)'
        """,
    build_issue=_issue_year2,
    summary_found=lambda n: f"  ⚠️ YEAR(2) 타입 {n}개 발견",
    summary_clean="  ✅ YEAR(2) 타입 없음",
)

_DEPRECATED_ENGINE_SPEC = _CheckSpec(
    start_log="🔍 deprecated 스토리지 엔진 확인 중...",
    query="""
        SELECT TABLE_NAME, ENGINE
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s
            AND TABLE_TYPE = 'BASE TABLE'
            AND ENGINE IS NOT NULL
        """,
    build_issue=_issue_deprecated_engine,
    summary_found=lambda n: f"  ⚠️ deprecated 엔진 {n}개 발견",
    summary_clean="  ✅ deprecated 엔진 없음",
)

_ENUM_EMPTY_SPEC = _CheckSpec(
    start_log="🔍 ENUM 빈 문자열 확인 중...",
    query="""
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE = 'enum'
            AND COLUMN_TYPE LIKE "%%''%%"
        """,
    build_issue=_issue_enum_empty,
    summary_found=lambda n: f"  ⚠️ ENUM 빈 문자열 {n}개 발견",
    summary_clean="  ✅ ENUM 빈 문자열 없음",
)

_TIMESTAMP_RANGE_SPEC = _CheckSpec(
    start_log="🔍 TIMESTAMP 범위 확인 중...",
    query="""
        SELECT TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE = 'timestamp'
        """,
    build_issue=_issue_timestamp_range,
    summary_found=lambda n: f"  ⚠️ TIMESTAMP 범위 제한 컬럼 {n}개 발견",
    summary_clean="  ✅ TIMESTAMP 컬럼 없음",
)

_INT_DISPLAY_WIDTH_SPEC = _CheckSpec(
    start_log="🔍 INT 표시 너비 확인 중...",
    query="""
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE IN ('tinyint', 'smallint', 'mediumint', 'int', 'bigint')
            AND COLUMN_TYPE REGEXP '^(tinyint|smallint|mediumint|int|bigint)\\\\([0-9]+\\\\)'
            AND NOT (DATA_TYPE = 'tinyint' AND COLUMN_TYPE LIKE 'tinyint(1)%%')
        """,
    build_issue=_issue_int_display_width,
    summary_found=lambda n: f"  ℹ️ INT 표시 너비 {n}개 발견 (경미)",
    summary_clean="  ✅ INT 표시 너비 없음",
)


class MySQLUpgradeCompatibilityChecker:
    """MySQL 8.4 Upgrade Checker 호환성 검사 모음"""

    # MySQL 8.4에서 제거된/deprecated된 함수들 (전역 상수 사용)
    DEPRECATED_FUNCTIONS = list(ALL_REMOVED_FUNCTIONS)
    # deprecated만 (경고 수준 차등화용)
    _DEPRECATED_ONLY = set(DEPRECATED_FUNCTIONS_84)

    def __init__(self, connector, log: Callable[[str], None]):
        self.connector = connector
        # 파사드가 공유하는 _log 를 주입받아 진행 상황을 동일 콜백으로 전달한다.
        self._log = log

    def _run_column_scan(self, schema: str, spec: _CheckSpec) -> List[CompatibilityIssue]:
        """선언형 CheckSpec 실행: 시작 log → 단일 쿼리 → 행 루프 → 요약 log"""
        self._log(spec.start_log)

        issues = []
        rows = self.connector.execute(spec.query, (schema,))
        for row in rows:
            issue = spec.build_issue(schema, row)
            if issue is not None:
                issues.append(issue)

        if issues:
            self._log(spec.summary_found(len(issues)))
        else:
            self._log(spec.summary_clean)

        return issues

    # ------------------------------------------------------------
    # 컬럼 스캔형 검사 (선언형 CheckSpec 위임)
    # ------------------------------------------------------------
    def check_zerofill_columns(self, schema: str) -> List[CompatibilityIssue]:
        """ZEROFILL 속성 사용 컬럼 확인"""
        return self._run_column_scan(schema, _ZEROFILL_SPEC)

    def check_float_precision(self, schema: str) -> List[CompatibilityIssue]:
        """FLOAT(M,D), DOUBLE(M,D) 구문 확인"""
        return self._run_column_scan(schema, _FLOAT_PRECISION_SPEC)

    def check_fk_name_length(self, schema: str) -> List[CompatibilityIssue]:
        """FK 이름 64자 초과 확인"""
        return self._run_column_scan(schema, _FK_NAME_LENGTH_SPEC)

    def check_year2_type(self, schema: str) -> List[CompatibilityIssue]:
        """YEAR(2) 타입 검사 - MySQL 8.0에서 제거됨"""
        return self._run_column_scan(schema, _YEAR2_SPEC)

    def check_deprecated_engines(self, schema: str) -> List[CompatibilityIssue]:
        """deprecated 스토리지 엔진 검사"""
        return self._run_column_scan(schema, _DEPRECATED_ENGINE_SPEC)

    def check_enum_empty_value(self, schema: str) -> List[CompatibilityIssue]:
        """ENUM 빈 문자열('') 정의 검사 - 8.4에서 엄격해짐"""
        return self._run_column_scan(schema, _ENUM_EMPTY_SPEC)

    def check_timestamp_range(self, schema: str) -> List[CompatibilityIssue]:
        """TIMESTAMP 2038년 범위 제한 검사

        TIMESTAMP는 애초에 '2038-01-19 03:14:07' UTC를 초과하는 값을 저장할
        수 없는 타입이므로, 저장된 데이터를 `WHERE col > '2038-01-19 03:14:07'`
        로 조회해 "범위 초과 데이터"를 찾으려는 시도는 절대 참이 될 수 없어
        항상 0건만 반환하는 무의미한 검사였다. 대신 TIMESTAMP 컬럼이 존재한다는
        사실 자체를 스키마 레벨 advisory(경고)로 보고한다.
        """
        return self._run_column_scan(schema, _TIMESTAMP_RANGE_SPEC)

    def check_int_display_width(self, schema: str) -> List[CompatibilityIssue]:
        """INT(11) 등 표시 너비 사용 확인 (TINYINT(1) 제외)"""
        return self._run_column_scan(schema, _INT_DISPLAY_WIDTH_SPEC)

    # ------------------------------------------------------------
    # 형태가 다른 검사 (그대로 유지)
    # ------------------------------------------------------------
    def check_charset_issues(self, schema: str) -> List[CompatibilityIssue]:
        """utf8mb3 사용 테이블/컬럼 확인"""
        self._log("🔍 문자셋 이슈 확인 중...")

        issues = []

        # 테이블 레벨 charset 확인
        table_query = """
        SELECT TABLE_NAME, TABLE_COLLATION
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s
            AND TABLE_TYPE = 'BASE TABLE'
            AND (TABLE_COLLATION LIKE 'utf8\\_%%' OR TABLE_COLLATION LIKE 'utf8mb3\\_%%')
        """
        tables = self.connector.execute(table_query, (schema,))

        for t in tables:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.CHARSET_ISSUE,
                severity="warning",
                location=f"{schema}.{t['TABLE_NAME']}",
                description=f"테이블이 utf8mb3 collation 사용 중: {t['TABLE_COLLATION']}",
                suggestion="ALTER TABLE ... CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            ))

        # 컬럼 레벨 charset 확인 (VIEW 제외, BASE TABLE만)
        column_query = """
        SELECT c.TABLE_NAME, c.COLUMN_NAME, c.CHARACTER_SET_NAME, c.COLLATION_NAME
        FROM INFORMATION_SCHEMA.COLUMNS c
        JOIN INFORMATION_SCHEMA.TABLES t
            ON c.TABLE_NAME = t.TABLE_NAME
            AND c.TABLE_SCHEMA = t.TABLE_SCHEMA
        WHERE c.TABLE_SCHEMA = %s
            AND c.CHARACTER_SET_NAME IN ('utf8', 'utf8mb3')
            AND t.TABLE_TYPE = 'BASE TABLE'
        """
        columns = self.connector.execute(column_query, (schema,))

        for c in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.CHARSET_ISSUE,
                severity="warning",
                location=f"{schema}.{c['TABLE_NAME']}.{c['COLUMN_NAME']}",
                description=f"컬럼이 utf8mb3 사용 중: {c['CHARACTER_SET_NAME']}",
                suggestion="ALTER TABLE ... MODIFY COLUMN ... CHARACTER SET utf8mb4"
            ))

        if issues:
            self._log(f"  ⚠️ 문자셋 이슈 {len(issues)}개 발견")
        else:
            self._log("  ✅ 문자셋 이슈 없음")

        return issues

    def check_reserved_keywords(self, schema: str) -> List[CompatibilityIssue]:
        """예약어와 충돌하는 컬럼/테이블명 확인"""
        self._log("🔍 예약어 충돌 확인 중...")

        issues = []
        keywords_upper = set(k.upper() for k in ALL_RESERVED_KEYWORDS)

        # 테이블명 확인
        tables = self.connector.get_tables(schema)
        for table in tables:
            if table.upper() in keywords_upper:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.RESERVED_KEYWORD,
                    severity="error",
                    location=f"{schema}.{table}",
                    description=f"테이블명 '{table}'이 MySQL 8.4 예약어와 충돌",
                    suggestion="테이블명을 백틱으로 감싸거나 이름 변경 필요"
                ))

        # 컬럼명 확인
        column_query = """
        SELECT TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
        """
        columns = self.connector.execute(column_query, (schema,))

        for c in columns:
            if c['COLUMN_NAME'].upper() in keywords_upper:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.RESERVED_KEYWORD,
                    severity="warning",
                    location=f"{schema}.{c['TABLE_NAME']}.{c['COLUMN_NAME']}",
                    description=f"컬럼명 '{c['COLUMN_NAME']}'이 MySQL 8.4 예약어와 충돌",
                    suggestion="컬럼 참조 시 백틱(`) 사용 필요"
                ))

        if issues:
            self._log(f"  ⚠️ 예약어 충돌 {len(issues)}개 발견")
        else:
            self._log("  ✅ 예약어 충돌 없음")

        return issues

    def check_deprecated_in_routines(self, schema: str) -> List[CompatibilityIssue]:
        """저장 프로시저/함수에서 deprecated 함수 사용 확인"""
        self._log("🔍 저장 프로시저/함수 검사 중...")

        issues = []

        # 저장 프로시저와 함수 조회
        routine_query = """
        SELECT ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION
        FROM INFORMATION_SCHEMA.ROUTINES
        WHERE ROUTINE_SCHEMA = %s
            AND ROUTINE_DEFINITION IS NOT NULL
        """
        routines = self.connector.execute(routine_query, (schema,))

        for routine in routines:
            definition = routine['ROUTINE_DEFINITION'].upper() if routine['ROUTINE_DEFINITION'] else ""

            for func in self.DEPRECATED_FUNCTIONS:
                # 단순 부분 문자열 매칭(`func in definition`)은 `password`
                # 같은 컬럼/변수명에도 오탐한다. 함수 호출 경계(뒤에 '('가
                # 오는지)까지 확인해 실제 호출만 잡는다. 언더스코어는 단어
                # 문자라서 \b가 AES_ENCRYPT 안의 ENCRYPT에는 걸리지 않는다.
                if re.search(r'\b' + re.escape(func) + r'\s*\(', definition):
                    # removed vs deprecated 차등화
                    is_deprecated_only = func in self._DEPRECATED_ONLY
                    severity = "warning" if is_deprecated_only else "error"
                    label = "deprecated" if is_deprecated_only else "removed"
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.DEPRECATED_FUNCTION,
                        severity=severity,
                        location=f"{routine['ROUTINE_TYPE']} {schema}.{routine['ROUTINE_NAME']}",
                        description=f"{label} 함수 '{func}' 사용 중",
                        suggestion=f"'{func}' 함수를 대체 함수로 변경 필요"
                    ))

        if issues:
            self._log(f"  ⚠️ deprecated 함수 사용 {len(issues)}개 발견")
        else:
            self._log("  ✅ deprecated 함수 없음")

        return issues

    def check_sql_modes(self) -> List[CompatibilityIssue]:
        """현재 SQL 모드 확인"""
        self._log("🔍 SQL 모드 확인 중...")

        issues = []

        # deprecated SQL 모드들 (상수 모듈의 OBSOLETE_SQL_MODES 사용)
        deprecated_modes = OBSOLETE_SQL_MODES

        result = self.connector.execute("SELECT @@sql_mode as sql_mode")
        if result:
            sql_mode_raw = result[0].get('sql_mode') or ''
            current_modes = sql_mode_raw.split(',') if sql_mode_raw else []

            for mode in current_modes:
                mode = mode.strip()
                if mode in deprecated_modes:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.SQL_MODE_ISSUE,
                        severity="warning",
                        location="@@sql_mode",
                        description=f"deprecated SQL 모드 '{mode}' 사용 중",
                        suggestion=f"sql_mode에서 '{mode}' 제거 필요"
                    ))

        if issues:
            self._log(f"  ⚠️ deprecated SQL 모드 {len(issues)}개 발견")
        else:
            self._log("  ✅ SQL 모드 정상")

        return issues

    def check_auth_plugins(self) -> List[CompatibilityIssue]:
        """mysql_native_password, sha256_password 사용자 확인"""
        self._log("🔍 인증 플러그인 확인 중...")

        issues = []

        # 사용자별 인증 플러그인 조회
        query = """
        SELECT User, Host, plugin
        FROM mysql.user
        WHERE plugin IN ('mysql_native_password', 'sha256_password', 'authentication_fido', 'authentication_fido_client')
        """
        try:
            users = self.connector.execute(query)

            for user in users:
                plugin = user['plugin']

                if plugin == 'mysql_native_password':
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.AUTH_PLUGIN_ISSUE,
                        severity="error",
                        location=f"'{user['User']}'@'{user['Host']}'",
                        description="mysql_native_password 인증 사용 (8.4에서 기본 비활성화)",
                        suggestion="ALTER USER ... IDENTIFIED WITH caching_sha2_password"
                    ))
                elif plugin == 'sha256_password':
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.AUTH_PLUGIN_ISSUE,
                        severity="warning",
                        location=f"'{user['User']}'@'{user['Host']}'",
                        description="sha256_password 인증 사용 (deprecated)",
                        suggestion="ALTER USER ... IDENTIFIED WITH caching_sha2_password 권장"
                    ))
                elif plugin in ('authentication_fido', 'authentication_fido_client'):
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.AUTH_PLUGIN_ISSUE,
                        severity="error",
                        location=f"'{user['User']}'@'{user['Host']}'",
                        description=f"{plugin} 플러그인 사용 (8.4에서 제거됨)",
                        suggestion="authentication_webauthn 또는 다른 인증 방식으로 변경 필요"
                    ))

            if issues:
                self._log(f"  ⚠️ 인증 플러그인 이슈 {len(issues)}개 발견")
            else:
                self._log("  ✅ 인증 플러그인 정상")

        except Exception as e:
            self._log(f"  ⚠️ 인증 플러그인 확인 실패: {str(e)}")

        return issues

    def check_invalid_date_values(self, schema: str) -> List[CompatibilityIssue]:
        """0000-00-00 및 잘못된 날짜값 검사 (MySQL 8.4 호환성)

        MySQL 8.4에서는 NO_ZERO_DATE, NO_ZERO_IN_DATE가 기본 sql_mode에 포함됨.
        0000-00-00 또는 2024-00-15 같은 날짜는 더 이상 허용되지 않음.
        """
        self._log("🔍 0000-00-00 날짜값 확인 중...")

        issues = []

        # DATE, DATETIME, TIMESTAMP 컬럼 조회
        col_query = """
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_DEFAULT
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE IN ('date', 'datetime', 'timestamp')
        ORDER BY TABLE_NAME, COLUMN_NAME
        """
        columns = self.connector.execute(col_query, (schema,))

        if not columns:
            self._log("  ✅ DATE/DATETIME 컬럼 없음")
            return issues

        self._log(f"  DATE/DATETIME 컬럼 {len(columns)}개 검사 중...")

        checked_count = 0
        for col in columns:
            table = col['TABLE_NAME']
            column = col['COLUMN_NAME']
            data_type = col['DATA_TYPE']

            try:
                # 0000-00-00 값 존재 확인 (COUNT로 빠르게)
                if data_type == 'date':
                    check_query = f"""
                    SELECT COUNT(*) as cnt
                    FROM `{schema}`.`{table}`
                    WHERE `{column}` = '0000-00-00'
                        OR (`{column}` IS NOT NULL
                            AND (MONTH(`{column}`) = 0 OR DAY(`{column}`) = 0))
                    """
                else:  # datetime, timestamp
                    check_query = f"""
                    SELECT COUNT(*) as cnt
                    FROM `{schema}`.`{table}`
                    WHERE `{column}` = '0000-00-00 00:00:00'
                        OR (`{column}` IS NOT NULL
                            AND (MONTH(`{column}`) = 0 OR DAY(`{column}`) = 0))
                    """

                result = self.connector.execute(check_query)
                invalid_count = result[0]['cnt'] if result else 0

                if invalid_count > 0:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.INVALID_DATE,
                        severity="error",
                        location=f"{schema}.{table}.{column}",
                        description=f"잘못된 날짜값 {invalid_count:,}개 발견 (0000-00-00 등)",
                        suggestion="NULL로 변경하거나 유효한 날짜로 수정 필요 (8.4 NO_ZERO_DATE)",
                        table_name=table,
                        column_name=column,
                        fix_query=f"UPDATE `{schema}`.`{table}` SET `{column}` = NULL WHERE `{column}` = '0000-00-00' OR MONTH(`{column}`) = 0 OR DAY(`{column}`) = 0;"
                    ))
                    self._log(f"    ⚠️ {table}.{column}: 잘못된 날짜 {invalid_count:,}개")

                checked_count += 1

            except Exception as e:
                # 특정 테이블 검사 실패 시 스킵 (권한 등)
                self._log(f"    ⏭️ {table}.{column} 검사 스킵: {str(e)[:50]}")
                continue

        if issues:
            self._log(f"  ⚠️ 잘못된 날짜값 {len(issues)}개 컬럼에서 발견")
        else:
            self._log(f"  ✅ 잘못된 날짜값 없음 ({checked_count}개 컬럼 검사)")

        return issues
