"""
MySQL 마이그레이션 분석기
- 고아 레코드(orphan rows) 탐지
- FK 관계 분석 및 정리
- MySQL 8.0.x → 8.4.x 호환성 검사
- dry-run 지원
"""
from typing import List, Dict, Set, Tuple, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
from src.core.db_connector import MySQLConnector


class IssueType(Enum):
    """문제 유형"""
    ORPHAN_ROW = "orphan_row"  # 부모 없는 자식 레코드
    DEPRECATED_FUNCTION = "deprecated_function"  # deprecated 함수 사용
    CHARSET_ISSUE = "charset_issue"  # utf8mb3 → utf8mb4 필요
    RESERVED_KEYWORD = "reserved_keyword"  # 예약어 충돌
    SQL_MODE_ISSUE = "sql_mode_issue"  # deprecated SQL 모드


class ActionType(Enum):
    """조치 유형"""
    DELETE = "delete"  # 삭제
    UPDATE = "update"  # 업데이트
    SET_NULL = "set_null"  # NULL로 설정
    MANUAL = "manual"  # 수동 처리 필요


@dataclass
class OrphanRecord:
    """고아 레코드 정보"""
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    orphan_count: int
    sample_values: List[Any] = field(default_factory=list)


@dataclass
class ForeignKeyInfo:
    """FK 관계 정보"""
    constraint_name: str
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    on_delete: str
    on_update: str


@dataclass
class CompatibilityIssue:
    """호환성 문제"""
    issue_type: IssueType
    severity: str  # "error", "warning", "info"
    location: str  # 테이블명 또는 위치
    description: str
    suggestion: str


@dataclass
class CleanupAction:
    """정리 작업"""
    action_type: ActionType
    table: str
    description: str
    sql: str
    affected_rows: int
    dry_run: bool = True


@dataclass
class AnalysisResult:
    """분석 결과"""
    schema: str
    analyzed_at: str
    total_tables: int
    total_fk_relations: int
    orphan_records: List[OrphanRecord] = field(default_factory=list)
    compatibility_issues: List[CompatibilityIssue] = field(default_factory=list)
    cleanup_actions: List[CleanupAction] = field(default_factory=list)
    fk_tree: Dict[str, List[str]] = field(default_factory=dict)


