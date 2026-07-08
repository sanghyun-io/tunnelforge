"""
MySQL 마이그레이션 분석기
- 고아 레코드(orphan rows) 탐지
- FK 관계 분석 및 정리
- MySQL 8.0.x → 8.4.x 호환성 검사 (Upgrade Checker 통합)
- dry-run 지원
- 덤프 파일 분석 (SQL/TSV)
"""
import re
from typing import List, Dict, Tuple, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from src.core.db_connector import MySQLConnector

# ============================================================
# 새 상수 모듈에서 import (migration_constants.py)
# ============================================================
from src.core.migration_constants import (
    ALL_REMOVED_FUNCTIONS,
    DEPRECATED_FUNCTIONS_84,
    OBSOLETE_SQL_MODES,
    IssueType,
    CompatibilityIssue,
    INVALID_DATE_PATTERN,
    INVALID_DATETIME_PATTERN,
    ZEROFILL_PATTERN,
    FLOAT_PRECISION_PATTERN,
    FK_NAME_LENGTH_PATTERN,
    AUTH_PLUGIN_PATTERN,
    FTS_TABLE_PREFIX_PATTERN,
    SUPER_PRIVILEGE_PATTERN,
    SYS_VAR_USAGE_PATTERN,
    ENGINE_POLICIES,
)


# IssueType은 migration_constants에서 import됨


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


# CompatibilityIssue는 migration_constants에서 import (단일 정의)


