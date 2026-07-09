"""
SQL 파서 모듈

CREATE TABLE, CREATE USER, GRANT 문을 파싱하여 구조화된 정보를 제공합니다.
mysql-upgrade-checker의 파서 로직을 Python으로 포팅.

구현된 파서:
- CreateTableParser: CREATE TABLE 문 파서
- CreateUserParser: CREATE USER 문 파서
- GrantParser: GRANT 문 파서
- SQLParser: 위 파서들을 묶은 통합 SQL 파서
"""

import configparser
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass
class ParsedColumn:
    """파싱된 컬럼 정보"""
    name: str
    data_type: str
    type_params: Optional[str] = None  # (10), (10,2), etc.
    nullable: bool = True
    default: Optional[str] = None
    extra: Optional[str] = None  # AUTO_INCREMENT, etc.
    charset: Optional[str] = None
    collation: Optional[str] = None
    comment: Optional[str] = None
    generated: Optional[Tuple[str, bool]] = None  # (expression, is_stored)
    unsigned: bool = False
    zerofill: bool = False

    @property
    def full_type(self) -> str:
        """전체 타입 문자열"""
        result = self.data_type
        if self.type_params:
            result += f"({self.type_params})"
        if self.unsigned:
            result += " UNSIGNED"
        if self.zerofill:
            result += " ZEROFILL"
        return result


@dataclass
class ParsedIndex:
    """파싱된 인덱스 정보"""
    name: str
    columns: List[str]
    prefix_lengths: Optional[List[Optional[int]]] = None
    is_primary: bool = False
    is_unique: bool = False
    is_fulltext: bool = False
    is_spatial: bool = False
    index_type: Optional[str] = None  # BTREE, HASH

    def covers_columns(self, cols: List[str]) -> bool:
        """주어진 컬럼 목록과 이 인덱스의 컬럼이 완전히 일치하는지 확인

        prefix(왼쪽부터 일부)만 일치해도 True를 반환하던 과거 로직은
        UNIQUE(a,b) 인덱스가 (a) 하나만으로도 유니크함을 보장한다고
        잘못 판단하게 만든다. FK 참조 검증처럼 "이 컬럼 목록으로 유니크함이
        보장되는가"를 확인하는 용도이므로 길이와 순서를 모두 엄격히 비교한다.
        """
        if len(cols) != len(self.columns):
            return False
        cols_lower = [c.lower() for c in cols]
        idx_cols_lower = [c.lower() for c in self.columns]
        return cols_lower == idx_cols_lower


@dataclass
class ParsedForeignKey:
    """파싱된 FK 정보"""
    name: str
    columns: List[str]
    ref_table: str
    ref_schema: Optional[str] = None
    ref_columns: List[str] = field(default_factory=list)
    on_delete: str = "RESTRICT"
    on_update: str = "RESTRICT"


@dataclass
class ParsedPartition:
    """파싱된 파티션 정보"""
    name: str
    partition_type: str  # RANGE, LIST, HASH, KEY
    expression: Optional[str] = None
    values: Optional[str] = None
    tablespace: Optional[str] = None


@dataclass
class ParsedTable:
    """파싱된 테이블 정보"""
    name: str
    schema: Optional[str] = None
    engine: Optional[str] = None
    charset: Optional[str] = None
    collation: Optional[str] = None
    row_format: Optional[str] = None
    tablespace: Optional[str] = None
    comment: Optional[str] = None
    columns: List[ParsedColumn] = field(default_factory=list)
    indexes: List[ParsedIndex] = field(default_factory=list)
    foreign_keys: List[ParsedForeignKey] = field(default_factory=list)
    partitions: List[ParsedPartition] = field(default_factory=list)

    def get_column(self, name: str) -> Optional[ParsedColumn]:
        """컬럼명으로 컬럼 조회"""
        for col in self.columns:
            if col.name.lower() == name.lower():
                return col
        return None

    def get_primary_key(self) -> Optional[ParsedIndex]:
        """PRIMARY KEY 조회"""
        for idx in self.indexes:
            if idx.is_primary:
                return idx
        return None

    def get_unique_indexes(self) -> List[ParsedIndex]:
        """UNIQUE 인덱스 목록 조회"""
        return [idx for idx in self.indexes if idx.is_unique]


