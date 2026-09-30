"""Save an already fetched result (the rows on screen) as CSV or JSON Lines.

Same file format as the Rust core's streaming export (`query.execute` with `output`), so a
"displayed rows" file and a "full result" file are interchangeable:

- CSV follows RFC 4180 (CRLF, quoted fields). NULL is an empty unquoted field; the empty
  string is written as `""`, so the two stay distinguishable.
- Formula guard (default on): a cell starting with `=`, `+`, `-`, `@`, TAB or CR gets a leading
  `'` so spreadsheets do not evaluate it. Plain numbers such as `-5` are left alone.
- JSON Lines: one object per row, keys in column order, NULL as `null`.
"""
import json
import os
import re
from typing import Any, Dict, Iterable, List, Sequence

FORMAT_CSV = "csv"
FORMAT_JSONL = "jsonl"

_PLAIN_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_field(value: Any, formula_guard: bool = True) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    if formula_guard and text.startswith(_FORMULA_START) and not _PLAIN_NUMBER.match(text):
        text = "'" + text
    if text == "" or any(ch in text for ch in ',"\r\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def write_result_file(
    path: str,
    columns: Sequence[str],
    rows: Iterable[Any],
    file_format: str = FORMAT_CSV,
    bom: bool = False,
    formula_guard: bool = True,
) -> int:
    """Write rows (dicts keyed by column, or sequences in column order) atomically.

    Data goes to `<path>.partial` and replaces `<path>` only after a complete write.
    Returns the number of rows written.
    """
    if file_format not in (FORMAT_CSV, FORMAT_JSONL):
        raise ValueError(f"unsupported format: {file_format}")
    partial = path + ".partial"
    count = 0
    try:
        with open(partial, "w", encoding="utf-8", newline="") as handle:
            if file_format == FORMAT_CSV:
                if bom:
                    handle.write("﻿")
                handle.write(",".join(csv_field(name, formula_guard) for name in columns) + "\r\n")
            for row in rows:
                values: List[Any] = (
                    [row.get(name) for name in columns] if isinstance(row, dict) else list(row)
                )
                if file_format == FORMAT_CSV:
                    handle.write(",".join(csv_field(v, formula_guard) for v in values) + "\r\n")
                else:
                    record: Dict[str, Any] = dict(zip(columns, values))
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, path)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    return count