@dataclass
class CleanupAction:
    """정리 작업"""
    action_type: ActionType
    table: str
    description: str
    sql: str
    affected_rows: int
    dry_run: bool = True
    # dry-run 시 COUNT 쿼리를 만들 때 쓰는 실행 메타데이터.
    # sql 텍스트를 문자열 분해(split)로 재파싱하지 않기 위해 생성 시점에
    # 직접 저장해둔다 (테이블명에 FROM/WHERE/SET 같은 키워드가 포함돼도 안전).
    target_schema: Optional[str] = None
    target_table: Optional[str] = None


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

    def to_dict(self) -> dict:
        """JSON 직렬화용 딕셔너리 변환"""
        import dataclasses
        return {
            'schema': self.schema,
            'analyzed_at': self.analyzed_at,
            'total_tables': self.total_tables,
            'total_fk_relations': self.total_fk_relations,
            'orphan_records': [dataclasses.asdict(o) for o in self.orphan_records],
            'compatibility_issues': [
                {**dataclasses.asdict(i), 'issue_type': i.issue_type.value}
                for i in self.compatibility_issues
            ],
            'cleanup_actions': [
                {**dataclasses.asdict(a), 'action_type': a.action_type.value}
                for a in self.cleanup_actions
            ],
            'fk_tree': self.fk_tree
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'AnalysisResult':
        """딕셔너리에서 AnalysisResult 복원"""
        orphan_records = [OrphanRecord(**o) for o in data.get('orphan_records', [])]
        compatibility_issues = [
            CompatibilityIssue(
                issue_type=IssueType(i['issue_type']),
                severity=i['severity'],
                location=i['location'],
                description=i['description'],
                suggestion=i['suggestion'],
                fix_query=i.get('fix_query'),
                doc_link=i.get('doc_link'),
                upgrade_check_id=i.get('upgrade_check_id'),
                code_snippet=i.get('code_snippet'),
                table_name=i.get('table_name'),
                column_name=i.get('column_name')
            )
            for i in data.get('compatibility_issues', [])
        ]
        cleanup_actions = [
            CleanupAction(
                action_type=ActionType(a['action_type']),
                table=a['table'],
                description=a['description'],
                sql=a['sql'],
                affected_rows=a['affected_rows'],
                dry_run=a.get('dry_run', True),
                target_schema=a.get('target_schema'),
                target_table=a.get('target_table')
            )
            for a in data.get('cleanup_actions', [])
        ]

        return cls(
            schema=data['schema'],
            analyzed_at=data['analyzed_at'],
            total_tables=data['total_tables'],
            total_fk_relations=data['total_fk_relations'],
            orphan_records=orphan_records,
            compatibility_issues=compatibility_issues,
            cleanup_actions=cleanup_actions,
            fk_tree=data.get('fk_tree', {})
        )


class MigrationAnalyzer:
    """마이그레이션 분석기"""

    # MySQL 8.4에서 제거된/deprecated된 함수들 (전역 상수 사용)
    DEPRECATED_FUNCTIONS = list(ALL_REMOVED_FUNCTIONS)
    # deprecated만 (경고 수준 차등화용)
    _DEPRECATED_ONLY = set(DEPRECATED_FUNCTIONS_84)

    # MySQL 8.4에서 새로운 예약어들 (기존 22개 + 8.4 추가 4개)
    NEW_RESERVED_KEYWORDS = [
        'CUME_DIST', 'DENSE_RANK', 'EMPTY', 'EXCEPT', 'FIRST_VALUE',
        'GROUPING', 'GROUPS', 'JSON_TABLE', 'LAG', 'LAST_VALUE', 'LATERAL',
        'LEAD', 'NTH_VALUE', 'NTILE', 'OF', 'OVER', 'PERCENT_RANK',
        'RANK', 'RECURSIVE', 'ROW_NUMBER', 'SYSTEM', 'WINDOW',
        # MySQL 8.4 추가 예약어
        'MANUAL', 'PARALLEL', 'QUALIFY', 'TABLESAMPLE'
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

    def _get_table_row_count(self, schema: str, table: str) -> int:
        """테이블 대략적인 행 수 조회 (INFORMATION_SCHEMA 사용, 빠름)"""
        query = f"""
        SELECT TABLE_ROWS
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{table}'
        """
        result = self.connector.execute(query)
        return result[0]['TABLE_ROWS'] if result and result[0]['TABLE_ROWS'] else 0

    def find_orphan_records(
        self,
        schema: str,
        sample_limit: int = 5,
        large_table_threshold: int = 500000  # 50만 행 이상이면 큰 테이블
    ) -> List[OrphanRecord]:
        """고아 레코드 탐지 (부모 없는 자식 레코드)"""
        import time
        self._log("🔍 고아 레코드 탐지 중...")

        fk_list = self.get_foreign_keys(schema)
        orphans = []

        for i, fk in enumerate(fk_list, 1):
            try:
                # 테이블 크기 사전 확인
                child_rows = self._get_table_row_count(schema, fk.child_table)
                parent_rows = self._get_table_row_count(schema, fk.parent_table)
                is_large = child_rows > large_table_threshold or parent_rows > large_table_threshold

                size_info = ""
                if child_rows > 100000 or parent_rows > 100000:
                    size_info = f" [자식:{child_rows:,}행, 부모:{parent_rows:,}행]"

                self._log(f"  검사 중: {fk.child_table}.{fk.child_column} → {fk.parent_table}.{fk.parent_column} ({i}/{len(fk_list)}){size_info}")

                start_time = time.time()

                if is_large:
                    # 큰 테이블: NOT EXISTS 사용 (더 빠름)
                    self._log(f"    📊 대용량 테이블 - 최적화 쿼리 사용")
                    count_query = f"""
                    SELECT COUNT(*) as cnt
                    FROM `{schema}`.`{fk.child_table}` c
                    WHERE c.`{fk.child_column}` IS NOT NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM `{schema}`.`{fk.parent_table}` p
                            WHERE p.`{fk.parent_column}` = c.`{fk.child_column}`
                        )
                    """
                else:
                    # 일반 테이블: LEFT JOIN 사용
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

                elapsed = time.time() - start_time
                if elapsed > 3:  # 3초 이상 걸리면 경고
                    self._log(f"    ⏱️ 쿼리 소요시간: {elapsed:.1f}초")

                if orphan_count > 0:
                    # 샘플 값 조회 (항상 LIMIT으로 제한)
                    if is_large:
                        sample_query = f"""
                        SELECT DISTINCT c.`{fk.child_column}` as orphan_value
                        FROM `{schema}`.`{fk.child_table}` c
                        WHERE c.`{fk.child_column}` IS NOT NULL
                            AND NOT EXISTS (
                                SELECT 1 FROM `{schema}`.`{fk.parent_table}` p
                                WHERE p.`{fk.parent_column}` = c.`{fk.child_column}`
                            )
                        LIMIT {sample_limit}
                        """
                    else:
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

            except Exception as e:
                self._log(f"    ❌ 검사 실패: {fk.child_table}.{fk.child_column} - {str(e)}")
                continue

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

    def generate_cleanup_sql(
        self,
        orphan: OrphanRecord,
        action: ActionType,
        schema: str,
        dry_run: bool = True
    ) -> CleanupAction:
        """고아 레코드 정리 SQL 생성

        NOT IN 대신 NOT EXISTS를 사용한다. 부모 테이블의 참조 컬럼에 NULL이
        하나라도 있으면 `col NOT IN (SELECT ... )`의 서브쿼리 결과에 NULL이
        섞여 비교 결과가 전부 UNKNOWN이 되어, 실제로는 고아 레코드가 있어도
        0건으로 처리되는 NULL-안전성 문제가 있다. find_orphan_records()의
        대용량 테이블 경로와 동일하게 NOT EXISTS로 통일한다.
        """
        if action == ActionType.DELETE:
            sql = f"""DELETE c FROM `{schema}`.`{orphan.child_table}` AS c
WHERE c.`{orphan.child_column}` IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM `{schema}`.`{orphan.parent_table}` AS p
        WHERE p.`{orphan.parent_column}` = c.`{orphan.child_column}`
    )"""
            description = f"{orphan.child_table}에서 고아 레코드 {orphan.orphan_count}개 삭제"

        elif action == ActionType.SET_NULL:
            sql = f"""UPDATE `{schema}`.`{orphan.child_table}` AS c
SET c.`{orphan.child_column}` = NULL
WHERE c.`{orphan.child_column}` IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM `{schema}`.`{orphan.parent_table}` AS p
        WHERE p.`{orphan.parent_column}` = c.`{orphan.child_column}`
    )"""
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
            dry_run=dry_run,
            target_schema=schema,
            target_table=orphan.child_table
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
        if not dry_run:
            raise RuntimeError(
                "Legacy Python cleanup mutation execution is disabled. "
                "DB mutations must be owned by Rust Core."
            )

        if dry_run:
            # dry-run: 실제 실행하지 않고 영향받는 행 수 확인
            self._log(f"🔍 [DRY-RUN] 영향 분석: {action.table}")

            if action.action_type == ActionType.MANUAL:
                return True, "수동 처리 필요", 0

            if not action.target_schema or not action.target_table:
                # sql 텍스트를 split('FROM')/split('UPDATE') 등으로 재파싱해
                # 테이블명을 추측하지 않는다. 테이블명이 SETTINGS/ASSETS처럼
                # FROM/SET 같은 키워드를 포함하면 잘못 잘려나가는 문제가 있었다.
                # 메타데이터가 없으면(예: 구버전 직렬화 복원) 추측 대신 명시적으로 실패시킨다.
                return False, "❌ 정리 대상 메타데이터 없음", 0

            # COUNT 쿼리로 변환하여 영향받는 행 수 확인
            # DELETE/UPDATE의 WHERE 절만 추출하고, 테이블은 생성 시점에
            # CleanupAction에 저장해둔 target_schema/target_table을 그대로 사용한다.
            sql_upper = action.sql.upper()
            if 'WHERE' not in sql_upper:
                return True, "[DRY-RUN] 영향 분석 완료", action.affected_rows

            where_idx = action.sql.upper().find('WHERE')
            where_clause = action.sql[where_idx:]

            count_sql = (
                f"SELECT COUNT(*) as cnt FROM `{action.target_schema}`.`{action.target_table}` AS c "
                f"{where_clause}"
            )
            result = self.connector.execute(count_sql)
            affected = result[0]['cnt'] if result else 0

            return True, f"[DRY-RUN] {affected}개 행이 영향받음", affected

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
        from datetime import datetime

        self._log(f"📊 스키마 '{schema}' 분석 시작...")

        # INFORMATION_SCHEMA 조회 시 COLUMN_DEFAULT '0000-00-00' 값이 있으면
        # MySQL strict mode(NO_ZERO_DATE)가 1525 오류를 발생시킴.
        # 분석 단계는 READ-ONLY이므로 세션 sql_mode를 임시 완화 후 복원.
        original_sql_mode = self.connector.get_session_sql_mode()
        self.connector.set_session_sql_mode('')

        try:
            return self._analyze_schema_impl(
                schema=schema,
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
        finally:
            self.connector.set_session_sql_mode(original_sql_mode)

    def _analyze_schema_impl(
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

        # 고아 레코드 검사
        if check_orphans and fk_list:
            self._log("📌 [1/15] 고아 레코드 검사 시작...")
            result.orphan_records = self.find_orphan_records(schema)
            self._log(f"✅ [1/15] 고아 레코드 검사 완료 (발견: {len(result.orphan_records)}건)")

        # 호환성 검사들 (기존)
        if check_charset:
            self._log("📌 [2/15] 문자셋 이슈 검사...")
            result.compatibility_issues.extend(self.check_charset_issues(schema))

        if check_keywords:
            self._log("📌 [3/15] 예약어 충돌 검사...")
            result.compatibility_issues.extend(self.check_reserved_keywords(schema))

        if check_routines:
            self._log("📌 [4/15] 저장 프로시저/함수 검사...")
            result.compatibility_issues.extend(self.check_deprecated_in_routines(schema))

        if check_sql_mode:
            self._log("📌 [5/15] SQL 모드 검사...")
            result.compatibility_issues.extend(self.check_sql_modes())

        # MySQL 8.4 Upgrade Checker 검사들
        if check_auth_plugins:
            self._log("📌 [6/15] 인증 플러그인 검사...")
            result.compatibility_issues.extend(self.check_auth_plugins())

        if check_zerofill:
            self._log("📌 [7/15] ZEROFILL 속성 검사...")
            result.compatibility_issues.extend(self.check_zerofill_columns(schema))

        if check_float_precision:
            self._log("📌 [8/15] FLOAT(M,D) 구문 검사...")
            result.compatibility_issues.extend(self.check_float_precision(schema))

        if check_fk_name_length:
            self._log("📌 [9/15] FK 이름 길이 검사...")
            result.compatibility_issues.extend(self.check_fk_name_length(schema))

        if check_invalid_dates:
            self._log("📌 [10/15] 0000-00-00 날짜값 검사...")
            result.compatibility_issues.extend(self.check_invalid_date_values(schema))

        # 추가 호환성 검사들
        if check_year2:
            self._log("📌 [11/15] YEAR(2) 타입 검사...")
            result.compatibility_issues.extend(self.check_year2_type(schema))

        if check_deprecated_engines:
            self._log("📌 [12/15] deprecated 스토리지 엔진 검사...")
            result.compatibility_issues.extend(self.check_deprecated_engines(schema))

        if check_enum_empty:
            self._log("📌 [13/15] ENUM 빈 문자열 검사...")
            result.compatibility_issues.extend(self.check_enum_empty_value(schema))

        if check_timestamp_range:
            self._log("📌 [14/15] TIMESTAMP 범위 검사...")
            result.compatibility_issues.extend(self.check_timestamp_range(schema))

        if check_int_display_width:
            self._log("📌 [15/15] INT 표시 너비 검사...")
            result.compatibility_issues.extend(self.check_int_display_width(schema))

        # 정리 작업 생성 (고아 레코드에 대해)
        for orphan in result.orphan_records:
            # 기본적으로 DELETE 작업 생성 (dry-run)
            cleanup = self.generate_cleanup_sql(orphan, ActionType.DELETE, schema, dry_run=True)
            result.cleanup_actions.append(cleanup)

        self._log("✅ 분석 완료")
        self._log(f"  - 고아 레코드: {len(result.orphan_records)}개 FK 관계에서 발견")
        self._log(f"  - 호환성 이슈: {len(result.compatibility_issues)}개")

        return result

    # ============================================================
    # MySQL 8.4 Upgrade Checker 검사 메서드들 (신규)
    # ============================================================

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

    def check_zerofill_columns(self, schema: str) -> List[CompatibilityIssue]:
        """ZEROFILL 속성 사용 컬럼 확인"""
        self._log("🔍 ZEROFILL 속성 확인 중...")

        issues = []

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND COLUMN_TYPE LIKE '%%ZEROFILL%%'
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.ZEROFILL_USAGE,
                severity="warning",
                location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                description=f"ZEROFILL 속성 사용: {col['COLUMN_TYPE']}",
                suggestion="ZEROFILL은 deprecated됨, 애플리케이션에서 LPAD() 등으로 처리 권장"
            ))

        if issues:
            self._log(f"  ⚠️ ZEROFILL 사용 {len(issues)}개 발견")
        else:
            self._log("  ✅ ZEROFILL 사용 없음")

        return issues

    def check_float_precision(self, schema: str) -> List[CompatibilityIssue]:
        """FLOAT(M,D), DOUBLE(M,D) 구문 확인"""
        self._log("🔍 FLOAT/DOUBLE 정밀도 구문 확인 중...")

        issues = []

        # FLOAT(M,D), DOUBLE(M,D) 형태 확인
        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE IN ('float', 'double')
            AND COLUMN_TYPE REGEXP '^(float|double)\\\\([0-9]+,[0-9]+\\\\)'
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.FLOAT_PRECISION,
                severity="warning",
                location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                description=f"FLOAT/DOUBLE 정밀도 구문 사용: {col['COLUMN_TYPE']}",
                suggestion="FLOAT(M,D) 구문은 deprecated됨, FLOAT 또는 DECIMAL(M,D) 사용 권장"
            ))

        if issues:
            self._log(f"  ⚠️ FLOAT/DOUBLE 정밀도 구문 {len(issues)}개 발견")
        else:
            self._log("  ✅ FLOAT/DOUBLE 구문 정상")

        return issues

    def check_fk_name_length(self, schema: str) -> List[CompatibilityIssue]:
        """FK 이름 64자 초과 확인"""
        self._log("🔍 FK 이름 길이 확인 중...")

        issues = []

        query = """
        SELECT CONSTRAINT_NAME, TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
        WHERE TABLE_SCHEMA = %s
            AND CONSTRAINT_TYPE = 'FOREIGN KEY'
            AND LENGTH(CONSTRAINT_NAME) > 64
        """
        fks = self.connector.execute(query, (schema,))

        for fk in fks:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.FK_NAME_LENGTH,
                severity="error",
                location=f"{schema}.{fk['TABLE_NAME']}.{fk['CONSTRAINT_NAME']}",
                description=f"FK 이름이 64자 초과: {len(fk['CONSTRAINT_NAME'])}자",
                suggestion="FK 이름을 64자 이하로 변경 필요 (8.4 제한)"
            ))

        if issues:
            self._log(f"  ⚠️ FK 이름 길이 초과 {len(issues)}개 발견")
        else:
            self._log("  ✅ FK 이름 길이 정상")

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

    def check_int_display_width(self, schema: str) -> List[CompatibilityIssue]:
        """INT(11) 등 표시 너비 사용 확인 (TINYINT(1) 제외)"""
        self._log("🔍 INT 표시 너비 확인 중...")

        issues = []

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE IN ('tinyint', 'smallint', 'mediumint', 'int', 'bigint')
            AND COLUMN_TYPE REGEXP '^(tinyint|smallint|mediumint|int|bigint)\\\\([0-9]+\\\\)'
            AND NOT (DATA_TYPE = 'tinyint' AND COLUMN_TYPE LIKE 'tinyint(1)%%')
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.INT_DISPLAY_WIDTH,
                severity="info",
                location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                description=f"INT 표시 너비 사용: {col['COLUMN_TYPE']}",
                suggestion="표시 너비는 deprecated됨, 8.4에서 자동 무시됨 (영향 최소)"
            ))

        if issues:
            self._log(f"  ℹ️ INT 표시 너비 {len(issues)}개 발견 (경미)")
        else:
            self._log("  ✅ INT 표시 너비 없음")

        return issues

    def check_year2_type(self, schema: str) -> List[CompatibilityIssue]:
        """YEAR(2) 타입 검사 - MySQL 8.0에서 제거됨"""
        self._log("🔍 YEAR(2) 타입 확인 중...")

        issues = []

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND COLUMN_TYPE = 'year(2)'
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.YEAR2_TYPE,
                severity="error",
                location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                description="YEAR(2) 타입 사용 - MySQL 8.0에서 제거됨",
                suggestion="YEAR(4) 또는 YEAR로 변경 필요",
                table_name=col['TABLE_NAME'],
                column_name=col['COLUMN_NAME'],
                fix_query=f"ALTER TABLE `{schema}`.`{col['TABLE_NAME']}` MODIFY `{col['COLUMN_NAME']}` YEAR;"
            ))

        if issues:
            self._log(f"  ⚠️ YEAR(2) 타입 {len(issues)}개 발견")
        else:
            self._log("  ✅ YEAR(2) 타입 없음")

        return issues

    def check_deprecated_engines(self, schema: str) -> List[CompatibilityIssue]:
        """deprecated 스토리지 엔진 검사"""
        self._log("🔍 deprecated 스토리지 엔진 확인 중...")

        issues = []

        query = """
        SELECT TABLE_NAME, ENGINE
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s
            AND TABLE_TYPE = 'BASE TABLE'
            AND ENGINE IS NOT NULL
        """
        tables = self.connector.execute(query, (schema,))

        for table in tables:
            engine = table['ENGINE']
            if engine in ENGINE_POLICIES:
                policy = ENGINE_POLICIES[engine]
                severity = policy['severity']
                suggestion = policy['suggestion']
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.DEPRECATED_ENGINE,
                    severity=severity,
                    location=f"{schema}.{table['TABLE_NAME']}",
                    description=f"deprecated 스토리지 엔진: {engine}",
                    suggestion=suggestion,
                    table_name=table['TABLE_NAME'],
                    fix_query=f"ALTER TABLE `{schema}`.`{table['TABLE_NAME']}` ENGINE=InnoDB;" if engine != 'MEMORY' else None
                ))

        if issues:
            self._log(f"  ⚠️ deprecated 엔진 {len(issues)}개 발견")
        else:
            self._log("  ✅ deprecated 엔진 없음")

        return issues

    def check_enum_empty_value(self, schema: str) -> List[CompatibilityIssue]:
        """ENUM 빈 문자열('') 정의 검사 - 8.4에서 엄격해짐"""
        self._log("🔍 ENUM 빈 문자열 확인 중...")

        issues = []

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE = 'enum'
            AND COLUMN_TYPE LIKE "%%''%%"
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.ENUM_EMPTY_VALUE,
                severity="warning",
                location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                description="ENUM에 빈 문자열('') 정의됨",
                suggestion="빈 문자열 대신 NULL 허용 또는 명시적 값 사용 권장",
                table_name=col['TABLE_NAME'],
                column_name=col['COLUMN_NAME']
            ))

        if issues:
            self._log(f"  ⚠️ ENUM 빈 문자열 {len(issues)}개 발견")
        else:
            self._log("  ✅ ENUM 빈 문자열 없음")

        return issues

    def check_timestamp_range(self, schema: str) -> List[CompatibilityIssue]:
        """TIMESTAMP 2038년 범위 제한 검사

        TIMESTAMP는 애초에 '2038-01-19 03:14:07' UTC를 초과하는 값을 저장할
        수 없는 타입이므로, 저장된 데이터를 `WHERE col > '2038-01-19 03:14:07'`
        로 조회해 "범위 초과 데이터"를 찾으려는 시도는 절대 참이 될 수 없어
        항상 0건만 반환하는 무의미한 검사였다. 대신 TIMESTAMP 컬럼이 존재한다는
        사실 자체를 스키마 레벨 advisory(경고)로 보고한다.
        """
        self._log("🔍 TIMESTAMP 범위 확인 중...")

        issues = []

        # TIMESTAMP 컬럼 조회
        col_query = """
        SELECT TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE = 'timestamp'
        """
        columns = self.connector.execute(col_query, (schema,))

        for col in columns:
            table = col['TABLE_NAME']
            column = col['COLUMN_NAME']

            issues.append(CompatibilityIssue(
                issue_type=IssueType.TIMESTAMP_RANGE,
                severity="warning",
                location=f"{schema}.{table}.{column}",
                description="TIMESTAMP 컬럼은 2038년 범위 제한이 있습니다",
                suggestion="2038년 이후 값이 필요한 컬럼은 DATETIME으로 변경을 검토하세요",
                table_name=table,
                column_name=column,
                fix_query=f"ALTER TABLE `{schema}`.`{table}` MODIFY `{column}` DATETIME;"
            ))

        if issues:
            self._log(f"  ⚠️ TIMESTAMP 범위 제한 컬럼 {len(issues)}개 발견")
        else:
            self._log("  ✅ TIMESTAMP 컬럼 없음")

        return issues

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

        def print_tree(table: str, prefix: str = "", is_last: bool = True, visited: set = None):
            if visited is None:
                visited = set()

            connector = "└── " if is_last else "├── "

            # 순환 참조 감지
            if table in visited:
                lines.append(f"{prefix}{connector}🔄 {table} (순환 참조)")
                return

            lines.append(f"{prefix}{connector}{table}")

            if table in fk_tree:
                children = fk_tree[table]
                child_prefix = prefix + ("    " if is_last else "│   ")
                for i, child in enumerate(children):
                    print_tree(child, child_prefix, i == len(children) - 1, visited | {table})

        for i, root in enumerate(sorted(root_tables)):
            print_tree(root, "", i == len(root_tables) - 1, set())

        return "\n".join(lines)