@dataclass
class ParsedUser:
    """파싱된 사용자 정보"""
    user: str
    host: str
    auth_plugin: Optional[str] = None
    password_hash: Optional[str] = None
    require_ssl: bool = False
    account_locked: bool = False
    password_expired: bool = False


@dataclass
class ParsedGrant:
    """파싱된 GRANT 정보"""
    privileges: List[str]
    object_type: str  # *.*, db.*, db.table
    database: Optional[str] = None
    table: Optional[str] = None
    grantee_user: str = ""
    grantee_host: str = ""
    with_grant_option: bool = False


class CreateTableParser:
    """CREATE TABLE 문 파서"""

    # 정규식 패턴들
    TABLE_NAME_PATTERN = re.compile(
        r'CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
        r'(?:`?(\w+)`?\.)?`?(\w+)`?\s*\(',
        re.IGNORECASE
    )

    COLUMN_PATTERN = re.compile(
        r'`(\w+)`\s+'  # 컬럼명
        r'(\w+)'       # 데이터타입
        r'(?:\s*\(([^)]+)\))?'  # 타입 파라미터
        r'([^,]*)',    # 나머지 옵션
        re.IGNORECASE
    )

    PRIMARY_KEY_PATTERN = re.compile(
        r'PRIMARY\s+KEY\s*'
        r'(?:`?(\w+)`?\s*)?'  # 인덱스명 (선택적)
        r'\(\s*([^)]+)\s*\)',
        re.IGNORECASE
    )

    # 정의(definition) 시작 부분에만 앵커링된 "헤더" 패턴.
    # 컬럼 목록은 여기서 캡처하지 않고 balanced-paren 스캔(_extract_parenthesized)으로
    # 별도 추출한다 - `[^)]+`로는 `name`(10)처럼 중첩된 괄호(prefix length)를
    # 다루지 못하고, PRIMARY KEY/FOREIGN KEY 절 내부의 "...KEY (...)" 부분에도
    # 오매칭되어 phantom index를 만들어내는 문제가 있었다.
    INDEX_PATTERN = re.compile(
        r'^(?:(UNIQUE|FULLTEXT|SPATIAL)\s+)?'
        r'(?:KEY|INDEX)\s+'
        r'(?:`?(\w+)`?\s*)?'  # 인덱스명
        r'\(',
        re.IGNORECASE
    )

    FK_PATTERN = re.compile(
        r'CONSTRAINT\s+`?(\w+)`?\s+'
        r'FOREIGN\s+KEY\s*\(\s*([^)]+)\s*\)\s+'
        r'REFERENCES\s+(?:`?(\w+)`?\.)?`?(\w+)`?\s*\(\s*([^)]+)\s*\)'
        r'(?:\s+ON\s+DELETE\s+(CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT|SET\s+DEFAULT))?'
        r'(?:\s+ON\s+UPDATE\s+(CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT|SET\s+DEFAULT))?',
        re.IGNORECASE
    )

    # 간단한 FK 패턴 (CONSTRAINT 없음)
    FK_SIMPLE_PATTERN = re.compile(
        r'FOREIGN\s+KEY\s*\(\s*([^)]+)\s*\)\s+'
        r'REFERENCES\s+(?:`?(\w+)`?\.)?`?(\w+)`?\s*\(\s*([^)]+)\s*\)'
        r'(?:\s+ON\s+DELETE\s+(CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT|SET\s+DEFAULT))?'
        r'(?:\s+ON\s+UPDATE\s+(CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT|SET\s+DEFAULT))?',
        re.IGNORECASE
    )

    def parse(self, sql: str) -> Optional[ParsedTable]:
        """CREATE TABLE 문 파싱"""
        # 1. 테이블명 추출
        table_match = self.TABLE_NAME_PATTERN.search(sql)
        if not table_match:
            return None

        schema = table_match.group(1)
        table_name = table_match.group(2)

        table = ParsedTable(name=table_name, schema=schema)

        # 2. 괄호 안 내용 추출
        body = self._extract_body(sql)
        if not body:
            return table

        # 3. 컬럼 파싱
        table.columns = self._parse_columns(body)

        # 4. PRIMARY KEY 파싱
        pk = self._parse_primary_key(body)
        if pk:
            table.indexes.append(pk)

        # 5. 인덱스 파싱
        table.indexes.extend(self._parse_indexes(body))

        # 6. FK 파싱
        table.foreign_keys = self._parse_foreign_keys(body)

        # 7. 테이블 옵션 파싱
        self._parse_table_options(sql, table)

        return table

    def _extract_body(self, sql: str) -> Optional[str]:
        """CREATE TABLE ( ... ) 내용 추출"""
        start = sql.find('(')
        if start == -1:
            return None

        end = self._find_matching_paren(sql, start)
        if end is None:
            # 닫는 괄호가 없는 불완전한 SQL - 여는 괄호 이후 전체를 body로 취급
            return sql[start + 1:]

        return sql[start + 1:end]

    def _find_matching_paren(self, text: str, open_idx: int) -> Optional[int]:
        """open_idx 위치의 '('에 대응하는 닫는 ')' 인덱스를 찾는다

        작은따옴표/큰따옴표/백틱으로 감싸인 문자열 리터럴 내부의 괄호는
        깊이 계산에서 제외한다 (예: COMMENT '50%) discount' 안의 ')'가
        괄호를 조기에 닫힌 것으로 오인되는 문제 방지).
        """
        depth = 0
        quote: Optional[str] = None
        i = open_idx
        n = len(text)

        while i < n:
            char = text[i]

            if quote:
                if char == '\\' and quote != '`' and i + 1 < n:
                    # 백슬래시 이스케이프: 다음 문자를 리터럴로 그대로 소비
                    i += 2
                    continue
                if char == quote:
                    if i + 1 < n and text[i + 1] == quote:
                        # 이스케이프된 따옴표 반복('' / `` / "") - 문자열 계속
                        i += 2
                        continue
                    quote = None
                i += 1
                continue

            if char in ("'", '"', '`'):
                quote = char
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    return i
            i += 1

        return None

    def _extract_parenthesized(self, text: str, open_idx: int) -> Optional[str]:
        """open_idx 위치의 '('부터 대응하는 ')'까지의 내부 텍스트를 반환"""
        if open_idx < 0 or open_idx >= len(text) or text[open_idx] != '(':
            return None

        close_idx = self._find_matching_paren(text, open_idx)
        if close_idx is None:
            return None

        return text[open_idx + 1:close_idx]

    def _parse_columns(self, body: str) -> List[ParsedColumn]:
        """컬럼 정의 파싱"""
        columns = []

        # 각 정의를 콤마로 분리 (단, 괄호 안 콤마 제외)
        definitions = self._split_definitions(body)

        for defn in definitions:
            defn = defn.strip()
            if not defn:
                continue

            # 키워드로 시작하는 줄은 컬럼이 아님
            defn_upper = defn.upper()
            if any(defn_upper.startswith(kw) for kw in [
                'PRIMARY', 'UNIQUE', 'INDEX', 'KEY', 'FULLTEXT',
                'SPATIAL', 'CONSTRAINT', 'FOREIGN', 'CHECK'
            ]):
                continue

            # 컬럼 파싱
            match = self.COLUMN_PATTERN.match(defn)
            if match:
                col = self._parse_column_definition(match)
                if col:
                    columns.append(col)

        return columns

    def _parse_column_definition(self, match: re.Match) -> Optional[ParsedColumn]:
        """컬럼 정의 상세 파싱"""
        name = match.group(1)
        data_type = match.group(2).upper()
        type_params = match.group(3)
        options = match.group(4) or ""
        options_upper = options.upper()

        col = ParsedColumn(
            name=name,
            data_type=data_type,
            type_params=type_params
        )

        # NOT NULL
        if 'NOT NULL' in options_upper:
            col.nullable = False
        elif 'NULL' in options_upper:
            col.nullable = True

        # UNSIGNED
        if 'UNSIGNED' in options_upper:
            col.unsigned = True

        # ZEROFILL
        if 'ZEROFILL' in options_upper:
            col.zerofill = True

        # DEFAULT
        default_match = re.search(r"DEFAULT\s+('(?:[^'\\]|\\.)*'|\S+)", options, re.IGNORECASE)
        if default_match:
            col.default = default_match.group(1)

        # AUTO_INCREMENT
        if 'AUTO_INCREMENT' in options_upper:
            col.extra = 'AUTO_INCREMENT'

        # CHARACTER SET
        charset_match = re.search(r'CHARACTER\s+SET\s+(\w+)', options, re.IGNORECASE)
        if charset_match:
            col.charset = charset_match.group(1)

        # COLLATE
        collate_match = re.search(r'COLLATE\s+(\w+)', options, re.IGNORECASE)
        if collate_match:
            col.collation = collate_match.group(1)

        # COMMENT
        comment_match = re.search(r"COMMENT\s+'([^']*)'", options, re.IGNORECASE)
        if comment_match:
            col.comment = comment_match.group(1)

        # GENERATED ALWAYS AS
        generated_match = re.search(
            r'GENERATED\s+ALWAYS\s+AS\s*\(([^)]+)\)(?:\s+(STORED|VIRTUAL))?',
            options, re.IGNORECASE
        )
        if generated_match:
            expr = generated_match.group(1)
            is_stored = generated_match.group(2) and generated_match.group(2).upper() == 'STORED'
            col.generated = (expr, is_stored)

        return col

    def _parse_primary_key(self, body: str) -> Optional[ParsedIndex]:
        """PRIMARY KEY 파싱"""
        match = self.PRIMARY_KEY_PATTERN.search(body)
        if not match:
            return None

        index_name = match.group(1) or 'PRIMARY'
        columns_str = match.group(2)
        columns, prefix_lengths = self._parse_index_columns(columns_str)

        return ParsedIndex(
            name=index_name,
            columns=columns,
            prefix_lengths=prefix_lengths,
            is_primary=True,
            is_unique=True
        )

    def _parse_indexes(self, body: str) -> List[ParsedIndex]:
        """인덱스 정의 파싱

        definition 단위(_split_definitions)로 순회하며 PRIMARY KEY/FOREIGN
        KEY/CONSTRAINT 절은 미리 건너뛴다. 예전에는 INDEX_PATTERN을 body
        전체에 finditer로 적용해서 "FOREIGN KEY (`col`)" 같은 절의 "KEY
        (`col`)" 부분에도 오매칭되어 존재하지 않는 phantom index를
        만들어냈다. 컬럼 목록도 정규식의 [^)]+ 대신 balanced-paren 스캔으로
        추출해 `name`(10) 같은 prefix length 표기를 올바르게 처리한다.
        """
        indexes = []

        for defn in self._split_definitions(body):
            defn_upper = defn.upper()
            if defn_upper.startswith('PRIMARY') or defn_upper.startswith('FOREIGN') or defn_upper.startswith('CONSTRAINT'):
                continue

            match = self.INDEX_PATTERN.match(defn)
            if not match:
                continue

            index_type = match.group(1) or ""
            index_name = match.group(2) or f"idx_{len(indexes)}"

            open_idx = match.end() - 1  # 패턴이 여는 '('까지 소비하므로 바로 그 위치
            columns_str = self._extract_parenthesized(defn, open_idx)
            if columns_str is None:
                continue

            columns, prefix_lengths = self._parse_index_columns(columns_str)

            idx = ParsedIndex(
                name=index_name,
                columns=columns,
                prefix_lengths=prefix_lengths,
                is_unique=index_type.upper() == 'UNIQUE',
                is_fulltext=index_type.upper() == 'FULLTEXT',
                is_spatial=index_type.upper() == 'SPATIAL'
            )
            indexes.append(idx)

        return indexes

    def _parse_index_columns(self, columns_str: str) -> Tuple[List[str], List[Optional[int]]]:
        """인덱스 컬럼 문자열 파싱"""
        columns = []
        prefix_lengths = []

        for part in columns_str.split(','):
            part = part.strip().strip('`')
            # prefix 길이 확인: column_name(10)
            prefix_match = re.match(r'`?(\w+)`?\s*\(\s*(\d+)\s*\)', part)
            if prefix_match:
                columns.append(prefix_match.group(1))
                prefix_lengths.append(int(prefix_match.group(2)))
            else:
                # ASC/DESC 제거
                col_name = re.sub(r'\s+(ASC|DESC)\s*$', '', part, flags=re.IGNORECASE)
                columns.append(col_name.strip().strip('`'))
                prefix_lengths.append(None)

        return columns, prefix_lengths

    def _parse_foreign_keys(self, body: str) -> List[ParsedForeignKey]:
        """FK 정의 파싱"""
        fks = []

        # CONSTRAINT ... FOREIGN KEY 패턴
        for match in self.FK_PATTERN.finditer(body):
            fk = ParsedForeignKey(
                name=match.group(1),
                columns=[c.strip().strip('`') for c in match.group(2).split(',')],
                ref_schema=match.group(3),
                ref_table=match.group(4),
                ref_columns=[c.strip().strip('`') for c in match.group(5).split(',')],
                on_delete=(match.group(6) or "RESTRICT").replace(' ', '_'),
                on_update=(match.group(7) or "RESTRICT").replace(' ', '_')
            )
            fks.append(fk)

        # 간단한 FOREIGN KEY 패턴 (CONSTRAINT 없음)
        fk_count = len(fks)
        for match in self.FK_SIMPLE_PATTERN.finditer(body):
            # 이미 파싱된 FK가 아닌지 확인 (위치 기반)
            is_duplicate = False
            for existing in fks:
                if existing.ref_table == match.group(3):
                    existing_cols = set(existing.columns)
                    new_cols = set(c.strip().strip('`') for c in match.group(1).split(','))
                    if existing_cols == new_cols:
                        is_duplicate = True
                        break

            if not is_duplicate:
                fk = ParsedForeignKey(
                    name=f"fk_{fk_count}",
                    columns=[c.strip().strip('`') for c in match.group(1).split(',')],
                    ref_schema=match.group(2),
                    ref_table=match.group(3),
                    ref_columns=[c.strip().strip('`') for c in match.group(4).split(',')],
                    on_delete=(match.group(5) or "RESTRICT").replace(' ', '_'),
                    on_update=(match.group(6) or "RESTRICT").replace(' ', '_')
                )
                fks.append(fk)
                fk_count += 1

        return fks

    def _parse_table_options(self, sql: str, table: ParsedTable):
        """테이블 옵션 파싱"""
        # 닫는 괄호 이후 내용에서 옵션 추출
        body_end = sql.rfind(')')
        if body_end == -1:
            return

        options_part = sql[body_end + 1:]

        # ENGINE
        engine_match = re.search(r'\bENGINE\s*=\s*(\w+)', options_part, re.IGNORECASE)
        if engine_match:
            table.engine = engine_match.group(1)

        # DEFAULT CHARSET / CHARSET
        charset_match = re.search(r'(?:DEFAULT\s+)?CHARSET\s*=\s*(\w+)', options_part, re.IGNORECASE)
        if charset_match:
            table.charset = charset_match.group(1)

        # CHARACTER SET
        if not table.charset:
            charset_match = re.search(r'CHARACTER\s+SET\s*=?\s*(\w+)', options_part, re.IGNORECASE)
            if charset_match:
                table.charset = charset_match.group(1)

        # COLLATE
        collate_match = re.search(r'COLLATE\s*=\s*(\w+)', options_part, re.IGNORECASE)
        if collate_match:
            table.collation = collate_match.group(1)

        # ROW_FORMAT
        row_format_match = re.search(r'ROW_FORMAT\s*=\s*(\w+)', options_part, re.IGNORECASE)
        if row_format_match:
            table.row_format = row_format_match.group(1)

        # TABLESPACE
        tablespace_match = re.search(r'TABLESPACE\s*=\s*`?(\w+)`?', options_part, re.IGNORECASE)
        if tablespace_match:
            table.tablespace = tablespace_match.group(1)

        # COMMENT
        comment_match = re.search(r"COMMENT\s*=\s*'([^']*)'", options_part, re.IGNORECASE)
        if comment_match:
            table.comment = comment_match.group(1)

    def _split_definitions(self, body: str) -> List[str]:
        """콤마로 정의 분리 (괄호 안 콤마 제외)

        문자열 리터럴(작은따옴표/큰따옴표/백틱) 내부의 콤마와 괄호는
        구분자로 보지 않는다. 예전 구현은 따옴표를 모르는 채로 depth만
        추적해서 `DEFAULT 'a,b'`나 `COMMENT '50%) discount'`처럼 리터럴
        안에 콤마/괄호가 들어간 정의를 잘못 쪼갰다.
        """
        definitions = []
        current: List[str] = []
        depth = 0
        quote: Optional[str] = None
        i = 0
        n = len(body)

        while i < n:
            char = body[i]

            if quote:
                current.append(char)
                if char == '\\' and quote != '`' and i + 1 < n:
                    # 백슬래시 이스케이프: 다음 문자를 리터럴로 그대로 소비
                    current.append(body[i + 1])
                    i += 2
                    continue
                if char == quote:
                    if i + 1 < n and body[i + 1] == quote:
                        # 이스케이프된 따옴표 반복('' / `` / "") - 문자열 계속
                        current.append(body[i + 1])
                        i += 2
                        continue
                    quote = None
                i += 1
                continue

            if char in ("'", '"', '`'):
                quote = char
                current.append(char)
            elif char == '(':
                depth += 1
                current.append(char)
            elif char == ')':
                depth -= 1
                current.append(char)
            elif char == ',' and depth == 0:
                definitions.append(''.join(current).strip())
                current = []
            else:
                current.append(char)
            i += 1

        if ''.join(current).strip():
            definitions.append(''.join(current).strip())

        return definitions


