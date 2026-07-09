"""
MySQL 마이그레이션 분석기 (파사드)

MigrationAnalyzer 는 아래 협력 모듈을 오케스트레이션하는 얇은 파사드다:
- migration_fk_analyzer.ForeignKeyAnalyzer          : FK 관계/고아 레코드 탐지
- migration_compat_checker.MySQLUpgradeCompatibilityChecker : 8.0→8.4 호환성 검사
- migration_cleanup_planner.OrphanCleanupPlanner    : 고아 레코드 정리 SQL/영향 분석
- migration_dump_analyzer.DumpFileAnalyzer          : 덤프 파일(SQL/TSV) 분석

데이터클래스는 migration_analysis_models 에 정의돼 있으며, 하위호환을 위해
이 모듈 최상위에서 re-export 한다 (src/core/__init__.py 및 UI/테스트가 의존).
"""
from typing import List, Dict, Tuple, Optional, Callable

from src.core.db_connector import MySQLConnector

# 상수/공용 이슈 타입 (re-export 대상 포함)
from src.core.migration_constants import IssueType, CompatibilityIssue

# 데이터 모델 (하위호환 re-export)
from src.core.migration_analysis_models import (
    ActionType,
    OrphanRecord,
    ForeignKeyInfo,
    CleanupAction,
    AnalysisResult,
    SchemaCheckOptions,
)

# 협력 모듈
from src.core.migration_fk_analyzer import ForeignKeyAnalyzer
from src.core.migration_compat_checker import MySQLUpgradeCompatibilityChecker
from src.core.migration_cleanup_planner import OrphanCleanupPlanner

# 덤프 파일 분석기 (하위호환 re-export)
from src.core.migration_dump_analyzer import DumpAnalysisResult, DumpFileAnalyzer

__all__ = [
    'MigrationAnalyzer',
    'AnalysisResult',
    'OrphanRecord',
    'CleanupAction',
    'ActionType',
    'ForeignKeyInfo',
    'SchemaCheckOptions',
    'CompatibilityIssue',
    'IssueType',
    'DumpFileAnalyzer',
    'DumpAnalysisResult',
]


