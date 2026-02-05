"""
MySQL 8.0 → 8.4 마이그레이션 Auto-Recommendation Engine

이슈 타입별 최적의 수정 전략을 자동으로 권장합니다.
- IssueType별 권장 FixStrategy 매핑
- 컨텍스트 기반 권장 (nullable 여부, FK 관계 등)
- 리스크 스코어 계산
"""
from dataclasses import dataclass
from typing import List, Dict, Optional, Any, Tuple
from enum import Enum

from src.core.db_connector import MySQLConnector
from src.core.migration_constants import IssueType
from src.core.migration_fix_wizard import (
    FixStrategy, FixOption, FixWizardStep, SmartFixGenerator
)


class RecommendationReason(Enum):
    """권장 이유"""
    FK_SAFE = "fk_safe"              # FK 충돌 방지
    DATA_PRESERVE = "data_preserve"   # 데이터 손실 최소화
    NO_ACTION = "no_action"           # 조치 불필요 (자동 무시)
    STANDARD = "standard"             # 표준/권장 방식
    MANUAL_REQUIRED = "manual"        # 수동 처리 필요


@dataclass
class RecommendationRule:
    """권장 규칙"""
    strategy: FixStrategy
    reason: RecommendationReason
    description: str
    risk_base_score: int = 0  # 기본 리스크 점수 (0-100)


# IssueType별 기본 권장 규칙
DEFAULT_RECOMMENDATION_RULES: Dict[IssueType, RecommendationRule] = {
    # Charset/Collation - FK 안전 변경 권장
    IssueType.CHARSET_ISSUE: RecommendationRule(
        strategy=FixStrategy.COLLATION_FK_SAFE,
        reason=RecommendationReason.FK_SAFE,
        description="FK 충돌(Error 3780) 방지를 위한 안전한 변경",
        risk_base_score=20
    ),

    # Invalid Date - nullable 여부에 따라 다름 (동적 처리)
    IssueType.INVALID_DATE: RecommendationRule(
        strategy=FixStrategy.DATE_TO_NULL,  # nullable일 때
        reason=RecommendationReason.DATA_PRESERVE,
        description="데이터 손실 최소화",
        risk_base_score=30
    ),

    # INT Display Width - 8.4에서 자동 무시되므로 건너뛰기
    IssueType.INT_DISPLAY_WIDTH: RecommendationRule(
        strategy=FixStrategy.SKIP,
        reason=RecommendationReason.NO_ACTION,
        description="MySQL 8.4에서 자동 무시됨 (영향 없음)",
        risk_base_score=0
    ),

    # ZEROFILL - 수동 처리 (앱 수정 필요)
    IssueType.ZEROFILL_USAGE: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="애플리케이션 코드 수정 필요 (LPAD 함수 사용)",
        risk_base_score=10
    ),

    # Deprecated Engine - InnoDB로 변경
    IssueType.DEPRECATED_ENGINE: RecommendationRule(
        strategy=FixStrategy.MANUAL,  # ENGINE_TO_INNODB 대신 MANUAL (옵션에서 선택)
        reason=RecommendationReason.STANDARD,
        description="InnoDB가 표준 스토리지 엔진",
        risk_base_score=40
    ),

    # Float Precision - 수동 처리 권장
    IssueType.FLOAT_PRECISION: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="DECIMAL 타입으로 변경 시 정밀도 유지 가능",
        risk_base_score=15
    ),

    # Enum Empty Value - 수동 처리
    IssueType.ENUM_EMPTY_VALUE: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="빈 문자열 대신 NULL 허용 또는 명시적 값 사용",
        risk_base_score=10
    ),

    # Timestamp Range - DATETIME으로 변경
    IssueType.TIMESTAMP_RANGE: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.DATA_PRESERVE,
        description="2038년 문제 - DATETIME으로 타입 변경 권장",
        risk_base_score=35
    ),

    # Reserved Keyword - 수동 처리 (이름 변경 또는 백틱)
    IssueType.RESERVED_KEYWORD: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="테이블/컬럼명 변경 또는 백틱 사용 필요",
        risk_base_score=25
    ),

    # Auth Plugin - 수동 처리 (ALTER USER)
    IssueType.AUTH_PLUGIN_ISSUE: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="caching_sha2_password로 변경 권장",
        risk_base_score=20
    ),

    # Super Privilege - 수동 처리 (권한 세분화)
    IssueType.SUPER_PRIVILEGE: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="세분화된 동적 권한으로 대체 필요",
        risk_base_score=15
    ),

    # Removed Sys Var - 수동 처리 (설정 파일)
    IssueType.REMOVED_SYS_VAR: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="설정 파일에서 변수 제거 필요",
        risk_base_score=25
    ),

    # GROUP BY ASC/DESC - 수동 처리 (쿼리 수정)
    IssueType.GROUPBY_ASC_DESC: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="ORDER BY 절로 대체 필요",
        risk_base_score=10
    ),

    # SQL_CALC_FOUND_ROWS - 수동 처리 (쿼리 패턴 변경)
    IssueType.SQL_CALC_FOUND_ROWS_USAGE: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="COUNT(*) + LIMIT 패턴으로 대체 필요",
        risk_base_score=10
    ),

    # Partition Issue - 수동 처리
    IssueType.PARTITION_ISSUE: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="파티션 재구성 필요",
        risk_base_score=45
    ),

    # BLOB/TEXT Default - 수동 처리
    IssueType.BLOB_TEXT_DEFAULT: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="DEFAULT 값 제거 필요",
        risk_base_score=10
    ),

    # FK Name Length - 수동 처리
    IssueType.FK_NAME_LENGTH: RecommendationRule(
        strategy=FixStrategy.MANUAL,
        reason=RecommendationReason.MANUAL_REQUIRED,
        description="FK 이름을 64자 이하로 변경 필요",
        risk_base_score=20
    ),
}


