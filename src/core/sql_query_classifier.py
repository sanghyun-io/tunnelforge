"""Unified SQL statement classifier shared by the DB core JSONL client.

Recognizes the leading keyword of a SQL statement after skipping comments
and leading parentheses, so callers can decide whether a statement returns
rows or triggers a MySQL implicit commit without re-implementing ad-hoc
`sql.lower().startswith(...)` checks.
"""
from dataclasses import dataclass
from typing import List

_ROW_RETURNING_KEYWORDS = frozenset({
    "select", "with", "show", "desc", "describe", "explain", "call", "values", "table",
})

_IMPLICIT_COMMIT_SINGLE_KEYWORDS = frozenset({
    "create", "alter", "drop", "truncate", "rename", "grant", "revoke",
})

_IMPLICIT_COMMIT_KEYWORD_PAIRS = frozenset({
    ("create", "index"),
    ("drop", "index"),
    ("lock", "tables"),
    ("unlock", "tables"),
    ("analyze", "table"),
    ("optimize", "table"),
    ("repair", "table"),
})


@dataclass(frozen=True)
class SQLQueryClassification:
    leading_keyword: str
    returns_rows: bool
    mysql_implicit_commit_ddl: bool


def _is_keyword_char(char: str) -> bool:
    return char == "_" or ("a" <= char <= "z") or ("A" <= char <= "Z")


def _strip_leading_comments_and_parens(sql: str) -> str:
    text = sql.lstrip()
    while True:
        if text.startswith("--") or text.startswith("#"):
            newline = text.find("\n")
            text = text[newline + 1:] if newline != -1 else ""
            text = text.lstrip()
            continue
        if text.startswith("/*"):
            end = text.find("*/")
            text = text[end + 2:] if end != -1 else ""
            text = text.lstrip()
            continue
        if text.startswith("("):
            text = text[1:].lstrip()
            continue
        return text


def _leading_tokens(sql: str, max_tokens: int = 3) -> List[str]:
    text = _strip_leading_comments_and_parens(sql)
    tokens: List[str] = []
    index = 0
    length = len(text)
    while len(tokens) < max_tokens:
        while index < length and text[index].isspace():
            index += 1
        start = index
        while index < length and _is_keyword_char(text[index]):
            index += 1
        if index == start:
            break
        tokens.append(text[start:index].lower())
    return tokens


def statement_returns_rows(sql: str) -> bool:
    """Return True when a SQL statement is expected to return a result set."""
    tokens = _leading_tokens(sql, max_tokens=1)
    return bool(tokens) and tokens[0] in _ROW_RETURNING_KEYWORDS


def is_mysql_implicit_commit_ddl(sql: str) -> bool:
    """Return True for statements that trigger a MySQL implicit commit."""
    tokens = _leading_tokens(sql, max_tokens=2)
    if not tokens:
        return False
    if tokens[0] in _IMPLICIT_COMMIT_SINGLE_KEYWORDS:
        return True
    if len(tokens) >= 2 and (tokens[0], tokens[1]) in _IMPLICIT_COMMIT_KEYWORD_PAIRS:
        return True
    return False


def classify_sql_statement(sql: str) -> SQLQueryClassification:
    """Classify a SQL statement's leading keyword in a single pass."""
    tokens = _leading_tokens(sql, max_tokens=1)
    leading_keyword = tokens[0] if tokens else ""
    return SQLQueryClassification(
        leading_keyword=leading_keyword,
        returns_rows=statement_returns_rows(sql),
        mysql_implicit_commit_ddl=is_mysql_implicit_commit_ddl(sql),
    )
