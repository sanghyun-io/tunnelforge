"""SQL statement splitting helpers shared by execution entry points."""
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


@dataclass
class SqlStatement:
    text: str
    start: int
    end: int
    boundary_end: int


def parse_sql_statements(sql_text: str) -> list[str]:
    """Split SQL text into statements while preserving SQL-internal semicolons."""
    return [statement.text for statement in parse_sql_statement_ranges(sql_text)]


def find_sql_statement_at_position(sql_text: str, cursor_pos: int) -> str:
    """Return the parsed SQL statement containing or nearest to cursor_pos."""
    statements = parse_sql_statement_ranges(sql_text)
    if not statements:
        return (sql_text or "").strip()

    cursor_pos = max(0, min(cursor_pos, len(sql_text or "")))
    for statement in statements:
        if statement.start <= cursor_pos <= statement.boundary_end:
            return statement.text
        if cursor_pos < statement.start:
            return statement.text

    return statements[-1].text


def parse_sql_statement_ranges(sql_text: str) -> list[SqlStatement]:
    """Split SQL text and keep source ranges for cursor-based statement lookup."""
    if not sql_text or not sql_text.strip():
        return []

    statements: list[SqlStatement] = []
    current: list[str] = []
    current_start: Optional[int] = None
    delimiter = ";"
    quote = None
    dollar_quote = None
    line_comment = False
    block_comment = False
    escape_next = False
    i = 0

    def append_text(text: str, source_pos: int) -> None:
        nonlocal current_start
        if current_start is None:
            current_start = source_pos
        current.append(text)

    def flush(source_end: int, boundary_end: int) -> None:
        nonlocal current_start, current
        if current_start is None:
            current = []
            return

        statement = _trim_statement("".join(current), current_start, source_end, boundary_end)
        if statement:
            statements.append(statement)
        current = []
        current_start = None

    while i < len(sql_text):
        char = sql_text[i]
        next_char = sql_text[i + 1] if i + 1 < len(sql_text) else ""

        if not any([quote, dollar_quote, line_comment, block_comment]):
            line_start = i == 0 or sql_text[i - 1] == "\n"
            if line_start:
                line_end = sql_text.find("\n", i)
                if line_end == -1:
                    line_end = len(sql_text)
                line = sql_text[i:line_end]
                stripped = line.strip()
                if stripped.upper().startswith("DELIMITER "):
                    delimiter = stripped.split(None, 1)[1].strip() or ";"
                    i = line_end + (1 if line_end < len(sql_text) else 0)
                    continue

        if line_comment:
            i, line_comment = _consume_line_comment(sql_text, i, append_text)
            continue

        if block_comment:
            i, block_comment = _consume_block_comment(sql_text, i, append_text)
            continue

        if dollar_quote:
            i, dollar_quote = _consume_dollar_quote(sql_text, i, dollar_quote, append_text)
            continue

        if quote:
            i, quote, escape_next = _consume_quoted_string(sql_text, i, quote, escape_next, append_text)
            continue

        if delimiter != ";" and delimiter and sql_text.startswith(delimiter, i):
            flush(i, i + len(delimiter))
            i += len(delimiter)
            continue

        if delimiter == ";":
            marker = read_dollar_quote(sql_text, i)
            if marker:
                dollar_quote = marker
                append_text(marker, i)
                i += len(marker)
                continue

        if char in ("'", '"', "`"):
            quote = char
            append_text(char, i)
            i += 1
            continue

        if char == "-" and next_char == "-":
            line_comment = True
            append_text(char + next_char, i)
            i += 2
            continue

        if char == "#":
            line_comment = True
            append_text(char, i)
            i += 1
            continue

        if char == "/" and next_char == "*":
            block_comment = True
            append_text(char + next_char, i)
            i += 2
            continue

        if delimiter and sql_text.startswith(delimiter, i):
            flush(i, i + len(delimiter))
            i += len(delimiter)
            continue

        append_text(char, i)
        i += 1

    flush(len(sql_text), len(sql_text))
    return statements


AppendText = Callable[[str, int], None]


def _consume_line_comment(sql_text: str, i: int, append_text: AppendText) -> Tuple[int, bool]:
    """줄 주석(`--`/`#`) 내부 문자 1개를 소비. Returns (다음 커서 위치, 주석 지속 여부)."""
    char = sql_text[i]
    append_text(char, i)
    if char == "\n":
        return i + 1, False
    return i + 1, True


def _consume_block_comment(sql_text: str, i: int, append_text: AppendText) -> Tuple[int, bool]:
    """블록 주석(`/* ... */`) 내부 문자를 소비. Returns (다음 커서 위치, 주석 지속 여부)."""
    char = sql_text[i]
    next_char = sql_text[i + 1] if i + 1 < len(sql_text) else ""
    append_text(char, i)
    if char == "*" and next_char == "/":
        append_text(next_char, i + 1)
        return i + 2, False
    return i + 1, True


def _consume_dollar_quote(
    sql_text: str, i: int, dollar_quote: str, append_text: AppendText
) -> Tuple[int, Optional[str]]:
    """PostgreSQL `$tag$ ... $tag$` 내부 문자를 소비. Returns (다음 커서 위치, 갱신된 dollar_quote 상태(None이면 종료))."""
    if sql_text.startswith(dollar_quote, i):
        append_text(dollar_quote, i)
        return i + len(dollar_quote), None
    char = sql_text[i]
    append_text(char, i)
    return i + 1, dollar_quote


def _consume_quoted_string(
    sql_text: str, i: int, quote: str, escape_next: bool, append_text: AppendText
) -> Tuple[int, Optional[str], bool]:
    """따옴표(`'`/`"`/backtick) 문자열 내부 문자를 소비.

    Returns (다음 커서 위치, 갱신된 quote 상태(None이면 종료), 갱신된 escape_next).
    """
    char = sql_text[i]
    append_text(char, i)
    if escape_next:
        return i + 1, quote, False
    if char == "\\":
        return i + 1, quote, True
    if char == quote:
        return i + 1, None, False
    return i + 1, quote, False


def read_dollar_quote(sql_text: str, start: int) -> str:
    sql_text = sql_text or ""
    if start < 0 or start >= len(sql_text):
        return ""
    if sql_text[start] != "$":
        return ""
    end = sql_text.find("$", start + 1)
    if end == -1:
        return ""
    tag = sql_text[start + 1:end]
    if tag:
        if not (tag[0].isalpha() or tag[0] == "_"):
            return ""
        if not all(char.isalnum() or char == "_" for char in tag[1:]):
            return ""
    return sql_text[start:end + 1]


def _trim_statement(
    raw_text: str,
    source_start: int,
    source_end: int,
    boundary_end: int,
) -> Optional[SqlStatement]:
    leading = len(raw_text) - len(raw_text.lstrip())
    trailing = len(raw_text) - len(raw_text.rstrip())
    text = raw_text.strip()
    if not text:
        return None

    start = source_start + leading
    end = source_end - trailing
    return SqlStatement(text=text, start=start, end=end, boundary_end=boundary_end)
