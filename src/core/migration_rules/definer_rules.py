"""
Definer/스키마 충돌 규칙 믹스인 (S23-S25)

SchemaRules에 합쳐지는 mysql 스키마 충돌(S23)과 루틴/뷰 Definer 존재 검사
(S24-S25) 모음. self._log_summary / self.connector 등 공통 기능은 SchemaRules가
상속하는 ProgressLoggingRuleBase에서 온다.
"""

from typing import List, Optional, Tuple

from ..migration_constants import (
    IssueType,
    CompatibilityIssue,
    MYSQL_SCHEMA_TABLES,
)


class DefinerRulesMixin:
    """Definer/스키마 충돌 관련 규칙 (S23-S25)"""

    # ================================================================
    # S23: MySQL 스키마 충돌 검사 (라이브 DB)
    # ================================================================
    def check_mysql_schema_conflict(self, schema: str) -> List[CompatibilityIssue]:
        """mysql 스키마 내부 테이블명과 충돌 확인"""
        if not self.connector:
            return []

        self._log("🔍 MySQL 스키마 충돌 검사 중...")
        issues = []

        tables = self.connector.get_tables(schema)
        conflicts = [t for t in tables if t.lower() in MYSQL_SCHEMA_TABLES]

        for table in conflicts:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.MYSQL_SCHEMA_CONFLICT,
                severity="error",
                location=f"{schema}.{table}",
                description=f"테이블명 '{table}'이 mysql 스키마 내부 테이블과 충돌",
                suggestion="테이블명 변경 필요",
                table_name=table
            ))

        self._log_summary(issues, "MySQL 스키마 충돌", "MySQL 스키마 충돌 없음")

        return issues

    # ================================================================
    # S24-S25: Definer 검사 (라이브 DB)
    # ================================================================
    def _fetch_existing_definers_or_issue(
        self, schema: str, issue_type: IssueType, object_label: str
    ) -> Tuple[Optional[set], Optional[CompatibilityIssue]]:
        """mysql.user에서 현재 존재하는 definer(user@host) 집합을 조회한다

        조회 자체가 실패하면(권한 부족 등) 빈 집합으로 대체해 모든
        definer를 "존재하지 않음"으로 오판(spam)하지 않도록, 검증
        불가 상태를 나타내는 info 이슈 1건을 대신 반환한다.
        호출부는 info 이슈가 반환되면 definer 목록과 비교하지 않고
        그 이슈 하나만 결과로 사용해야 한다.
        """
        try:
            users_query = "SELECT CONCAT(User, '@', Host) as definer FROM mysql.user"
            users = self.connector.execute(users_query)
            existing_users = {u['definer'].lower() for u in users}
            return existing_users, None
        except Exception:
            info_issue = CompatibilityIssue(
                issue_type=issue_type,
                severity="info",
                location=schema,
                description=f"{object_label} Definer 검증 불가: mysql.user 조회 권한 부족 또는 접근 실패",
                suggestion="mysql.user 조회 권한이 있는 계정으로 재검사하거나 DEFINER 계정을 수동 확인하세요"
            )
            return None, info_issue

    def check_routine_definer_missing(self, schema: str) -> List[CompatibilityIssue]:
        """저장 프로시저/함수의 definer가 존재하지 않는 사용자인지 확인"""
        if not self.connector:
            return []

        self._log("🔍 루틴 Definer 검사 중...")
        issues = []

        query = """
        SELECT ROUTINE_NAME, ROUTINE_TYPE, DEFINER
        FROM INFORMATION_SCHEMA.ROUTINES
        WHERE ROUTINE_SCHEMA = %s
        """
        routines = self.connector.execute(query, (schema,))

        if not routines:
            return issues

        existing_users, info_issue = self._fetch_existing_definers_or_issue(
            schema, IssueType.ROUTINE_DEFINER_MISSING, "루틴"
        )
        if info_issue:
            return [info_issue]

        for routine in routines:
            definer = routine.get('DEFINER', '')
            if definer and definer.lower() not in existing_users:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.ROUTINE_DEFINER_MISSING,
                    severity="warning",
                    location=f"{routine['ROUTINE_TYPE']} {schema}.{routine['ROUTINE_NAME']}",
                    description=f"Definer '{definer}'가 존재하지 않음",
                    suggestion="Definer를 존재하는 사용자로 변경하거나 사용자 생성 필요"
                ))

        self._log_summary(issues, "루틴 Definer 누락", "루틴 Definer 정상")

        return issues

    def check_view_definer_missing(self, schema: str) -> List[CompatibilityIssue]:
        """뷰의 definer가 존재하지 않는 사용자인지 확인"""
        if not self.connector:
            return []

        self._log("🔍 뷰 Definer 검사 중...")
        issues = []

        query = """
        SELECT TABLE_NAME, DEFINER
        FROM INFORMATION_SCHEMA.VIEWS
        WHERE TABLE_SCHEMA = %s
        """
        views = self.connector.execute(query, (schema,))

        if not views:
            return issues

        existing_users, info_issue = self._fetch_existing_definers_or_issue(
            schema, IssueType.VIEW_DEFINER_MISSING, "뷰"
        )
        if info_issue:
            return [info_issue]

        for view in views:
            definer = view.get('DEFINER', '')
            if definer and definer.lower() not in existing_users:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.VIEW_DEFINER_MISSING,
                    severity="warning",
                    location=f"VIEW {schema}.{view['TABLE_NAME']}",
                    description=f"Definer '{definer}'가 존재하지 않음",
                    suggestion="Definer를 존재하는 사용자로 변경하거나 사용자 생성 필요"
                ))

        self._log_summary(issues, "뷰 Definer 누락", "뷰 Definer 정상")

        return issues
