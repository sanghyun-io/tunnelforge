"""
SQL 파서 모듈

CREATE TABLE, CREATE USER, GRANT 문을 파싱하여 구조화된 정보를 제공합니다.
mysql-upgrade-checker의 파서 로직을 Python으로 포팅.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union


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
        """주어진 컬럼들이 이 인덱스로 커버되는지 확인"""
        cols_lower = [c.lower() for c in cols]
        idx_cols_lower = [c.lower() for c in self.columns[:len(cols)]]
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

    INDEX_PATTERN = re.compile(
        r'(?:(UNIQUE|FULLTEXT|SPATIAL)\s+)?'
        r'(?:KEY|INDEX)\s+'
        r'(?:`?(\w+)`?\s*)?'  # 인덱스명
        r'\(\s*([^)]+)\s*\)',
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

        depth = 0
        end = start
        for i in range(start, len(sql)):
            if sql[i] == '(':
                depth += 1
            elif sql[i] == ')':
                depth -= 1
                if depth == 0:
                    end = i
                    break

        return sql[start + 1:end]

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
        """인덱스 정의 파싱"""
        indexes = []

        for match in self.INDEX_PATTERN.finditer(body):
            index_type = match.group(1) or ""
            index_name = match.group(2) or f"idx_{len(indexes)}"
            columns_str = match.group(3)

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
        """콤마로 정의 분리 (괄호 안 콤마 제외)"""
        definitions = []
        current = ""
        depth = 0

        for char in body:
            if char == '(':
                depth += 1
                current += char
            elif char == ')':
                depth -= 1
                current += char
            elif char == ',' and depth == 0:
                definitions.append(current.strip())
                current = ""
            else:
                current += char

        if current.strip():
            definitions.append(current.strip())

        return definitions


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

    def extract_create_table_statements(self, content: str) -> List[str]:
        """SQL 파일에서 CREATE TABLE 문 추출"""
        statements = []

        # CREATE TABLE ... ; 패턴 (세미콜론까지)
        pattern = re.compile(
            r'CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
            r'[^;]+?'  # 테이블 정의
            r';',
            re.IGNORECASE | re.DOTALL
        )

        for match in pattern.finditer(content):
            statements.append(match.group(0))

        return statements

    def extract_create_user_statements(self, content: str) -> List[str]:
        """SQL 파일에서 CREATE USER 문 추출"""
        statements = []

        pattern = re.compile(
            r'CREATE\s+USER\s+[^;]+;',
            re.IGNORECASE | re.DOTALL
        )

        for match in pattern.finditer(content):
            statements.append(match.group(0))

        return statements

    def extract_grant_statements(self, content: str) -> List[str]:
        """SQL 파일에서 GRANT 문 추출"""
        statements = []

        pattern = re.compile(
            r'GRANT\s+[^;]+;',
            re.IGNORECASE | re.DOTALL
        )

        for match in pattern.finditer(content):
            statements.append(match.group(0))

        return statements
