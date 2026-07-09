"""
SQL 자동완성 제공자
- 커서 위치 기반 컨텍스트 분석 (테이블/컬럼/키워드)
- 정규식 기반 파싱 (의존성 없음)
"""
import re
from typing import Dict, List

from src.core.sql_metadata import SchemaMetadata, SchemaMetadataProvider
from src.core.sql_identifier_utils import (
    extract_cte_names, extract_derived_table_aliases, extract_table_aliases,
)


class SQLAutoCompleter:
    """SQL 자동완성 제공자"""

    SQL_KEYWORDS = [
        'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'IN', 'LIKE', 'BETWEEN',
        'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE',
        'CREATE', 'ALTER', 'DROP', 'TABLE', 'INDEX', 'VIEW', 'DATABASE',
        'JOIN', 'INNER', 'LEFT', 'RIGHT', 'OUTER', 'FULL', 'CROSS', 'ON',
        'GROUP', 'BY', 'ORDER', 'ASC', 'DESC', 'HAVING', 'LIMIT', 'OFFSET',
        'UNION', 'ALL', 'DISTINCT', 'AS', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
        'NULL', 'IS', 'EXISTS', 'PRIMARY', 'KEY', 'FOREIGN', 'REFERENCES',
        'CONSTRAINT', 'DEFAULT', 'AUTO_INCREMENT', 'TRUNCATE',
        'BEGIN', 'COMMIT', 'ROLLBACK', 'TRANSACTION',
    ]

    SQL_FUNCTIONS = [
        'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'COALESCE', 'IFNULL', 'NULLIF',
        'CONCAT', 'SUBSTRING', 'LENGTH', 'TRIM', 'UPPER', 'LOWER', 'REPLACE',
        'NOW', 'DATE', 'TIME', 'DATETIME', 'TIMESTAMP', 'YEAR', 'MONTH', 'DAY',
        'HOUR', 'MINUTE', 'SECOND', 'DATEDIFF', 'DATE_ADD', 'DATE_SUB',
        'CAST', 'CONVERT', 'ROUND', 'FLOOR', 'CEIL', 'ABS', 'MOD', 'POWER',
        'GROUP_CONCAT', 'JSON_EXTRACT', 'JSON_ARRAY', 'JSON_OBJECT',
    ]

    def __init__(self, metadata_provider: SchemaMetadataProvider = None):
        self.metadata_provider = metadata_provider or SchemaMetadataProvider()

    def get_completions(self, sql: str, cursor_pos: int, schema: str = None) -> List[Dict]:
        """커서 위치에서 자동완성 목록 반환

        Args:
            sql: SQL 쿼리 문자열
            cursor_pos: 커서 위치
            schema: 대상 스키마

        Returns:
            자동완성 항목 목록 [{label, type, detail}, ...]
        """
        completions = []
        metadata = self.metadata_provider.get_metadata(schema)

        # 커서 앞 텍스트 분석
        text_before = sql[:cursor_pos]
        context = self._analyze_context(text_before)
        prefix = self._get_current_word(text_before)

        if context['type'] == 'table':
            completions.extend(self._complete_tables(metadata, prefix))
        elif context['type'] == 'column':
            target_table = context.get('table')
            if target_table:
                completions.extend(self._complete_columns_for_table(sql, metadata, target_table, prefix))
            else:
                completions.extend(self._complete_columns_from_from_clause(sql, metadata, prefix))

        # table. 뒤가 아닌 경우에만 키워드/함수 추가
        # (table. 뒤에서는 해당 테이블 컬럼만 제안)
        if not context.get('table'):
            completions.extend(self._complete_keywords_and_functions(context, prefix))

        return completions

    def _complete_tables(self, metadata: SchemaMetadata, prefix: str) -> List[Dict]:
        """FROM/JOIN 뒤 → 테이블 목록"""
        completions = []
        for table in sorted(metadata.tables):
            if self._matches_prefix(table, prefix):
                completions.append({
                    'label': table,
                    'type': 'table',
                    'detail': '테이블'
                })
        return completions

    def _complete_columns_for_table(self, sql: str, metadata: SchemaMetadata,
                                     target_table: str, prefix: str) -> List[Dict]:
        """table. 또는 alias. 뒤 → 해당 테이블의 컬럼 목록"""
        completions = []
        # 별칭 → 실제 테이블명 변환은 조회 전에 수행
        aliases = extract_table_aliases(sql, metadata)
        resolved_table = aliases.get(target_table.lower(), target_table)
        real_table = metadata.get_table_name(resolved_table)
        if real_table and real_table in metadata.columns:
            for col in sorted(metadata.columns[real_table]):
                if self._matches_prefix(col, prefix):
                    completions.append({
                        'label': col,
                        'type': 'column',
                        'detail': f'{real_table} 컬럼'
                    })
        return completions

    def _complete_columns_from_from_clause(self, sql: str, metadata: SchemaMetadata,
                                            prefix: str) -> List[Dict]:
        """SELECT/WHERE 등 뒤 (테이블 미지정) → FROM 절의 모든 테이블 컬럼"""
        completions = []
        from_tables = self._extract_from_tables(sql, metadata)
        for table in from_tables:
            if table in metadata.columns:
                for col in sorted(metadata.columns[table]):
                    if self._matches_prefix(col, prefix):
                        completions.append({
                            'label': col,
                            'type': 'column',
                            'detail': f'{table}'
                        })
        return completions

    def _complete_keywords_and_functions(self, context: Dict, prefix: str) -> List[Dict]:
        """키워드/함수 완성 (keyword 또는 column 컨텍스트에서만)"""
        completions = []

        if context['type'] in ('keyword', 'column'):
            for kw in self.SQL_KEYWORDS:
                if self._matches_prefix(kw, prefix):
                    completions.append({
                        'label': kw,
                        'type': 'keyword',
                        'detail': 'SQL 키워드'
                    })

        if context['type'] in ('keyword', 'column'):
            for func in self.SQL_FUNCTIONS:
                if self._matches_prefix(func, prefix):
                    completions.append({
                        'label': f'{func}()',
                        'type': 'function',
                        'detail': 'SQL 함수'
                    })

        return completions

    def _analyze_context(self, text_before: str) -> Dict:
        """커서 앞 컨텍스트 분석"""
        text_upper = text_before.upper()

        # FROM schema. / JOIN schema. 뒤에서는 schema-qualified table 이름을 제안
        if re.search(r'\b(FROM|JOIN)\s+`?\w+`?\.\w*$', text_upper):
            return {'type': 'table'}

        if re.search(r'\b(LEFT|RIGHT|INNER|OUTER|CROSS)\s+JOIN\s+`?\w+`?\.\w*$', text_upper):
            return {'type': 'table'}

        # table. 뒤인지 확인 (table.col 입력 중)
        dot_match = re.search(r'`?(\w+)`?\.\w*$', text_before)
        if dot_match:
            return {'type': 'column', 'table': dot_match.group(1)}

        # FROM/JOIN 뒤인지 확인 (FROM table 또는 FROM 직후)
        # \w*$로 현재 입력 중인 단어까지 포함
        if re.search(r'\b(FROM|JOIN)\s+\w*$', text_upper):
            return {'type': 'table'}

        # LEFT/RIGHT/INNER/OUTER/CROSS JOIN 뒤인지 확인
        if re.search(r'\b(LEFT|RIGHT|INNER|OUTER|CROSS)\s+JOIN\s+\w*$', text_upper):
            return {'type': 'table'}

        # SELECT/WHERE/ORDER BY 등 뒤인지 확인
        if re.search(r'\b(SELECT|WHERE|AND|OR|ORDER\s+BY|GROUP\s+BY|HAVING|SET)\s+\w*$', text_upper):
            return {'type': 'column'}

        # 기본값: 키워드
        return {'type': 'keyword'}

    def _get_current_word(self, text: str) -> str:
        """현재 입력 중인 단어 추출"""
        match = re.search(r'(\w*)$', text)
        return match.group(1) if match else ''

    def _matches_prefix(self, item: str, prefix: str) -> bool:
        """접두사 매칭 (대소문자 무시)"""
        if not prefix:
            return True
        return item.lower().startswith(prefix.lower())

    def _extract_from_tables(self, sql: str, metadata: SchemaMetadata) -> List[str]:
        """FROM 절에서 테이블 추출 (CTE 이름 / 파생 테이블 별칭은 제외)"""
        tables = []
        virtual_tables = extract_cte_names(sql) | extract_derived_table_aliases(sql)
        pattern = r'\b(?:FROM|JOIN)\s+(?:`?(\w+)`?\.)?`?(\w+)`?'

        for match in re.finditer(pattern, sql, re.IGNORECASE):
            table = match.group(2)
            if table.lower() in virtual_tables:
                continue
            real_table = metadata.get_table_name(table)
            if real_table and real_table not in tables:
                tables.append(real_table)

        return tables
