"""SQL safety warning helpers for user-authored scheduled queries."""
import re

from src.core.sql_statement_parser import parse_sql_statements


DANGER_PATTERNS = [
    (r'\bDROP\s+(TABLE|DATABASE|INDEX)\b', "DROP 문은 데이터를 완전히 삭제합니다!"),
    (r'\bTRUNCATE\s+TABLE\b', "TRUNCATE는 테이블의 모든 데이터를 삭제합니다!"),
    (r'\bDELETE\s+FROM\s+\w+\s*(?:;|$)', "DELETE에 WHERE 절이 없어 전체 데이터가 삭제됩니다!"),
    (r'\bUPDATE\s+\w+\s+SET\s+(?:(?!\bWHERE\b|;).)*(?:;|$)', "UPDATE에 WHERE 절이 없어 전체 데이터가 수정됩니다!"),
]


def find_dangerous_sql_warnings(sql_text: str) -> list[str]:
    """Return warning messages for dangerous SQL patterns in first-seen order."""
    if not sql_text.strip():
        return []

    statements = parse_sql_statements(sql_text) or [sql_text]
    messages: list[str] = []
    for statement in statements:
        for pattern, message in DANGER_PATTERNS:
            if re.search(pattern, statement, re.IGNORECASE | re.DOTALL) and message not in messages:
                messages.append(message)
    return messages