class SqlStatementScanner:
    """따옴표/괄호 상태를 추적하며 SQL 문을 토큰 경계로 스캔하는 헬퍼

    정규식 하나로는 다루기 어려운, 문자열 리터럴 안의 세미콜론/콤마/괄호를
    올바르게 무시한다. CREATE TABLE 문 경계 추출과 INSERT VALUES 행/값
    분해에 사용된다 (CreateTableParser의 괄호 균형 파싱과 짝을 이룬다).
    """

    def find_statement_end(self, content: str, start: int) -> int:
        """start 이후 문자열/식별자 밖의 첫 세미콜론 위치(문장 경계)를 찾는다

        작은따옴표, 큰따옴표, 백틱 안의 세미콜론은 무시하며, 백슬래시
        이스케이프와 이중 작은따옴표('')로 이스케이프된 따옴표도 처리한다.
        종료 세미콜론이 없으면 len(content)를 반환한다.
        """
        in_single = False
        in_double = False
        in_backtick = False
        i = start
        n = len(content)
        while i < n:
            ch = content[i]
            if in_single:
                if ch == '\\':
                    i += 2
                    continue
                if ch == "'":
                    if i + 1 < n and content[i + 1] == "'":
                        i += 2
                        continue
                    in_single = False
                i += 1
                continue
            if in_double:
                if ch == '\\':
                    i += 2
                    continue
                if ch == '"':
                    in_double = False
                i += 1
                continue
            if in_backtick:
                if ch == '`':
                    in_backtick = False
                i += 1
                continue
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == '`':
                in_backtick = True
            elif ch == ';':
                return i
            i += 1
        return n

    def iter_create_table_statements(self, content: str):
        """content에서 CREATE TABLE 문 전체 텍스트를 하나씩 생성한다

        CreateTableParser.TABLE_NAME_PATTERN으로 문장 시작 위치를 찾고
        find_statement_end로 종료 세미콜론까지 경계를 잡는다. 이렇게 얻은
        완전한 문장을 CreateTableParser.parse()에 넘기면, 파서의 괄호
        균형 기반 바디 추출(_extract_body)이 varchar(255) DEFAULT ... 같은
        중첩 괄호에서도 CREATE TABLE 바디를 올바르게 잘라낸다.
        """
        for table_match in CreateTableParser.TABLE_NAME_PATTERN.finditer(content):
            start = table_match.start()
            end = self.find_statement_end(content, start)
            yield content[start:end]

    def iter_values_rows(self, values_segment: str):
        """VALUES (...) 세그먼트에서 최상위 행 바디만 추출한다

        따옴표 상태와 괄호 중첩(깊이)을 함께 추적하여, 문자열 리터럴 안의
        괄호나 함수 호출의 중첩 괄호가 행 경계로 오인되지 않도록 한다.
        """
        depth = 0
        in_quote = False
        buf: list = []
        i = 0
        n = len(values_segment)
        while i < n:
            ch = values_segment[i]

            if in_quote:
                if ch == '\\' and i + 1 < n:
                    if depth >= 1:
                        buf.append(values_segment[i:i + 2])
                    i += 2
                    continue
                if ch == "'":
                    if i + 1 < n and values_segment[i + 1] == "'":
                        if depth >= 1:
                            buf.append("''")
                        i += 2
                        continue
                    in_quote = False
                if depth >= 1:
                    buf.append(ch)
                i += 1
                continue

            if ch == "'":
                in_quote = True
                if depth >= 1:
                    buf.append(ch)
                i += 1
                continue

            if ch == '(':
                if depth == 0:
                    buf = []
                else:
                    buf.append(ch)
                depth += 1
                i += 1
                continue

            if ch == ')':
                depth -= 1
                if depth == 0:
                    yield ''.join(buf)
                elif depth > 0:
                    buf.append(ch)
                else:
                    depth = 0
                i += 1
                continue

            if depth >= 1:
                buf.append(ch)
            i += 1

    def split_sql_values(self, row_body: str) -> List[str]:
        """행 바디를 최상위 콤마 기준으로 분리한다 (따옴표/괄호 안 콤마 제외)"""
        parts: List[str] = []
        current: list = []
        in_quote = False
        depth = 0
        i = 0
        n = len(row_body)
        while i < n:
            ch = row_body[i]
            if in_quote:
                current.append(ch)
                if ch == '\\' and i + 1 < n:
                    current.append(row_body[i + 1])
                    i += 2
                    continue
                if ch == "'":
                    if i + 1 < n and row_body[i + 1] == "'":
                        current.append("'")
                        i += 2
                        continue
                    in_quote = False
                i += 1
                continue
            if ch == "'":
                in_quote = True
                current.append(ch)
                i += 1
                continue
            if ch == '(':
                depth += 1
                current.append(ch)
                i += 1
                continue
            if ch == ')':
                depth = max(depth - 1, 0)
                current.append(ch)
                i += 1
                continue
            if ch == ',' and depth == 0:
                parts.append(''.join(current))
                current = []
                i += 1
                continue
            current.append(ch)
            i += 1
        parts.append(''.join(current))
        return parts