# ============================================================
# 덤프 파일 분석기 (Task 3)
# ============================================================

@dataclass
class DumpAnalysisResult:
    """덤프 파일 분석 결과"""
    dump_path: str
    analyzed_at: str
    total_sql_files: int
    total_tsv_files: int
    compatibility_issues: List[CompatibilityIssue] = field(default_factory=list)


class DumpFileAnalyzer:
    """
    dump 파일 분석기

    덤프 폴더의 SQL/TSV 파일을 분석하여 MySQL 8.4 호환성 이슈를 탐지합니다.
    """

    def __init__(self):
        self._progress_callback: Optional[Callable[[str], None]] = None
        self._issue_callback: Optional[Callable[[CompatibilityIssue], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def set_issue_callback(self, callback: Callable[[CompatibilityIssue], None]):
        """이슈 발견 시 콜백 설정"""
        self._issue_callback = callback

    def _log(self, message: str):
        """진행 상황 로깅"""
        if self._progress_callback:
            self._progress_callback(message)

    def _report_issue(self, issue: CompatibilityIssue):
        """이슈 발견 시 콜백 호출"""
        if self._issue_callback:
            self._issue_callback(issue)

    def analyze_dump_folder(self, dump_path: str) -> DumpAnalysisResult:
        """
        덤프 폴더 전체 분석

        Args:
            dump_path: dump 폴더 경로

        Returns:
            DumpAnalysisResult
        """
        from datetime import datetime

        path = Path(dump_path)
        if not path.exists():
            raise FileNotFoundError(f"덤프 폴더를 찾을 수 없습니다: {dump_path}")

        self._log(f"🔍 덤프 폴더 분석 시작: {dump_path}")

        issues: List[CompatibilityIssue] = []

        # SQL 파일 목록
        sql_files = list(path.glob("*.sql"))
        tsv_files = list(path.glob("*.tsv")) + list(path.glob("*.tsv.zst"))

        self._log(f"  SQL 파일: {len(sql_files)}개, 데이터 파일: {len(tsv_files)}개")

        # SQL 파일 분석
        for i, sql_file in enumerate(sql_files, 1):
            self._log(f"  [{i}/{len(sql_files)}] {sql_file.name} 분석 중...")
            file_issues = self._analyze_sql_file(sql_file)
            issues.extend(file_issues)

            # 실시간 이슈 콜백
            for issue in file_issues:
                self._report_issue(issue)

        # TSV 데이터 파일 분석 (0000-00-00 날짜 등)
        # 압축되지 않은 TSV 파일만 분석 (압축 파일은 너무 느림)
        uncompressed_tsv = [f for f in tsv_files if not str(f).endswith('.zst')]
        if uncompressed_tsv:
            for i, tsv_file in enumerate(uncompressed_tsv, 1):
                self._log(f"  [{i}/{len(uncompressed_tsv)}] {tsv_file.name} 분석 중...")
                file_issues = self._analyze_tsv_file(tsv_file)
                issues.extend(file_issues)

                for issue in file_issues:
                    self._report_issue(issue)

        # 결과 생성
        result = DumpAnalysisResult(
            dump_path=str(dump_path),
            analyzed_at=datetime.now().isoformat(),
            total_sql_files=len(sql_files),
            total_tsv_files=len(tsv_files),
            compatibility_issues=issues
        )

        # 요약
        error_count = sum(1 for i in issues if i.severity == "error")
        warning_count = sum(1 for i in issues if i.severity == "warning")

        self._log("✅ 덤프 분석 완료")
        self._log(f"  - 오류: {error_count}개")
        self._log(f"  - 경고: {warning_count}개")

        return result

    def _analyze_sql_file(self, file_path: Path) -> List[CompatibilityIssue]:
        """
        SQL 파일 분석 - 스키마 호환성 검사

        Args:
            file_path: SQL 파일 경로

        Returns:
            발견된 이슈 목록
        """
        issues = []

        try:
            # 대용량 파일 가드레일: 100MB 초과 시 경고 후 스킵
            MAX_SQL_FILE_SIZE = 100 * 1024 * 1024  # 100MB
            file_size = file_path.stat().st_size
            if file_size > MAX_SQL_FILE_SIZE:
                self._log(f"  ⚠️ 파일 크기 초과 ({file_size // (1024*1024)}MB > 100MB): {file_path.name} - 스키마 분석 스킵")
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.SCAN_TRUNCATED,
                    severity="warning",
                    location=file_path.name,
                    description=f"SQL 파일이 너무 큼 ({file_size // (1024*1024)}MB): 스키마 호환성 검사 스킵",
                    suggestion="파일을 분할하거나 라이브 DB 모드로 직접 분석하세요"
                ))
                return issues

            content = file_path.read_text(encoding='utf-8', errors='replace')

            # 1. ZEROFILL 속성 검사
            for match in ZEROFILL_PATTERN.finditer(content):
                # 컨텍스트에서 테이블/컬럼 이름 추출 시도
                line_start = content.rfind('\n', 0, match.start()) + 1
                line_end = content.find('\n', match.end())
                line = content[line_start:line_end]

                issues.append(CompatibilityIssue(
                    issue_type=IssueType.ZEROFILL_USAGE,
                    severity="warning",
                    location=f"{file_path.name}",
                    description=f"ZEROFILL 속성 사용: {line.strip()[:80]}...",
                    suggestion="ZEROFILL은 deprecated됨"
                ))

            # 2. FLOAT(M,D), DOUBLE(M,D) 구문 검사
            for match in FLOAT_PRECISION_PATTERN.finditer(content):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.FLOAT_PRECISION,
                    severity="warning",
                    location=f"{file_path.name}",
                    description=f"FLOAT/DOUBLE 정밀도 구문: {match.group(0)}",
                    suggestion="FLOAT(M,D) 구문은 deprecated됨"
                ))

            # 3. FK 이름 64자 초과 검사
            for match in FK_NAME_LENGTH_PATTERN.finditer(content):
                fk_name = match.group(1)
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.FK_NAME_LENGTH,
                    severity="error",
                    location=f"{file_path.name}",
                    description=f"FK 이름 64자 초과: {fk_name[:30]}... ({len(fk_name)}자)",
                    suggestion="FK 이름을 64자 이하로 변경 필요"
                ))

            # 4. 인증 플러그인 검사
            for match in AUTH_PLUGIN_PATTERN.finditer(content):
                plugin = match.group(1).lower()
                # removed(fido 계열)=error, disabled(native)=error, deprecated(sha256)=warning
                if plugin in ('authentication_fido', 'authentication_fido_client'):
                    severity = "error"
                    desc = f"{plugin} 플러그인 사용 (8.4에서 제거됨)"
                elif plugin == 'mysql_native_password':
                    severity = "error"
                    desc = f"{plugin} 인증 사용 (8.4에서 기본 비활성화)"
                else:
                    severity = "warning"
                    desc = f"{plugin} 인증 사용 (deprecated)"
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.AUTH_PLUGIN_ISSUE,
                    severity=severity,
                    location=f"{file_path.name}",
                    description=desc,
                    suggestion="caching_sha2_password 사용 권장"
                ))

            # 5. FTS_ 테이블명 검사
            for match in FTS_TABLE_PREFIX_PATTERN.finditer(content):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.FTS_TABLE_PREFIX,
                    severity="error",
                    location=f"{file_path.name}",
                    description="FTS_ 접두사 테이블명 (내부 예약어)",
                    suggestion="FTS_ 접두사는 내부 전문 검색용으로 예약됨, 테이블명 변경 필요"
                ))

            # 6. SUPER 권한 검사
            for match in SUPER_PRIVILEGE_PATTERN.finditer(content):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.SUPER_PRIVILEGE,
                    severity="warning",
                    location=f"{file_path.name}",
                    description="SUPER 권한 사용 (deprecated)",
                    suggestion="동적 권한 (BINLOG_ADMIN, CONNECTION_ADMIN 등)으로 세분화 권장"
                ))

            # 7. 제거된 시스템 변수 사용 검사
            for match in SYS_VAR_USAGE_PATTERN.finditer(content):
                var_name = match.group(1)
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.REMOVED_SYS_VAR,
                    severity="error",
                    location=f"{file_path.name}",
                    description=f"제거된 시스템 변수 사용: {var_name}",
                    suggestion=f"'{var_name}'은 8.4에서 제거됨, 대체 방법 확인 필요"
                ))

            # 8. 예약어 충돌 (테이블/컬럼 이름) - CREATE TABLE 문에서
            table_pattern = re.compile(
                r'CREATE\s+TABLE\s+`?(\w+)`?\s*\(',
                re.IGNORECASE
            )
            column_pattern = re.compile(
                r'`(\w+)`\s+(?:INT|VARCHAR|TEXT|DATE|DECIMAL|FLOAT|DOUBLE|CHAR|BLOB|ENUM|SET)',
                re.IGNORECASE
            )

            keywords_upper = set(k.upper() for k in MigrationAnalyzer.NEW_RESERVED_KEYWORDS)

            for match in table_pattern.finditer(content):
                table_name = match.group(1)
                if table_name.upper() in keywords_upper:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.RESERVED_KEYWORD,
                        severity="error",
                        location=f"{file_path.name}",
                        description=f"테이블명 '{table_name}'이 예약어와 충돌",
                        suggestion="테이블명 변경 또는 백틱(`) 사용 필요"
                    ))

            for match in column_pattern.finditer(content):
                column_name = match.group(1)
                if column_name.upper() in keywords_upper:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.RESERVED_KEYWORD,
                        severity="warning",
                        location=f"{file_path.name}",
                        description=f"컬럼명 '{column_name}'이 예약어와 충돌",
                        suggestion="컬럼 참조 시 백틱(`) 사용 필요"
                    ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")

        return issues

    def _analyze_tsv_file(self, file_path: Path) -> List[CompatibilityIssue]:
        """
        TSV 데이터 파일 분석 - 데이터 무결성 검사

        Args:
            file_path: TSV 파일 경로

        Returns:
            발견된 이슈 목록
        """
        issues = []
        invalid_date_count = 0

        try:
            # 대용량 파일은 샘플링
            max_lines = 10000
            line_count = 0

            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line_count += 1
                    if line_count > max_lines:
                        break

                    # 0000-00-00 날짜 검사
                    if INVALID_DATE_PATTERN.search(line) or INVALID_DATETIME_PATTERN.search(line):
                        invalid_date_count += 1

            if invalid_date_count > 0:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.INVALID_DATE,
                    severity="error",
                    location=f"{file_path.name}",
                    description=f"잘못된 날짜 값 발견: {invalid_date_count}개 행 (0000-00-00)",
                    suggestion="NO_ZERO_DATE SQL 모드 활성화 시 오류 발생, 유효한 날짜로 변환 필요"
                ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")

        return issues

    def quick_scan(self, dump_path: str) -> Tuple[int, int, int]:
        """
        빠른 스캔 - 이슈 개수만 반환

        Args:
            dump_path: 덤프 폴더 경로

        Returns:
            (오류 수, 경고 수, 정보 수)
        """
        try:
            result = self.analyze_dump_folder(dump_path)
            error_count = sum(1 for i in result.compatibility_issues if i.severity == "error")
            warning_count = sum(1 for i in result.compatibility_issues if i.severity == "warning")
            info_count = sum(1 for i in result.compatibility_issues if i.severity == "info")
            return error_count, warning_count, info_count
        except Exception as e:
            self._log(f"  ⚠️ 요약 카운트 오류: {str(e)[:80]}")
            return 0, 0, 0
