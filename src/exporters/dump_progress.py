"""Rust DB Core dump/import progress tracking and event forwarding."""
import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple


def _format_import_phase_message(event: dict) -> Optional[str]:
    strategy = str(event.get("strategy") or "")
    performance = str(event.get("performance") or "")
    message = str(event.get("message") or "")
    if (
        strategy in {"temporary_local_infile", "temporary_local_infile_restore", "insert_fallback"}
        or performance == "fast_path"
        or "local_infile" in message
        or "LOAD DATA LOCAL" in message
    ):
        return None
    return str(event.get("message") or event.get("phase") or "Rust DB Core 작업 중...")


class TableProgressTracker:
    """Table progress metadata helper for the import UI."""

    def __init__(self, metadata: Optional[Dict]):
        self.chunk_counts = metadata.get("chunk_counts", {}) if metadata else {}
        self.table_sizes = metadata.get("table_sizes", {}) if metadata else {}
        self.total_bytes = metadata.get("total_bytes", 0) if metadata else 0
        self.completed_tables: Set[str] = set()

    def estimate_loading_tables(
        self,
        loaded_bytes: int,
        completed_tables: List[str],
    ) -> List[Tuple[str, int, int]]:
        self.completed_tables = set(completed_tables)
        candidates = [
            (table, self.table_sizes.get(table, 0), self.chunk_counts.get(table, 1))
            for table in self.table_sizes
            if table not in self.completed_tables and self.table_sizes.get(table, 0) > 10_000_000
        ]
        candidates.sort(key=lambda item: -item[1])
        return candidates[:4]

    def get_table_info(self, table_name: str) -> Tuple[int, int]:
        return self.table_sizes.get(table_name, 0), self.chunk_counts.get(table_name, 1)

    def format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        if size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


@dataclass
class DumpEventCallbacks:
    """Bundle of optional callbacks forwarded while a Rust dump/import runs."""

    progress: Optional[Callable[[str], None]] = None
    table_progress: Optional[Callable[[int, int, str], None]] = None
    detail: Optional[Callable[[dict], None]] = None
    table_status: Optional[Callable[[str, str, str], None]] = None
    raw_output: Optional[Callable[[str], None]] = None
    metadata: Optional[Callable[[dict], None]] = None
    table_chunk_progress: Optional[Callable[[str, int, int], None]] = None


def _handle_dump_plan_event(
    event: Dict,
    detail_callback: Optional[Callable[[dict], None]],
) -> None:
    if detail_callback:
        detail_callback({
            "event": "dump_plan",
            "tables_total": int(event.get("tables_total") or 0),
            "rows_total": int(event.get("rows_total") or 0),
            "tables": event.get("tables") if isinstance(event.get("tables"), list) else [],
        })


def _handle_dump_schedule_event(
    event: Dict,
    detail_callback: Optional[Callable[[dict], None]],
) -> None:
    if detail_callback:
        detail_callback({
            "event": "dump_schedule",
            "threads": int(event.get("threads") or 0),
            "table_workers": int(event.get("table_workers") or 0),
            "range_workers_per_table": int(event.get("range_workers_per_table") or 0),
            "chunk_size": int(event.get("chunk_size") or 0),
            "data_format": str(event.get("data_format") or ""),
            "compression": str(event.get("compression") or ""),
            "scheduled_tables": event.get("scheduled_tables") if isinstance(event.get("scheduled_tables"), list) else [],
        })


def _handle_phase_event(
    event: Dict,
    progress_callback: Optional[Callable[[str], None]],
) -> None:
    if not progress_callback:
        return
    message = _format_import_phase_message(event)
    if message:
        progress_callback(message)


def _handle_table_progress_event(
    event: Dict,
    table: str,
    table_progress_callback: Optional[Callable[[int, int, str], None]],
    table_status_callback: Optional[Callable[[str, str, str], None]],
    import_results: Optional[dict],
) -> None:
    current = int(event.get("current") or 0)
    total = int(event.get("total") or 0)
    status = str(event.get("status") or "")
    ui_status = "loading" if status in ("dumping", "importing") else "done" if status == "completed" else status
    if table_progress_callback and status == "completed":
        table_progress_callback(current, total, table)
    if table_status_callback and table:
        table_status_callback(table, ui_status, "")
    if import_results is not None and table:
        import_results[table] = {"status": ui_status or "loading", "message": ""}


def _handle_row_progress_event(
    event: Dict,
    table: str,
    detail_callback: Optional[Callable[[dict], None]],
    table_chunk_progress_callback: Optional[Callable[[str, int, int], None]],
) -> None:
    rows = int(event.get("table_rows_done") or event.get("rows") or 0)
    total = int(event.get("table_rows_total") or event.get("total") or 0)
    overall_rows = int(event.get("overall_rows_done") or 0)
    overall_total = int(event.get("overall_rows_total") or 0)
    chunk_rows = int(event.get("chunk_rows") or 0)
    elapsed_ms = int(event.get("stream_ms") or event.get("read_ms") or event.get("load_ms") or 0)
    rows_sec = int((chunk_rows * 1000) / elapsed_ms) if chunk_rows and elapsed_ms else 0
    if overall_total:
        percent = int((overall_rows / overall_total) * 100)
    else:
        percent = int((rows / total) * 100) if total else 0
    if detail_callback:
        detail_callback({
            "event": "row_progress",
            "table": table,
            "percent": min(percent, 100),
            "rows_done": rows,
            "rows_total": total,
            "overall_rows_done": overall_rows,
            "overall_rows_total": overall_total,
            "chunk_rows": chunk_rows,
            "rows_sec": rows_sec,
            "speed": f"{rows_sec:,} rows/s" if rows_sec else "Rust DB Core",
            "chunk_index": event.get("chunk_index"),
            "chunks_done": event.get("chunks_done"),
            "chunks_total": event.get("chunks_total"),
            "strategy": event.get("strategy"),
            "stream_ms": event.get("stream_ms"),
            "read_ms": event.get("read_ms"),
            "write_ms": event.get("write_ms"),
            "load_ms": event.get("load_ms"),
        })
    chunks_done = int(event.get("chunks_done") or 0)
    chunks_total = int(event.get("chunks_total") or 0)
    if table_chunk_progress_callback and table and chunks_done and chunks_total:
        table_chunk_progress_callback(table, chunks_done, chunks_total)


def emit_core_event(
    event: Dict,
    progress_callback: Optional[Callable[[str], None]] = None,
    table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
    detail_callback: Optional[Callable[[dict], None]] = None,
    table_status_callback: Optional[Callable[[str, str, str], None]] = None,
    raw_output_callback: Optional[Callable[[str], None]] = None,
    import_results: Optional[dict] = None,
    table_chunk_progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> None:
    event_type = event.get("event")
    table = str(event.get("table") or "")
    if raw_output_callback:
        raw_output_callback(json.dumps(event, ensure_ascii=False))

    handlers = {
        "dump_plan": lambda: _handle_dump_plan_event(event, detail_callback),
        "dump_schedule": lambda: _handle_dump_schedule_event(event, detail_callback),
        "phase": lambda: _handle_phase_event(event, progress_callback),
        "table_progress": lambda: _handle_table_progress_event(
            event, table, table_progress_callback, table_status_callback, import_results
        ),
        "row_progress": lambda: _handle_row_progress_event(
            event, table, detail_callback, table_chunk_progress_callback
        ),
    }
    handler = handlers.get(event_type)
    if handler:
        handler()
