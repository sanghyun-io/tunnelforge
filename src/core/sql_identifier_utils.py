"""
SQL 식별자 파싱 유틸리티
- 별칭/CTE/파생 테이블 이름 추출
- 정규식 기반 파싱 (의존성 없음)
"""
import re
from typing import Dict, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.sql_metadata import SchemaMetadata


# 별칭/CTE 파싱에서 공통으로 사용하는 예약어 스킵 목록
ALIAS_STOP_WORDS = {
    'WHERE', 'ON', 'AND', 'OR', 'SET', 'VALUES',
    'LEFT', 'RIGHT', 'INNER', 'OUTER', 'CROSS', 'FULL',
    'JOIN', 'ORDER', 'GROUP', 'HAVING', 'LIMIT', 'OFFSET',
    'UNION', 'ALL', 'SELECT', 'FROM', 'BY', 'AS',
}


def _normalize_identifier(identifier: str) -> str:
    """식별자를 감싸는 백틱 한 겹 제거 (없으면 그대로 반환)"""
    if len(identifier) >= 2 and identifier[0] == '`' and identifier[-1] == '`':
        return identifier[1:-1]
    return identifier


def _read_identifier(sql: str, pos: int) -> Tuple[Optional[str], int]:
    """pos 위치(앞쪽 공백 허용)부터 식별자 하나를 읽는다.

    백틱 식별자(`name`)와 일반 \\w+ 식별자를 모두 지원한다.

    Returns:
        (식별자, 다음 위치) 또는 식별자를 읽지 못하면 (None, pos)
    """
    length = len(sql)
    i = pos
    while i < length and sql[i].isspace():
        i += 1

    if i >= length:
        return None, pos

    if sql[i] == '`':
        end = sql.find('`', i + 1)
        if end == -1:
            return None, pos
        return sql[i + 1:end], end + 1

    match = re.match(r'\w+', sql[i:])
    if not match:
        return None, pos
    return match.group(0), i + match.end()


def _skip_balanced_parentheses(sql: str, open_pos: int) -> int:
    """open_pos가 가리키는 '('과 짝이 맞는 ')' 바로 다음 위치를 반환한다.

    문자열 리터럴(작은따옴표/큰따옴표) 내부의 괄호는 무시하며,
    이스케이프 처리는 `_find_string_regions`와 동일하게 따옴표 2개 연속을 이스케이프로 본다.
    짝이 맞지 않으면 len(sql)을 반환한다.
    """
    length = len(sql)
    if open_pos >= length or sql[open_pos] != '(':
        return open_pos

    depth = 0
    in_string = False
    string_char = None
    i = open_pos

    while i < length:
        char = sql[i]

        if in_string:
            if char == string_char:
                if i + 1 < length and sql[i + 1] == string_char:
                    i += 1  # 이스케이프된 따옴표 스킵
                else:
                    in_string = False
                    string_char = None
        else:
            if char in ("'", '"'):
                in_string = True
                string_char = char
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    return i + 1

        i += 1

    return length


def extract_cte_names(sql: str) -> Set[str]:
    """WITH 절에 정의된 CTE 이름 목록 추출 (테이블 존재 검증 제외용)

    지원 형태:
        WITH cte AS (...) SELECT * FROM cte
        WITH a AS (...), b AS (...) SELECT * FROM b
        WITH RECURSIVE cte AS (...) SELECT * FROM cte
        WITH `cte` AS (...) SELECT * FROM `cte`
    """
    names: Set[str] = set()

    with_match = re.search(r'\bWITH\b', sql, re.IGNORECASE)
    if not with_match:
        return names

    length = len(sql)
    pos = with_match.end()

    recursive_match = re.match(r'\s+RECURSIVE\b', sql[pos:], re.IGNORECASE)
    if recursive_match:
        pos += recursive_match.end()

    while True:
        name, pos = _read_identifier(sql, pos)
        if not name:
            break
        names.add(_normalize_identifier(name).lower())

        while pos < length and sql[pos].isspace():
            pos += 1

        # 선택적 컬럼 목록: cte(col1, col2)
        if pos < length and sql[pos] == '(':
            pos = _skip_balanced_parentheses(sql, pos)
            while pos < length and sql[pos].isspace():
                pos += 1

        as_match = re.match(r'AS\b', sql[pos:], re.IGNORECASE)
        if not as_match:
            break
        pos += as_match.end()

        while pos < length and sql[pos].isspace():
            pos += 1

        if pos >= length or sql[pos] != '(':
            break
        pos = _skip_balanced_parentheses(sql, pos)

        while pos < length and sql[pos].isspace():
            pos += 1

        if pos < length and sql[pos] == ',':
            pos += 1
            continue
        break

    return names


def extract_derived_table_aliases(sql: str) -> Set[str]:
    """FROM (...) AS alias / JOIN (...) AS alias 형태의 파생 테이블 별칭 추출

    파생 테이블 별칭은 실제 테이블이 아니므로, 테이블 존재 검증에서 제외하기 위한
    스킵 목록으로만 사용한다 (메타데이터 조회용이 아님).
    """
    aliases: Set[str] = set()
    length = len(sql)

    for match in re.finditer(r'\b(?:FROM|JOIN)\s*\(', sql, re.IGNORECASE):
        open_pos = match.end() - 1
        pos = _skip_balanced_parentheses(sql, open_pos)

        while pos < length and sql[pos].isspace():
            pos += 1

        as_match = re.match(r'AS\b', sql[pos:], re.IGNORECASE)
        if as_match:
            pos += as_match.end()

        alias, _ = _read_identifier(sql, pos)
        if not alias:
            continue

        normalized = _normalize_identifier(alias)
        if normalized.upper() not in ALIAS_STOP_WORDS:
            aliases.add(normalized.lower())

    return aliases


def extract_table_aliases(sql: str, metadata: 'SchemaMetadata') -> Dict[str, str]:
    """FROM/JOIN 절의 실제 테이블 참조에서 별칭(별칭 → 테이블명) 추출

    CTE 이름과 파생 테이블(서브쿼리)은 실제 테이블이 아니므로 별칭 매핑에서 제외한다.
    Validator와 AutoCompleter가 공용으로 사용하는 단일 파서다.
    """
    aliases: Dict[str, str] = {}
    cte_names = extract_cte_names(sql)

    # FROM/JOIN 뒤가 '('인 경우(파생 테이블)는 이 패턴에서 제외
    pattern = r'\b(?:FROM|JOIN)\s+(?!\()(?:`?(\w+)`?\.)?`?(\w+)`?(?:\s+(?:AS\s+)?`?(\w+)`?)?'

    for match in re.finditer(pattern, sql, re.IGNORECASE):
        table = match.group(2)
        alias = match.group(3)

        # CTE 이름이면서 실제 메타데이터 테이블이 아니면 별칭 소스로 사용하지 않음
        if table.lower() in cte_names and not metadata.has_table(table):
            continue

        if alias and alias.upper() in ALIAS_STOP_WORDS:
            alias = None

        if alias:
            aliases[alias.lower()] = table

    # 테이블 자체도 추가 (self-reference)
    for real_table in metadata.tables:
        aliases[real_table.lower()] = real_table

    return aliases