@dataclass
class RecommendationSummary:
    """권장 결과 요약"""
    total_issues: int
    auto_fixable: int
    manual_review: int
    skip_recommended: int
    total_risk_score: int
    average_risk_score: float
    high_risk_issues: List[Tuple[Any, int]]  # (issue, risk_score) 리스트


class AutoRecommendationEngine:
    """자동 권장 옵션 선택 엔진"""

    def __init__(self, connector: MySQLConnector, schema: str):
        """
        Args:
            connector: MySQL 연결 객체
            schema: 대상 스키마명
        """
        self.connector = connector
        self.schema = schema
        self._smart_generator: Optional[SmartFixGenerator] = None
        self._column_nullable_cache: Dict[str, bool] = {}

    def get_smart_generator(self) -> SmartFixGenerator:
        """SmartFixGenerator 인스턴스 (lazy init)"""
        if self._smart_generator is None:
            self._smart_generator = SmartFixGenerator(self.connector, self.schema)
        return self._smart_generator

    def recommend_all(
        self,
        issues: List[Any],
        steps: List[FixWizardStep]
    ) -> List[FixWizardStep]:
        """
        모든 이슈에 대해 권장 옵션 자동 선택

        Args:
            issues: CompatibilityIssue 목록
            steps: FixWizardStep 목록 (옵션이 생성된 상태)

        Returns:
            권장 옵션이 선택된 FixWizardStep 목록
        """
        for step in steps:
            if not step.options:
                continue

            # 이슈 찾기
            issue = issues[step.issue_index] if step.issue_index < len(issues) else None
            if not issue:
                continue

            # 권장 옵션 선택
            recommended = self.recommend_for_issue(issue, step.options)
            if recommended:
                step.selected_option = recommended

        return steps

    def recommend_for_issue(
        self,
        issue: Any,
        options: List[FixOption]
    ) -> Optional[FixOption]:
        """
        개별 이슈에 대해 권장 옵션 선택

        Args:
            issue: CompatibilityIssue
            options: 사용 가능한 FixOption 목록

        Returns:
            권장 FixOption 또는 None
        """
        if not options:
            return None

        issue_type = issue.issue_type

        # 1. 이미 권장(is_recommended=True)으로 표시된 옵션이 있으면 선택
        for opt in options:
            if opt.is_recommended:
                return opt

        # 2. 기본 권장 규칙 조회
        rule = DEFAULT_RECOMMENDATION_RULES.get(issue_type)

        if rule:
            # 동적 처리가 필요한 경우
            if issue_type == IssueType.INVALID_DATE:
                return self._recommend_invalid_date(issue, options)

            if issue_type == IssueType.CHARSET_ISSUE:
                return self._recommend_charset(issue, options)

            # 규칙의 전략과 일치하는 옵션 찾기
            for opt in options:
                if opt.strategy == rule.strategy:
                    return opt

        # 3. SKIP이 아닌 첫 번째 옵션 반환 (기본)
        for opt in options:
            if opt.strategy != FixStrategy.SKIP:
                return opt

        # 4. 모두 SKIP이면 첫 번째 반환
        return options[0] if options else None

    def _recommend_invalid_date(
        self,
        issue: Any,
        options: List[FixOption]
    ) -> Optional[FixOption]:
        """Invalid Date 이슈 권장 (nullable 여부 기반)"""
        table = getattr(issue, 'table_name', None)
        column = getattr(issue, 'column_name', None)

        if table and column:
            is_nullable = self._is_column_nullable(table, column)

            if is_nullable:
                # Nullable이면 DATE_TO_NULL 권장
                for opt in options:
                    if opt.strategy == FixStrategy.DATE_TO_NULL:
                        return opt
            else:
                # NOT NULL이면 DATE_TO_MIN 권장
                for opt in options:
                    if opt.strategy == FixStrategy.DATE_TO_MIN:
                        return opt

        # 기본: is_recommended=True 옵션 또는 첫 번째
        for opt in options:
            if opt.is_recommended:
                return opt

        return options[0] if options else None

    def _recommend_charset(
        self,
        issue: Any,
        options: List[FixOption]
    ) -> Optional[FixOption]:
        """Charset 이슈 권장 (FK 안전 변경 우선)"""
        # FK 안전 변경 옵션 우선
        for opt in options:
            if opt.strategy == FixStrategy.COLLATION_FK_SAFE:
                return opt

        # FK 연관 일괄 변경
        for opt in options:
            if opt.strategy == FixStrategy.COLLATION_FK_CASCADE:
                return opt

        # 단일 테이블 변경
        for opt in options:
            if opt.strategy == FixStrategy.COLLATION_SINGLE:
                return opt

        return options[0] if options else None

    def _is_column_nullable(self, table: str, column: str) -> bool:
        """컬럼의 nullable 여부 확인"""
        cache_key = f"{self.schema}.{table}.{column}"
        if cache_key in self._column_nullable_cache:
            return self._column_nullable_cache[cache_key]

        query = """
        SELECT IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """
        result = self.connector.execute(query, (self.schema, table, column))

        is_nullable = result[0]['IS_NULLABLE'] == 'YES' if result else False
        self._column_nullable_cache[cache_key] = is_nullable
        return is_nullable

    def calculate_risk_score(self, issue: Any) -> int:
        """
        이슈의 리스크 스코어 계산 (0-100)

        Args:
            issue: CompatibilityIssue

        Returns:
            리스크 스코어 (0-100)
        """
        base_score = 0
        issue_type = issue.issue_type

        # 기본 규칙에서 base score 가져오기
        rule = DEFAULT_RECOMMENDATION_RULES.get(issue_type)
        if rule:
            base_score = rule.risk_base_score

        # 추가 요소에 따른 보정

        # 1. 데이터 손실 가능성
        if issue_type in [IssueType.INVALID_DATE, IssueType.TIMESTAMP_RANGE]:
            # 실제 데이터가 있으면 추가 리스크
            base_score += 10

        # 2. FK 관련
        if issue_type == IssueType.CHARSET_ISSUE:
            # FK 연관 테이블이 많으면 추가 리스크
            location_parts = issue.location.split('.')
            if len(location_parts) >= 2:
                table = location_parts[1]
                generator = self.get_smart_generator()
                fk_builder = generator.get_fk_graph_builder()
                related = fk_builder.get_related_tables(table)
                if len(related) > 3:
                    base_score += 15

        # 3. 구조 변경
        if issue_type in [IssueType.DEPRECATED_ENGINE, IssueType.PARTITION_ISSUE]:
            base_score += 10

        # 4. 권한 관련
        if issue_type in [IssueType.AUTH_PLUGIN_ISSUE, IssueType.SUPER_PRIVILEGE]:
            base_score += 5

        return min(base_score, 100)

    def get_summary(self, steps: List[FixWizardStep], issues: List[Any]) -> RecommendationSummary:
        """
        권장 결과 요약 생성

        Args:
            steps: FixWizardStep 목록
            issues: CompatibilityIssue 목록

        Returns:
            RecommendationSummary
        """
        auto_fixable = 0
        manual_review = 0
        skip_recommended = 0
        total_risk = 0
        high_risk_issues: List[Tuple[Any, int]] = []

        for step in steps:
            if not step.selected_option:
                manual_review += 1
                continue

            strategy = step.selected_option.strategy

            if strategy == FixStrategy.SKIP:
                skip_recommended += 1
            elif strategy == FixStrategy.MANUAL:
                manual_review += 1
            else:
                auto_fixable += 1

            # 리스크 스코어 계산
            if step.issue_index < len(issues):
                issue = issues[step.issue_index]
                risk = self.calculate_risk_score(issue)
                total_risk += risk

                # 고위험 이슈 (50 이상) 수집
                if risk >= 50:
                    high_risk_issues.append((issue, risk))

        avg_risk = total_risk / len(steps) if steps else 0.0

        return RecommendationSummary(
            total_issues=len(steps),
            auto_fixable=auto_fixable,
            manual_review=manual_review,
            skip_recommended=skip_recommended,
            total_risk_score=total_risk,
            average_risk_score=avg_risk,
            high_risk_issues=high_risk_issues
        )

    def get_execution_order(self, steps: List[FixWizardStep]) -> List[int]:
        """
        실행 순서 결정 (리스크가 낮은 것부터)

        Args:
            steps: FixWizardStep 목록

        Returns:
            실행 순서 (step 인덱스 목록)
        """
        # (index, risk_score) 튜플 목록 생성
        step_risks = []
        for i, step in enumerate(steps):
            if step.selected_option and step.selected_option.strategy not in [FixStrategy.SKIP, FixStrategy.MANUAL]:
                # 간단한 리스크 평가 (strategy 기반)
                risk = 0
                if step.selected_option.strategy == FixStrategy.COLLATION_FK_SAFE:
                    risk = 30
                elif step.selected_option.strategy == FixStrategy.COLLATION_FK_CASCADE:
                    risk = 40
                elif step.selected_option.strategy in [FixStrategy.DATE_TO_NULL, FixStrategy.DATE_TO_MIN]:
                    risk = 20
                else:
                    risk = 10
                step_risks.append((i, risk))

        # 리스크 오름차순 정렬
        step_risks.sort(key=lambda x: x[1])

        return [idx for idx, _ in step_risks]
