"""
마이그레이션 자동 수정 롤백 SQL 생성기
"""
from typing import List, Dict, Set, Optional, Tuple, Any

from src.core.db_connector import MySQLConnector
from src.core.migration_fix_models import FixStrategy, _format_default_sql_clause


class RollbackSQLGenerator:
    """Rollback SQL 생성기

    DDL(ALTER TABLE)은 auto-commit되므로 트랜잭션 롤백이 불가능합니다.
    대신 변경 전 상태를 기록하고, 원래 상태로 되돌리는 SQL을 생성합니다.
    """

    def __init__(self, connector: MySQLConnector, schema: str):
        self.connector = connector
        self.schema = schema
        # 변경 전 상태 캐시
        self._table_charset_cache: Dict[str, Dict[str, str]] = {}
        self._column_info_cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _format_default_clause(col_info: Dict[str, Any]) -> str:
        """COLUMN_DEFAULT 값 → DEFAULT 절 문자열 생성

        SmartFixGenerator와 공유하는 모듈 레벨 헬퍼(_format_default_sql_clause)에 위임한다.
        """
        return _format_default_sql_clause(col_info)

    @staticmethod
    def _format_extra_clause(col_info: Dict[str, Any]) -> str:
        """EXTRA 필드 → SQL 절 생성 (AUTO_INCREMENT, ON UPDATE 등)

        'DEFAULT_GENERATED' 등 내부 마킹은 생략하고 유의미한 속성만 출력.
        """
        extra = (col_info.get('EXTRA') or '').lower()
        if not extra:
            return ''
        parts = []
        if 'auto_increment' in extra:
            parts.append('AUTO_INCREMENT')
        if 'on update current_timestamp' in extra:
            parts.append('ON UPDATE CURRENT_TIMESTAMP')
        return ' '.join(parts)

    def capture_table_charset(self, table: str) -> Dict[str, str]:
        """테이블의 현재 charset/collation 캡처"""
        cache_key = f"{self.schema}.{table}"
        if cache_key in self._table_charset_cache:
            return self._table_charset_cache[cache_key]

        query = """
        SELECT
            TABLE_NAME,
            TABLE_COLLATION,
            CCSA.CHARACTER_SET_NAME as TABLE_CHARSET
        FROM INFORMATION_SCHEMA.TABLES T
        LEFT JOIN INFORMATION_SCHEMA.COLLATION_CHARACTER_SET_APPLICABILITY CCSA
            ON T.TABLE_COLLATION = CCSA.COLLATION_NAME
        WHERE T.TABLE_SCHEMA = %s AND T.TABLE_NAME = %s
        """
        result = self.connector.execute(query, (self.schema, table))

        if result:
            info = {
                'charset': result[0]['TABLE_CHARSET'] or 'utf8mb3',
                'collation': result[0]['TABLE_COLLATION'] or 'utf8mb3_general_ci'
            }
        else:
            info = {'charset': 'utf8mb3', 'collation': 'utf8mb3_general_ci'}

        self._table_charset_cache[cache_key] = info
        return info

    def _get_fk_sql_for_tables(self, schema: str, tables: List[str]) -> Tuple[List[str], List[str]]:
        """대상 테이블의 FK DROP/ADD SQL 조회

        Returns:
            (drop_sqls, add_sqls) 튜플
        """
        if not tables or not self.connector:
            return [], []

        placeholders = ", ".join(["%s"] * len(tables))
        query = f"""
        SELECT
            kcu.CONSTRAINT_NAME,
            kcu.TABLE_NAME,
            kcu.COLUMN_NAME,
            kcu.REFERENCED_TABLE_NAME,
            kcu.REFERENCED_COLUMN_NAME,
            kcu.ORDINAL_POSITION,
            rc.DELETE_RULE,
            rc.UPDATE_RULE
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            AND kcu.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
        WHERE kcu.TABLE_SCHEMA = %s
            AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
            AND (kcu.TABLE_NAME IN ({placeholders}) OR kcu.REFERENCED_TABLE_NAME IN ({placeholders}))
        ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """
        try:
            params = (schema,) + tuple(tables) + tuple(tables)
            rows = self.connector.execute(query, params)
        except Exception:
            return [], []

        # 복합 FK 그룹화
        fk_map: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = f"{row['TABLE_NAME']}.{row['CONSTRAINT_NAME']}"
            if key not in fk_map:
                fk_map[key] = {
                    'constraint': row['CONSTRAINT_NAME'],
                    'table': row['TABLE_NAME'],
                    'columns': [],
                    'ref_table': row['REFERENCED_TABLE_NAME'],
                    'ref_columns': [],
                    'on_delete': row.get('DELETE_RULE', 'RESTRICT'),
                    'on_update': row.get('UPDATE_RULE', 'RESTRICT'),
                }
            fk_map[key]['columns'].append(row['COLUMN_NAME'])
            fk_map[key]['ref_columns'].append(row['REFERENCED_COLUMN_NAME'])

        drop_sqls = []
        add_sqls = []
        for fk in fk_map.values():
            drop_sqls.append(
                f"ALTER TABLE `{schema}`.`{fk['table']}` DROP FOREIGN KEY `{fk['constraint']}`;"
            )
            cols = ", ".join(f"`{c}`" for c in fk['columns'])
            ref_cols = ", ".join(f"`{c}`" for c in fk['ref_columns'])
            add_sqls.append(
                f"ALTER TABLE `{schema}`.`{fk['table']}` ADD CONSTRAINT `{fk['constraint']}` "
                f"FOREIGN KEY ({cols}) REFERENCES `{fk['ref_table']}` ({ref_cols}) "
                f"ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']};"
            )

        return drop_sqls, add_sqls

    def capture_column_info(self, table: str, column: str) -> Dict[str, Any]:
        """컬럼의 현재 정보 캡처 (charset 포함)"""
        cache_key = f"{self.schema}.{table}.{column}"
        if cache_key in self._column_info_cache:
            return self._column_info_cache[cache_key]

        query = """
        SELECT
            COLUMN_NAME,
            COLUMN_TYPE,
            IS_NULLABLE,
            COLUMN_DEFAULT,
            CHARACTER_SET_NAME,
            COLLATION_NAME,
            EXTRA
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """
        result = self.connector.execute(query, (self.schema, table, column))

        if result:
            info = dict(result[0])
        else:
            info = {}

        self._column_info_cache[cache_key] = info
        return info

    def capture_tables_state(self, tables: Set[str]) -> Dict[str, Dict[str, str]]:
        """여러 테이블의 상태 일괄 캡처"""
        states = {}
        for table in tables:
            states[table] = self.capture_table_charset(table)
        return states

    def generate_rollback_sql(
        self,
        step: 'FixWizardStep',
        original_state: Optional[Dict[str, Any]] = None,
        all_pre_states: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> str:
        """단일 step에 대한 Rollback SQL 생성

        Args:
            step: 실행된 FixWizardStep
            original_state: 변경 전 상태 (없으면 캐시에서 조회)
            all_pre_states: 전체 pre-state 맵 (FK 일괄 변경 시 연관 테이블 상태 조회용)

        Returns:
            Rollback SQL 문자열
        """
        if not step.selected_option:
            return ""

        strategy = step.selected_option.strategy

        # 건너뛰기/수동 처리는 롤백 불필요
        if strategy in (FixStrategy.SKIP, FixStrategy.MANUAL):
            return ""

        location_parts = step.location.split('.')
        if len(location_parts) < 2:
            return ""

        schema = location_parts[0]
        table = location_parts[1]
        column = location_parts[2] if len(location_parts) > 2 else None

        lines = []

        # === 날짜 수정 롤백 ===
        if strategy in (FixStrategy.DATE_TO_NULL, FixStrategy.DATE_TO_MIN, FixStrategy.DATE_TO_CUSTOM):
            lines.append(f"-- ⚠️ 날짜 값 롤백 불가")
            lines.append(f"-- 원본 값이 0000-00-00이었으므로 복원할 값을 알 수 없습니다.")
            lines.append(f"-- 테이블: {table}, 컬럼: {column}")
            lines.append(f"-- 백업 데이터에서 복원하거나 수동으로 처리하세요.")
            return "\n".join(lines)

        # === Collation 롤백 ===
        if strategy == FixStrategy.COLLATION_SINGLE:
            if column:
                # 컬럼 레벨 롤백
                col_info = original_state or self.capture_column_info(table, column)
                if col_info:
                    orig_charset = col_info.get('CHARACTER_SET_NAME', 'utf8mb3')
                    orig_collation = col_info.get('COLLATION_NAME', 'utf8mb3_general_ci')
                    col_type = col_info.get('COLUMN_TYPE', 'VARCHAR(255)')
                    nullable = 'NULL' if col_info.get('IS_NULLABLE') == 'YES' else 'NOT NULL'
                    default_clause = self._format_default_clause(col_info)
                    extra_clause = self._format_extra_clause(col_info)

                    # 컬럼 정의: type nullable [default] [extra] charset collation
                    col_def_parts = [col_type, nullable]
                    if default_clause:
                        col_def_parts.append(default_clause)
                    if extra_clause:
                        col_def_parts.append(extra_clause)
                    col_def_parts.append(
                        f"CHARACTER SET {orig_charset} COLLATE {orig_collation}"
                    )

                    lines.append(f"-- Rollback: {table}.{column} 컬럼 charset 복원")
                    lines.append(f"-- 원본: {orig_charset} / {orig_collation}")
                    lines.append(
                        f"ALTER TABLE `{schema}`.`{table}` "
                        f"MODIFY COLUMN `{column}` {' '.join(col_def_parts)};"
                    )
            else:
                # 테이블 레벨 롤백
                tbl_info = original_state or self.capture_table_charset(table)
                orig_charset = tbl_info.get('charset', 'utf8mb3')
                orig_collation = tbl_info.get('collation', 'utf8mb3_general_ci')

                lines.append(f"-- Rollback: {table} 테이블 charset 복원")
                lines.append(f"-- 원본: {orig_charset} / {orig_collation}")
                lines.append(
                    f"ALTER TABLE `{schema}`.`{table}` "
                    f"CONVERT TO CHARACTER SET {orig_charset} COLLATE {orig_collation};"
                )

        elif strategy in (FixStrategy.COLLATION_FK_CASCADE, FixStrategy.COLLATION_FK_SAFE):
            # FK 일괄 변경 롤백 - 모든 연관 테이블 복원
            related_tables = step.selected_option.related_tables or [table]

            lines.append(f"-- Rollback: FK 연관 테이블 일괄 charset 복원")
            lines.append(f"-- 대상 테이블: {', '.join(related_tables)}")
            lines.append("")

            # FK 안전 변경과 동일하게 FK DROP → 변경 → FK 재생성 구조
            # FK SQL 조회 (concrete SQL 생성)
            drop_sqls, add_sqls = [], []
            if strategy == FixStrategy.COLLATION_FK_SAFE:
                drop_sqls, add_sqls = self._get_fk_sql_for_tables(schema, related_tables)

                lines.append("-- Phase 1: FK 임시 DROP")
                if drop_sqls:
                    for sql in drop_sqls:
                        lines.append(sql)
                else:
                    lines.append("-- (FK 정의 조회 실패 - 원본 실행 로그 참조)")
                lines.append("")

            lines.append("-- Phase 2: Charset 복원")
            for tbl in related_tables:
                # pre-state 우선 사용 (변경 전 상태), 없으면 현재 상태 캡처 (fallback)
                # 테이블 레벨 키(schema.table) 먼저 조회, 없으면 컬럼 레벨 키도 탐색
                tbl_location = f"{schema}.{tbl}"
                tbl_info = None
                if all_pre_states:
                    if tbl_location in all_pre_states:
                        tbl_info = all_pre_states[tbl_location]
                    else:
                        # 컬럼 레벨 키 중 해당 테이블 소속 첫 번째 항목 사용
                        for key, val in all_pre_states.items():
                            if key.startswith(f"{tbl_location}."):
                                tbl_info = val
                                break
                if tbl_info is None:
                    if original_state and tbl == table:
                        tbl_info = original_state
                    else:
                        tbl_info = self.capture_table_charset(tbl)
                orig_charset = tbl_info.get('charset', 'utf8mb3')
                orig_collation = tbl_info.get('collation', 'utf8mb3_general_ci')

                lines.append(f"-- {tbl}: {orig_charset} / {orig_collation}")
                lines.append(
                    f"ALTER TABLE `{schema}`.`{tbl}` "
                    f"CONVERT TO CHARACTER SET {orig_charset} COLLATE {orig_collation};"
                )

            if strategy == FixStrategy.COLLATION_FK_SAFE:
                lines.append("")
                lines.append("-- Phase 3: FK 재생성")
                if add_sqls:
                    for sql in add_sqls:
                        lines.append(sql)
                else:
                    lines.append("-- (FK 정의 조회 실패 - 원본 실행 로그 참조)")

        return "\n".join(lines)

    def generate_batch_rollback(
        self,
        steps: List['FixWizardStep'],
        pre_states: Dict[str, Dict[str, Any]]
    ) -> str:
        """배치 실행에 대한 전체 Rollback SQL 생성

        Args:
            steps: 실행된 FixWizardStep 목록
            pre_states: 변경 전 상태 맵 (location -> state)

        Returns:
            전체 Rollback SQL 문자열
        """
        from datetime import datetime

        lines = []
        lines.append("-- " + "=" * 60)
        lines.append("-- 마이그레이션 자동 수정 ROLLBACK SQL")
        lines.append(f"-- 스키마: {self.schema}")
        lines.append(f"-- 생성일시: {datetime.now().isoformat()}")
        lines.append("-- " + "=" * 60)
        lines.append("")
        lines.append("-- ⚠️ 주의사항:")
        lines.append("-- 1. 이 파일은 변경 전 상태로 되돌리기 위한 SQL입니다.")
        lines.append("-- 2. DDL(ALTER TABLE)은 트랜잭션 롤백이 불가능하므로")
        lines.append("--    문제 발생 시 이 SQL을 수동으로 실행하세요.")
        lines.append("-- 3. 날짜 값 변경은 원본 값을 알 수 없어 자동 롤백이 불가능합니다.")
        lines.append("-- 4. 실행 전 반드시 내용을 확인하세요.")
        lines.append("")
        lines.append("")

        # 이미 처리한 테이블/컬럼 추적 (중복 방지)
        processed_tables: Set[str] = set()      # 테이블 레벨 중복 방지
        processed_locations: Set[str] = set()  # 컬럼 레벨 COLLATION_SINGLE 중복 방지
        rollback_count = 0

        for step in steps:
            if not step.selected_option:
                continue

            if step.selected_option.strategy == FixStrategy.SKIP:
                continue

            # 자동 포함된 테이블은 건너뛰기 (원본 step에서 처리)
            if step.included_by is not None:
                continue

            location = step.location
            location_parts = location.split('.')
            table = location_parts[1] if len(location_parts) > 1 else location
            column = location_parts[2] if len(location_parts) > 2 else None
            strategy = step.selected_option.strategy

            if strategy in (FixStrategy.COLLATION_FK_CASCADE, FixStrategy.COLLATION_FK_SAFE):
                # FK 일괄 변경: 연관 테이블 전체를 테이블 단위로 중복 방지
                tables_to_check = set(step.selected_option.related_tables or [table])
                if tables_to_check & processed_tables:
                    continue
                processed_tables.update(tables_to_check)
            elif strategy == FixStrategy.COLLATION_SINGLE and column:
                # 컬럼 레벨: 같은 테이블의 여러 컬럼이 각각 롤백되어야 하므로
                # 테이블 단위가 아닌 location 전체를 키로 사용
                if location in processed_locations:
                    continue
                processed_locations.add(location)
            else:
                # 테이블 레벨: 테이블 단위 중복 방지
                if table in processed_tables:
                    continue
                processed_tables.add(table)

            # 원본 상태 가져오기
            original_state = pre_states.get(location)

            rollback_sql = self.generate_rollback_sql(step, original_state, all_pre_states=pre_states)
            if rollback_sql:
                rollback_count += 1
                lines.append(f"-- [{rollback_count}] {location}")
                lines.append(f"-- 전략: {step.selected_option.label}")
                lines.append(rollback_sql)
                lines.append("")

        if rollback_count == 0:
            lines.append("-- (롤백 가능한 변경사항이 없습니다)")

        return "\n".join(lines)
