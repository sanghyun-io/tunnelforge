"""Pure SQL editability helpers for the SQL editor dialog."""
import re


POSTGRES_PRIMARY_KEY_QUERY = (
    "SELECT kcu.column_name "
    "FROM information_schema.table_constraints tc "
    "JOIN information_schema.key_column_usage kcu "
    "  ON tc.constraint_name = kcu.constraint_name "
    " AND tc.table_schema = kcu.table_schema "
    " AND tc.table_name = kcu.table_name "
    "WHERE tc.constraint_type = 'PRIMARY KEY' "
    "  AND tc.table_schema = %s "
    "  AND tc.table_name = %s "
    "ORDER BY kcu.ordinal_position"
)

MYSQL_PRIMARY_KEY_QUERY_WITH_SCHEMA = (
    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_KEY='PRI' "
    "ORDER BY ORDINAL_POSITION"
)

MYSQL_PRIMARY_KEY_QUERY_CURRENT_DATABASE = (
    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_KEY='PRI' "
    "ORDER BY ORDINAL_POSITION"
)


def analyze_query_editability(query):
    """Extract editable single-table SELECT information.

    Returns ``{"schema": str | None, "table": str}`` or ``None``.
    """
    if not query:
        return None

    q = re.sub(r'/\*.*?\*/', ' ', query, flags=re.DOTALL)
    q = re.sub(r'--[^\n]*', ' ', q)
    q_norm = q.strip().rstrip(';').strip()
    if not q_norm:
        return None

    q_upper = q_norm.upper()
    if not q_upper.startswith('SELECT'):
        return None

    forbidden_patterns = [
        r'\bJOIN\b', r'\bUNION\b', r'\bGROUP\s+BY\b',
        r'\bHAVING\b', r'\bDISTINCT\b',
    ]
    for pat in forbidden_patterns:
        if re.search(pat, q_upper):
            return None
    if re.search(r'\b(COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT)\s*\(', q_upper):
        return None

    match = re.search(
        r'\bFROM\s+(`[^`]+`|"[^"]+"|[\w$]+)(\s*\.\s*(`[^`]+`|"[^"]+"|[\w$]+))?',
        q_norm,
        re.IGNORECASE,
    )
    if not match:
        return None

    after_from = q_norm[match.start():]
    from_kw_end = re.search(r'\bFROM\s+', after_from, re.IGNORECASE).end()
    if after_from[from_kw_end:].lstrip().startswith('('):
        return None

    part1 = match.group(1).strip().strip('`"')
    part2 = match.group(3).strip().strip('`"') if match.group(3) else None
    schema, table = (part1, part2) if part2 else (None, part1)

    rest = q_norm[match.end():]
    stop = re.search(r'\b(WHERE|ORDER|LIMIT|GROUP|HAVING|FOR)\b', rest, re.IGNORECASE)
    rest_check = rest[:stop.start()] if stop else rest
    if ',' in rest_check:
        return None

    return {'schema': schema, 'table': table}


def quote_editor_identifier(engine, name: str) -> str:
    """Quote an editor identifier for the selected SQL engine."""
    if str(engine).lower() == "postgresql":
        return f'"{name.replace(chr(34), chr(34) + chr(34))}"'
    return f"`{name.replace('`', '``')}`"


def build_primary_key_query(engine, has_schema):
    """Return the existing information_schema PK lookup SQL for an engine."""
    if str(engine).lower() == "postgresql":
        return POSTGRES_PRIMARY_KEY_QUERY
    if has_schema:
        return MYSQL_PRIMARY_KEY_QUERY_WITH_SCHEMA
    return MYSQL_PRIMARY_KEY_QUERY_CURRENT_DATABASE