class MigrationAnalyzer:
    """마이그레이션 분석기"""

    # MySQL 8.4에서 제거된/deprecated된 함수들
    DEPRECATED_FUNCTIONS = [
        'PASSWORD', 'ENCODE', 'DECODE', 'DES_ENCRYPT', 'DES_DECRYPT',
        'ENCRYPT', 'OLD_PASSWORD', 'MASTER_POS_WAIT'
    ]

    # MySQL 8.4에서 새로운 예약어들
    NEW_RESERVED_KEYWORDS = [
        'CUME_DIST', 'DENSE_RANK', 'EMPTY', 'EXCEPT', 'FIRST_VALUE',
        'GROUPING', 'GROUPS', 'JSON_TABLE', 'LAG', 'LAST_VALUE', 'LATERAL',
        'LEAD', 'NTH_VALUE', 'NTILE', 'OF', 'OVER', 'PERCENT_RANK',
        'RANK', 'RECURSIVE', 'ROW_NUMBER', 'SYSTEM', 'WINDOW'
    ]

    def __init__(self, connector: MySQLConnector):
        self.connector = connector
        self._progress_callback: Optional[Callable[[str], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def _log(self, message: str):
        """진행 상황 로깅"""
        if self._progress_callback:
            self._progress_callback(message)

    def get_foreign_keys(self, schema: str) -> List[ForeignKeyInfo]:
        """스키마의 모든 FK 관계 조회"""
        query = """
        SELECT
            tc.CONSTRAINT_NAME,
            kcu.TABLE_NAME as CHILD_TABLE,
            kcu.COLUMN_NAME as CHILD_COLUMN,
            kcu.REFERENCED_TABLE_NAME as PARENT_TABLE,
            kcu.REFERENCED_COLUMN_NAME as PARENT_COLUMN,
            rc.DELETE_RULE,
            rc.UPDATE_RULE
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
            ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
            AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
        JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            ON tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            AND tc.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
        WHERE tc.TABLE_SCHEMA = %s
            AND tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
        ORDER BY kcu.TABLE_NAME, kcu.COLUMN_NAME
        """
        rows = self.connector.execute(query, (schema,))

        fk_list = []
        for row in rows:
            fk_list.append(ForeignKeyInfo(
                constraint_name=row['CONSTRAINT_NAME'],
                child_table=row['CHILD_TABLE'],
                child_column=row['CHILD_COLUMN'],
                parent_table=row['PARENT_TABLE'],
                parent_column=row['PARENT_COLUMN'],
                on_delete=row['DELETE_RULE'],
                on_update=row['UPDATE_RULE']
            ))

        return fk_list

    def build_fk_tree(self, schema: str) -> Dict[str, List[str]]:
        """FK 관계 트리 구성 (부모 → 자식 목록)"""
        fk_list = self.get_foreign_keys(schema)

        tree = {}
        for fk in fk_list:
            if fk.parent_table not in tree:
                tree[fk.parent_table] = []
            if fk.child_table not in tree[fk.parent_table]:
                tree[fk.parent_table].append(fk.child_table)

        return tree

    def find_orphan_records(
        self,
        schema: str,
        sample_limit: int = 5
    ) -> List[OrphanRecord]:
        """고아 레코드 탐지 (부모 없는 자식 레코드)"""
        self._log("🔍 고아 레코드 탐지 중...")

        fk_list = self.get_foreign_keys(schema)
        orphans = []

        for i, fk in enumerate(fk_list, 1):
            self._log(f"  검사 중: {fk.child_table}.{fk.child_column} → {fk.parent_table}.{fk.parent_column} ({i}/{len(fk_list)})")

            # 고아 레코드 수 조회
            count_query = f"""
            SELECT COUNT(*) as cnt
            FROM `{schema}`.`{fk.child_table}` c
            LEFT JOIN `{schema}`.`{fk.parent_table}` p
                ON c.`{fk.child_column}` = p.`{fk.parent_column}`
            WHERE c.`{fk.child_column}` IS NOT NULL
                AND p.`{fk.parent_column}` IS NULL
            """
            result = self.connector.execute(count_query)
            orphan_count = result[0]['cnt'] if result else 0

            if orphan_count > 0:
                # 샘플 값 조회
                sample_query = f"""
                SELECT DISTINCT c.`{fk.child_column}` as orphan_value
                FROM `{schema}`.`{fk.child_table}` c
                LEFT JOIN `{schema}`.`{fk.parent_table}` p
                    ON c.`{fk.child_column}` = p.`{fk.parent_column}`
                WHERE c.`{fk.child_column}` IS NOT NULL
                    AND p.`{fk.parent_column}` IS NULL
                LIMIT {sample_limit}
                """
                samples = self.connector.execute(sample_query)
                sample_values = [s['orphan_value'] for s in samples]

                orphans.append(OrphanRecord(
                    child_table=fk.child_table,
                    child_column=fk.child_column,
                    parent_table=fk.parent_table,
                    parent_column=fk.parent_column,
                    orphan_count=orphan_count,
                    sample_values=sample_values
                ))

                self._log(f"    ⚠️ 고아 레코드 발견: {orphan_count}개")

        return orphans

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
            AND (TABLE_COLLATION LIKE 'utf8_%%' OR TABLE_COLLATION LIKE 'utf8mb3_%%')
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

        # 컬럼 레벨 charset 확인
        column_query = """
        SELECT TABLE_NAME, COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND CHARACTER_SET_NAME IN ('utf8', 'utf8mb3')
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
        keywords_upper = set(k.upper() for k in self.NEW_RESERVED_KEYWORDS)

        # 테이블명 확인
        tables = self.connector.get_tables(schema)
        for table in tables:
            if table.upper() in keywords_upper:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.RESERVED_KEYWORD,
                    severity="error",
                    location=f"{schema}.{table}",
                    description=f"테이블명 '{table}'이 MySQL 8.4 예약어와 충돌",
                    suggestion=f"테이블명을 백틱으로 감싸거나 이름 변경 필요"
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
                if func in definition:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.DEPRECATED_FUNCTION,
                        severity="error",
                        location=f"{routine['ROUTINE_TYPE']} {schema}.{routine['ROUTINE_NAME']}",
                        description=f"deprecated 함수 '{func}' 사용 중",
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

        # deprecated SQL 모드들
        deprecated_modes = [
            'NO_AUTO_CREATE_USER',  # 8.0에서 제거됨
            'NO_FIELD_OPTIONS',
            'NO_KEY_OPTIONS',
            'NO_TABLE_OPTIONS',
        ]

        result = self.connector.execute("SELECT @@sql_mode as sql_mode")
        if result:
            current_modes = result[0]['sql_mode'].split(',')

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

    def generate_cleanup_sql(
        self,
        orphan: OrphanRecord,
        action: ActionType,
        schema: str,
        dry_run: bool = True
    ) -> CleanupAction:
        """고아 레코드 정리 SQL 생성"""
        if action == ActionType.DELETE:
            sql = f"""DELETE FROM `{schema}`.`{orphan.child_table}`
WHERE `{orphan.child_column}` NOT IN (
    SELECT `{orphan.parent_column}` FROM `{schema}`.`{orphan.parent_table}`
)
AND `{orphan.child_column}` IS NOT NULL"""
            description = f"{orphan.child_table}에서 고아 레코드 {orphan.orphan_count}개 삭제"

        elif action == ActionType.SET_NULL:
            sql = f"""UPDATE `{schema}`.`{orphan.child_table}`
SET `{orphan.child_column}` = NULL
WHERE `{orphan.child_column}` NOT IN (
    SELECT `{orphan.parent_column}` FROM `{schema}`.`{orphan.parent_table}`
)
AND `{orphan.child_column}` IS NOT NULL"""
            description = f"{orphan.child_table}.{orphan.child_column}을 NULL로 설정 ({orphan.orphan_count}개)"

        else:
            sql = f"-- 수동 처리 필요: {orphan.child_table}.{orphan.child_column}"
            description = f"{orphan.child_table} 수동 검토 필요"

        return CleanupAction(
            action_type=action,
            table=orphan.child_table,
            description=description,
            sql=sql,
            affected_rows=orphan.orphan_count,
            dry_run=dry_run
        )

    def execute_cleanup(
        self,
        action: CleanupAction,
        dry_run: bool = True
    ) -> Tuple[bool, str, int]:
        """
        정리 작업 실행

        Args:
            action: 실행할 정리 작업
            dry_run: True면 실제 실행하지 않고 영향받는 행 수만 반환

        Returns:
            (성공여부, 메시지, 영향받은 행 수)
        """
        if dry_run:
            # dry-run: 실제 실행하지 않고 영향받는 행 수 확인
            self._log(f"🔍 [DRY-RUN] 영향 분석: {action.table}")

            if action.action_type == ActionType.MANUAL:
                return True, "수동 처리 필요", 0

            # COUNT 쿼리로 변환하여 영향받는 행 수 확인
            # DELETE/UPDATE의 WHERE 절 추출
            sql_upper = action.sql.upper()
            if 'WHERE' in sql_upper:
                where_idx = action.sql.upper().find('WHERE')
                where_clause = action.sql[where_idx:]

                # 테이블명 추출
                if action.action_type == ActionType.DELETE:
                    # DELETE FROM `schema`.`table` WHERE ...
                    count_sql = f"SELECT COUNT(*) as cnt FROM {action.sql.split('FROM')[1].split('WHERE')[0].strip()} {where_clause}"
                else:
                    # UPDATE `schema`.`table` SET ... WHERE ...
                    count_sql = f"SELECT COUNT(*) as cnt FROM {action.sql.split('UPDATE')[1].split('SET')[0].strip()} {where_clause}"

                result = self.connector.execute(count_sql)
                affected = result[0]['cnt'] if result else 0

                return True, f"[DRY-RUN] {affected}개 행이 영향받음", affected

            return True, "[DRY-RUN] 영향 분석 완료", action.affected_rows

        else:
            # 실제 실행
            self._log(f"🔧 실행 중: {action.table}")

            try:
                with self.connector.connection.cursor() as cursor:
                    cursor.execute(action.sql)
                    affected = cursor.rowcount
                    self.connector.connection.commit()

                return True, f"✅ {affected}개 행 처리됨", affected

            except Exception as e:
                self.connector.connection.rollback()
                return False, f"❌ 오류: {str(e)}", 0

    def analyze_schema(
        self,
        schema: str,
        check_orphans: bool = True,
        check_charset: bool = True,
        check_keywords: bool = True,
        check_routines: bool = True,
        check_sql_mode: bool = True
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

        Returns:
            AnalysisResult
        """
        from datetime import datetime

        self._log(f"📊 스키마 '{schema}' 분석 시작...")

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

        # 고아 레코드 검사
        if check_orphans and fk_list:
            result.orphan_records = self.find_orphan_records(schema)

        # 호환성 검사들
        if check_charset:
            result.compatibility_issues.extend(self.check_charset_issues(schema))

        if check_keywords:
            result.compatibility_issues.extend(self.check_reserved_keywords(schema))

        if check_routines:
            result.compatibility_issues.extend(self.check_deprecated_in_routines(schema))

        if check_sql_mode:
            result.compatibility_issues.extend(self.check_sql_modes())

        # 정리 작업 생성 (고아 레코드에 대해)
        for orphan in result.orphan_records:
            # 기본적으로 DELETE 작업 생성 (dry-run)
            cleanup = self.generate_cleanup_sql(orphan, ActionType.DELETE, schema, dry_run=True)
            result.cleanup_actions.append(cleanup)

        self._log(f"✅ 분석 완료")
        self._log(f"  - 고아 레코드: {len(result.orphan_records)}개 FK 관계에서 발견")
        self._log(f"  - 호환성 이슈: {len(result.compatibility_issues)}개")

        return result

    def get_fk_visualization(self, schema: str) -> str:
        """FK 관계를 트리 형태로 시각화"""
        fk_tree = self.build_fk_tree(schema)

        if not fk_tree:
            return "FK 관계가 없습니다."

        lines = ["FK 관계 트리:", ""]

        # 루트 테이블 찾기 (다른 테이블의 자식이 아닌 테이블)
        all_children = set()
        for children in fk_tree.values():
            all_children.update(children)

        root_tables = set(fk_tree.keys()) - all_children

        def print_tree(table: str, prefix: str = "", is_last: bool = True):
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{table}")

            if table in fk_tree:
                children = fk_tree[table]
                child_prefix = prefix + ("    " if is_last else "│   ")
                for i, child in enumerate(children):
                    print_tree(child, child_prefix, i == len(children) - 1)

        for i, root in enumerate(sorted(root_tables)):
            print_tree(root, "", i == len(root_tables) - 1)

        return "\n".join(lines)