class CreateUserParser:
    """CREATE USER 문 파서"""

    USER_PATTERN = re.compile(
        r"CREATE\s+USER\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"['\"]?(\w+)['\"]?\s*@\s*['\"]?([^'\"]+)['\"]?"
        r"(?:\s+IDENTIFIED\s+(?:WITH\s+['\"]?(\w+)['\"]?)?"
        r"(?:\s+(?:BY|AS)\s+['\"]([^'\"]+)['\"])?)?",
        re.IGNORECASE
    )

    def parse(self, sql: str) -> Optional[ParsedUser]:
        """CREATE USER 문 파싱"""
        match = self.USER_PATTERN.search(sql)
        if not match:
            return None

        user = ParsedUser(
            user=match.group(1),
            host=match.group(2),
            auth_plugin=match.group(3),
            password_hash=match.group(4)
        )

        # 추가 옵션 확인
        sql_upper = sql.upper()
        if 'REQUIRE SSL' in sql_upper or 'REQUIRE X509' in sql_upper:
            user.require_ssl = True
        if 'ACCOUNT LOCK' in sql_upper:
            user.account_locked = True
        if 'PASSWORD EXPIRE' in sql_upper:
            user.password_expired = True

        return user


class GrantParser:
    """GRANT 문 파서"""

    GRANT_PATTERN = re.compile(
        r"GRANT\s+(.+?)\s+ON\s+"
        r"(?:`?(\w+|\*)`?\.)?`?(\w+|\*)`?\s+"
        r"TO\s+['\"]?(\w+)['\"]?\s*@\s*['\"]?([^'\"]+)['\"]?"
        r"(?:\s+WITH\s+GRANT\s+OPTION)?",
        re.IGNORECASE | re.DOTALL
    )

    def parse(self, sql: str) -> Optional[ParsedGrant]:
        """GRANT 문 파싱"""
        match = self.GRANT_PATTERN.search(sql)
        if not match:
            return None

        privileges_str = match.group(1)
        privileges = [p.strip() for p in privileges_str.split(',')]

        database = match.group(2) or '*'
        table = match.group(3) or '*'

        return ParsedGrant(
            privileges=privileges,
            object_type=f"{database}.{table}",
            database=database if database != '*' else None,
            table=table if table != '*' else None,
            grantee_user=match.group(4),
            grantee_host=match.group(5),
            with_grant_option='WITH GRANT OPTION' in sql.upper()
        )


class SQLParser:
    """SQL 파서 팩토리"""

    # 세미콜론까지의 문 추출용 패턴 (extract_* 공용)
    _CREATE_TABLE_STMT_PATTERN = re.compile(
        r'CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
        r'[^;]+?'  # 테이블 정의
        r';',
        re.IGNORECASE | re.DOTALL
    )
    _CREATE_USER_STMT_PATTERN = re.compile(
        r'CREATE\s+USER\s+[^;]+;',
        re.IGNORECASE | re.DOTALL
    )
    _GRANT_STMT_PATTERN = re.compile(
        r'GRANT\s+[^;]+;',
        re.IGNORECASE | re.DOTALL
    )

    def __init__(self):
        self.table_parser = CreateTableParser()
        self.user_parser = CreateUserParser()
        self.grant_parser = GrantParser()

    def parse_table(self, sql: str) -> Optional[ParsedTable]:
        return self.table_parser.parse(sql)

    def parse_user(self, sql: str) -> Optional[ParsedUser]:
        return self.user_parser.parse(sql)

    def parse_grant(self, sql: str) -> Optional[ParsedGrant]:
        return self.grant_parser.parse(sql)

    def detect_and_parse(self, sql: str) -> Optional[Union[ParsedTable, ParsedUser, ParsedGrant]]:
        """SQL 타입 감지 후 적절한 파서로 파싱"""
        sql_stripped = sql.strip()
        sql_upper = sql_stripped.upper()

        if sql_upper.startswith('CREATE TABLE'):
            return self.parse_table(sql_stripped)
        elif sql_upper.startswith('CREATE USER'):
            return self.parse_user(sql_stripped)
        elif sql_upper.startswith('GRANT'):
            return self.parse_grant(sql_stripped)

        return None

    @staticmethod
    def _extract_statements(pattern: 're.Pattern', content: str) -> List[str]:
        """미리 컴파일된 패턴으로 문 텍스트를 순서대로 추출한다"""
        return [match.group(0) for match in pattern.finditer(content)]

    def extract_create_table_statements(self, content: str) -> List[str]:
        """SQL 파일에서 CREATE TABLE 문 추출"""
        return self._extract_statements(self._CREATE_TABLE_STMT_PATTERN, content)

    def extract_create_user_statements(self, content: str) -> List[str]:
        """SQL 파일에서 CREATE USER 문 추출"""
        return self._extract_statements(self._CREATE_USER_STMT_PATTERN, content)

    def extract_grant_statements(self, content: str) -> List[str]:
        """SQL 파일에서 GRANT 문 추출"""
        return self._extract_statements(self._GRANT_STMT_PATTERN, content)