class MigrationAnalyzer:
    """마이그레이션 분석기 (협력 모듈 파사드)

    기존 public API(analyze_schema, check_* 검사, FK/고아/정리 메서드)를
    100% 유지하되, 실제 구현은 협력 객체(_fk/_compat/_cleanup)로 위임한다.
    """

    def __init__(self, connector: MySQLConnector):
        self.connector = connector
        self._progress_callback: Optional[Callable[[str], None]] = None
        # 공유 _log 를 각 협력 객체에 주입해 진행 상황을 동일 콜백으로 전달한다.
        self._fk = ForeignKeyAnalyzer(connector, self._log)
        self._compat = MySQLUpgradeCompatibilityChecker(connector, self._log)
        self._cleanup = OrphanCleanupPlanner(connector, self._log)

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def _log(self, message: str):
        """진행 상황 로깅"""
        if self._progress_callback:
            self._progress_callback(message)

    # ------------------------------------------------------------
    # FK 관계 / 고아 레코드 (ForeignKeyAnalyzer 위임)
    # ------------------------------------------------------------
    def get_foreign_keys(self, schema: str) -> List[ForeignKeyInfo]:
        """스키마의 모든 FK 관계 조회"""
        return self._fk.get_foreign_keys(schema)

    def build_fk_tree(self, schema: str) -> Dict[str, List[str]]:
        """FK 관계 트리 구성 (부모 → 자식 목록)"""
        return self._fk.build_fk_tree(schema)

    def find_orphan_records(self, schema: str, *args, **kwargs) -> List[OrphanRecord]:
        """고아 레코드 탐지 (부모 없는 자식 레코드)"""
        return self._fk.find_orphan_records(schema, *args, **kwargs)

    def get_fk_visualization(self, schema: str) -> str:
        """FK 관계를 트리 형태로 시각화"""
        return self._fk.get_fk_visualization(schema)

    # ------------------------------------------------------------
    # 호환성 검사 (MySQLUpgradeCompatibilityChecker 위임)
    # ------------------------------------------------------------
    def check_charset_issues(self, schema: str) -> List[CompatibilityIssue]:
        """utf8mb3 사용 테이블/컬럼 확인"""
        return self._compat.check_charset_issues(schema)

    def check_reserved_keywords(self, schema: str) -> List[CompatibilityIssue]:
        """예약어와 충돌하는 컬럼/테이블명 확인"""
        return self._compat.check_reserved_keywords(schema)

    def check_deprecated_in_routines(self, schema: str) -> List[CompatibilityIssue]:
        """저장 프로시저/함수에서 deprecated 함수 사용 확인"""
        return self._compat.check_deprecated_in_routines(schema)

    def check_sql_modes(self) -> List[CompatibilityIssue]:
        """현재 SQL 모드 확인"""
        return self._compat.check_sql_modes()

    def check_auth_plugins(self) -> List[CompatibilityIssue]:
        """mysql_native_password, sha256_password 사용자 확인"""
        return self._compat.check_auth_plugins()

    def check_zerofill_columns(self, schema: str) -> List[CompatibilityIssue]:
        """ZEROFILL 속성 사용 컬럼 확인"""
        return self._compat.check_zerofill_columns(schema)

    def check_float_precision(self, schema: str) -> List[CompatibilityIssue]:
        """FLOAT(M,D), DOUBLE(M,D) 구문 확인"""
        return self._compat.check_float_precision(schema)

    def check_fk_name_length(self, schema: str) -> List[CompatibilityIssue]:
        """FK 이름 64자 초과 확인"""
        return self._compat.check_fk_name_length(schema)

    def check_invalid_date_values(self, schema: str) -> List[CompatibilityIssue]:
        """0000-00-00 및 잘못된 날짜값 검사 (MySQL 8.4 호환성)"""
        return self._compat.check_invalid_date_values(schema)

    def check_int_display_width(self, schema: str) -> List[CompatibilityIssue]:
        """INT(11) 등 표시 너비 사용 확인 (TINYINT(1) 제외)"""
        return self._compat.check_int_display_width(schema)

    def check_year2_type(self, schema: str) -> List[CompatibilityIssue]:
        """YEAR(2) 타입 검사 - MySQL 8.0에서 제거됨"""
        return self._compat.check_year2_type(schema)

    def check_deprecated_engines(self, schema: str) -> List[CompatibilityIssue]:
        """deprecated 스토리지 엔진 검사"""
        return self._compat.check_deprecated_engines(schema)

    def check_enum_empty_value(self, schema: str) -> List[CompatibilityIssue]:
        """ENUM 빈 문자열('') 정의 검사 - 8.4에서 엄격해짐"""
        return self._compat.check_enum_empty_value(schema)

    def check_timestamp_range(self, schema: str) -> List[CompatibilityIssue]:
        """TIMESTAMP 2038년 범위 제한 검사"""
        return self._compat.check_timestamp_range(schema)

    # ------------------------------------------------------------
    # 정리 작업 (OrphanCleanupPlanner 위임)
    # ------------------------------------------------------------
    def generate_cleanup_sql(
        self,
        orphan: OrphanRecord,
        action: ActionType,
        schema: str,
        dry_run: bool = True
    ) -> CleanupAction:
        """고아 레코드 정리 SQL 생성"""
        return self._cleanup.generate_cleanup_sql(orphan, action, schema, dry_run)

    def execute_cleanup(
        self,
        action: CleanupAction,
        dry_run: bool = True
    ) -> Tuple[bool, str, int]:
        """정리 작업 실행 (dry-run 영향 분석; dry_run=False는 항상 RuntimeError)"""
        return self._cleanup.execute_cleanup(action, dry_run)

    # ------------------------------------------------------------
    # 스키마 전체 분석 (오케스트레이션)
    # ------------------------------------------------------------
    def analyze_schema(
        self,
        schema: str,
        check_orphans: bool = True,
        check_charset: bool = True,
        check_keywords: bool = True,
        check_routines: bool = True,
        check_sql_mode: bool = True,
        check_auth_plugins: bool = True,
        check_zerofill: bool = True,
        check_float_precision: bool = True,
        check_fk_name_length: bool = True,
        check_invalid_dates: bool = True,
        check_year2: bool = True,
        check_deprecated_engines: bool = True,
        check_enum_empty: bool = True,
        check_timestamp_range: bool = True,
        check_int_display_width: bool = True
    ) -> AnalysisResult:
        """
        스키마 전체 분석

        Args:
            schema: 분석할 스키마명
            check_orphans: 고아 레코드 검사 여부
            check_charset: 문자셋 이슈 검사 여부
            check_keywords: 예약어 충돌 검사 여부
            check_routines: 저장 프로시저/함수 검사 여부
            check_sql_mode: SQL 모드 검사 여부
            check_auth_plugins: 인증 플러그인 검사 여부
            check_zerofill: ZEROFILL 속성 검사 여부
            check_float_precision: FLOAT(M,D) 구문 검사 여부
            check_fk_name_length: FK 이름 길이 검사 여부
            check_invalid_dates: 0000-00-00 날짜 검사 여부
            check_year2: YEAR(2) 타입 검사 여부
            check_deprecated_engines: deprecated 스토리지 엔진 검사 여부
            check_enum_empty: ENUM 빈 문자열 검사 여부
            check_timestamp_range: TIMESTAMP 2038년 범위 검사 여부
            check_int_display_width: INT(11) 등 표시 너비 검사 여부

        Returns:
            AnalysisResult
        """
        self._log(f"📊 스키마 '{schema}' 분석 시작...")

        # 15개 check_* 불리언을 값 객체로 묶어 impl 로 단 한 번 전달한다.
        options = SchemaCheckOptions(
            check_orphans=check_orphans,
            check_charset=check_charset,
            check_keywords=check_keywords,
            check_routines=check_routines,
            check_sql_mode=check_sql_mode,
            check_auth_plugins=check_auth_plugins,
            check_zerofill=check_zerofill,
            check_float_precision=check_float_precision,
            check_fk_name_length=check_fk_name_length,
            check_invalid_dates=check_invalid_dates,
            check_year2=check_year2,
            check_deprecated_engines=check_deprecated_engines,
            check_enum_empty=check_enum_empty,
            check_timestamp_range=check_timestamp_range,
            check_int_display_width=check_int_display_width,
        )

        # INFORMATION_SCHEMA 조회 시 COLUMN_DEFAULT '0000-00-00' 값이 있으면
        # MySQL strict mode(NO_ZERO_DATE)가 1525 오류를 발생시킴.
        # 분석 단계는 READ-ONLY이므로 세션 sql_mode를 임시 완화 후 복원.
        original_sql_mode = self.connector.get_session_sql_mode()
        self.connector.set_session_sql_mode('')

        try:
            return self._analyze_schema_impl(schema, options)
        finally:
            self.connector.set_session_sql_mode(original_sql_mode)

    def _analyze_schema_impl(
        self,
        schema: str,
        options: SchemaCheckOptions
    ) -> 'AnalysisResult':
        """analyze_schema 내부 구현 (sql_mode 완화 상태에서 실행)"""
        from datetime import datetime

        # 기본 정보 수집
        tables = self.connector.get_tables(schema)
        fk_list = self.get_foreign_keys(schema)
        fk_tree = self.build_fk_tree(schema)

        self._log(f"  테이블 수: {len(tables)}, FK 관계: {len(fk_list)}")

        result = AnalysisResult(
            schema=schema,
            analyzed_at=datetime.now().isoformat(),
            total_tables=len(tables),
            total_fk_relations=len(fk_list),
            fk_tree=fk_tree
        )

        # 호환성 검사 스텝을 선언형으로 정의한다: (활성화 플래그, 로그 라벨, 검사 호출).
        # 고아 레코드 검사(check_orphans)는 두 줄 로그 + cleanup 생성이 얽혀 있어
        # 아래에서 [1/N]로 별도 처리하고, 나머지는 [2/N]...[N/N]로 자동 번호매김한다.
        compat_steps = [
            (options.check_charset, "문자셋 이슈 검사...", lambda: self.check_charset_issues(schema)),
            (options.check_keywords, "예약어 충돌 검사...", lambda: self.check_reserved_keywords(schema)),
            (options.check_routines, "저장 프로시저/함수 검사...", lambda: self.check_deprecated_in_routines(schema)),
            (options.check_sql_mode, "SQL 모드 검사...", lambda: self.check_sql_modes()),
            (options.check_auth_plugins, "인증 플러그인 검사...", lambda: self.check_auth_plugins()),
            (options.check_zerofill, "ZEROFILL 속성 검사...", lambda: self.check_zerofill_columns(schema)),
            (options.check_float_precision, "FLOAT(M,D) 구문 검사...", lambda: self.check_float_precision(schema)),
            (options.check_fk_name_length, "FK 이름 길이 검사...", lambda: self.check_fk_name_length(schema)),
            (options.check_invalid_dates, "0000-00-00 날짜값 검사...", lambda: self.check_invalid_date_values(schema)),
            (options.check_year2, "YEAR(2) 타입 검사...", lambda: self.check_year2_type(schema)),
            (options.check_deprecated_engines, "deprecated 스토리지 엔진 검사...", lambda: self.check_deprecated_engines(schema)),
            (options.check_enum_empty, "ENUM 빈 문자열 검사...", lambda: self.check_enum_empty_value(schema)),
            (options.check_timestamp_range, "TIMESTAMP 범위 검사...", lambda: self.check_timestamp_range(schema)),
            (options.check_int_display_width, "INT 표시 너비 검사...", lambda: self.check_int_display_width(schema)),
        ]
        total_steps = len(compat_steps) + 1  # +1: 고아 레코드 검사 스텝

        # 고아 레코드 검사 (스텝 1)
        if options.check_orphans and fk_list:
            self._log(f"📌 [1/{total_steps}] 고아 레코드 검사 시작...")
            result.orphan_records = self.find_orphan_records(schema)
            self._log(f"✅ [1/{total_steps}] 고아 레코드 검사 완료 (발견: {len(result.orphan_records)}건)")

        # 호환성 검사들 (스텝 2..N — 번호/총계 자동 계산)
        for step_no, (enabled, label, run_check) in enumerate(compat_steps, start=2):
            if enabled:
                self._log(f"📌 [{step_no}/{total_steps}] {label}")
                result.compatibility_issues.extend(run_check())

        # 정리 작업 생성 (고아 레코드에 대해)
        for orphan in result.orphan_records:
            # 기본적으로 DELETE 작업 생성 (dry-run)
            cleanup = self.generate_cleanup_sql(orphan, ActionType.DELETE, schema, dry_run=True)
            result.cleanup_actions.append(cleanup)

        self._log("✅ 분석 완료")
        self._log(f"  - 고아 레코드: {len(result.orphan_records)}개 FK 관계에서 발견")
        self._log(f"  - 호환성 이슈: {len(result.compatibility_issues)}개")

        return result
