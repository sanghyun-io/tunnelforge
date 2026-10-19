"""
Rust DB Core 기반 Import 다이얼로그
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QSpinBox, QPushButton, QComboBox,
    QCheckBox, QListWidget, QListWidgetItem, QGroupBox,
    QFileDialog, QMessageBox, QProgressBar,
    QWidget, QAbstractItemView,
    QSplitter, QScrollArea
)
from PyQt6.QtCore import Qt, QTimer, QUrl
from typing import List, Optional
from datetime import datetime
import json
import os

from src.core.connection_trust import apply_registered_tls
from src.core.constants import MAX_LOG_ENTRIES, MAX_VISIBLE_LOG_LINES, TABLE_STATUS_ICONS
from src.core.db_core_service import RustDbConnector, normalize_db_engine
from src.core.error_report_sanitizer import (
    sanitize_local_diagnostic,
    sanitize_local_diagnostic_data,
)
from src.core.i18n import translate_text
from src.core.logger import get_logger
from src.core.path_safety import safe_filename_component
from src.core.production_guard import ProductionGuard
from src.exporters.rust_dump_exporter import (
    DEFAULT_DUMP_THREADS, build_rust_dump_config, check_rust_dump, describe_copy_blockers, describe_core_failure,
    dump_original_namespace, restore_target_connection_info, describe_program_results, describe_program_warnings, describe_core_code, display_program_name,
)
from src.ui.dialogs.collapsible_config_dialog import CollapsibleConfigDialog
from src.ui.dialogs.import_plan_dialog import ImportPlanDialog, describe_plan, names_preview as _names_preview
from src.ui.workers.error_reporting_worker import (
    ErrorReportingMixin,
    report_operation_error,
    sanitize_local_diagnostic_json,
)
from src.ui.dialogs.backup_lifecycle_dialog import BackupLifecycleDialog
from src.ui.dialogs.job_recording import (
    begin_import_job, begin_promotion_job, finish_import_job, finish_promotion_job, made_copy, record_import_report,
)
from src.ui.workers.rust_dump_worker import RustDumpWorker
from src.core.migration_analyzer import DumpFileAnalyzer, CompatibilityIssue

logger = get_logger('db_dialogs')

def _sanitized_rust_event(event: dict) -> dict:
    """Return a cycle-safe, recursively sanitized Rust event copy."""
    sanitized = sanitize_local_diagnostic_data(event)
    return sanitized if type(sanitized) is dict else {}


def _sanitize_plain_rust_line(line: str) -> str:
    """JSON으로 파싱되지 않는 원시 출력 라인에서 자격 증명으로 보이는 조각을 마스킹."""
    return sanitize_local_diagnostic(line)


def _capture_import_audit(audit: dict, event: dict):
    """Retain local execution evidence independently of the bounded log ring."""
    phase = event.get("phase")
    if isinstance(phase, str) and phase:
        audit["phase"] = phase
    if (event.get("event") == "target_change" and event.get("action") == "drop_table"
            and event.get("status") == "completed" and isinstance(event.get("table"), str)):
        audit.setdefault("confirmed_drops", set()).add(event["table"])
        audit["destructive_phase"] = phase or "unknown"
    if event.get("event") == "import_report":
        for key in ("status", "report_path"):
            if isinstance(event.get(key), str):
                audit[key] = event[key]
        for key in ("dropped_tables", "dropped_not_restored_tables", "unattempted_tables", "dropped_foreign_keys",
                    "restored_foreign_keys", "triggers_created", "tables_lost_triggers", "programs_skipped_toggle",
                    "programs_skipped_partial", "programs_skipped_cross_engine", "program_warnings", "views_failed"):
            if isinstance(event.get(key), list):
                audit[key] = event[key]
        if isinstance(event.get("data_imported"), bool):
            audit["data_imported"] = event["data_imported"]
        if isinstance(event.get("programs_failed"), list):
            audit["programs_failed"] = event["programs_failed"]
        target = event.get("target")
        if isinstance(target, dict) and isinstance(target.get("schema"), str):
            name = target["schema"]
            if target.get("engine") == "postgresql" and isinstance(target.get("database"), str):
                name = f"{target['database']}.{name}"
            if event.get("namespace_created") is True:
                audit["created_namespace"] = name
            if event.get("namespace_creation_outcome_unknown") is True:
                audit["namespace_outcome_unknown"] = name

    if str(event.get("event", "")).startswith("safe_restore") or event.get("candidate_target"):
        for key in ("original_target", "candidate_target"):
            if isinstance(event.get(key), dict):
                audit[key] = restore_target_connection_info(event[key])
        for key in ("verified", "original_unchanged", "cutover_pending", "ready_for_switch", "namespace_existed",
                    "namespace_created", "candidate_created"):
            if isinstance(event.get(key), bool):
                audit[key] = event[key]
        if isinstance(event.get("status"), str):
            audit["restore_status"] = event["status"]
        if isinstance(event.get("blockers"), list):
            audit["blockers"] = event["blockers"]
        for key in ("restore_id", "report_path", "plan_digest", "error"):
            if isinstance(event.get(key), str):
                audit[key] = event[key]


def _replace_failure_details(audit: dict) -> str:
    """Which tables a failed per-table replace left empty, and which kept their data."""
    lines = []
    lost = [name for name in audit.get("dropped_not_restored_tables") or [] if isinstance(name, str)]
    kept = [name for name in audit.get("unattempted_tables") or [] if isinstance(name, str)]
    if lost:
        lines.append(translate_text("삭제 후 복원되지 않은 테이블: {}").format(_names_preview(lost)))
    trigger_losses = [name for name in audit.get("tables_lost_triggers") or [] if isinstance(name, str)]
    if trigger_losses:
        lines.append(translate_text("트리거를 다시 만들지 못한 테이블: {}").format(_names_preview(trigger_losses)))
    if kept and (audit.get("created_namespace") or audit.get("namespace_outcome_unknown")):
        # The target did not exist before this import, so these tables had no original data.
        lines.append(translate_text("아직 만들지 않은 테이블: {}").format(_names_preview(kept)))
    elif kept:
        lines.append(translate_text("원본 데이터를 유지한 테이블(미실행): {}").format(_names_preview(kept)))
    # A child that was never dropped (unattempted, or failed before its own DROP)
    # still holds its original rows but lost the FK to an already replaced parent.
    dropped = set(audit.get("dropped_tables") or [])
    restored = sorted(name for name in audit.get("restored_foreign_keys") or [] if isinstance(name, str))
    unlinked = sorted({
        f"{fk.get('table')}.{fk.get('constraint')}"
        for fk in audit.get("dropped_foreign_keys") or []
        if isinstance(fk, dict) and isinstance(fk.get("table"), str) and fk["table"] not in dropped
    } - set(restored))
    if unlinked:
        lines.append(translate_text("원본 테이블에서 제거된 외래 키: {}").format(_names_preview(unlinked)))
    if restored:
        lines.append(translate_text("다시 연결한 외래 키: {}").format(_names_preview(restored)))
    if audit.get("created_namespace"):
        lines.append(translate_text("이번 Import에서 새로 만든 대상(지우지 않고 남겨 둠): {}").format(audit["created_namespace"]))
    if audit.get("namespace_outcome_unknown"):
        lines.append(translate_text("새 대상을 만들었는지 확인하지 못함: {}").format(audit["namespace_outcome_unknown"]))
    failed_triggers = [f"{item.get('table')}.{item.get('name')}" for item in audit.get("programs_failed") or []
                       if isinstance(item, dict) and item.get("kind") == "TRIGGER"]
    if failed_triggers:
        lines.append(translate_text("Import가 멈춘 트리거: {}").format(_names_preview(failed_triggers)))
    made = [f"{item.get('table')}.{item.get('name')}" for item in audit.get("triggers_created") or [] if isinstance(item, dict)]
    if made:
        lines.append(translate_text("만든 트리거: {}").format(_names_preview(made)))
    for code in ("programs_skipped_cross_engine", "programs_skipped_partial", "programs_skipped_toggle"):
        names = [display_program_name(item) for item in audit.get(code) or []]
        if names:
            lines.append(describe_core_code(code) + " " + _names_preview(names))
    lines.extend(describe_program_warnings(audit.get("program_warnings")))
    return "\n".join(lines)


def _namespace_name(target: object) -> str:
    """A MySQL database, or a PostgreSQL database.schema."""
    target = target if isinstance(target, dict) else {}
    name = str(target.get("database") or "?")
    if target.get("engine") == "postgresql":
        name += "." + str(target.get("schema") or "?")
    return sanitize_local_diagnostic(name)


def _safe_copy_left(audit: dict) -> str:
    """The copy a failed verify-in-copy left in place; it is never removed automatically."""
    if audit.get("candidate_created") is not True:
        return ""
    name = _namespace_name(audit.get("candidate_target"))
    if audit.get("namespace_existed") is False:
        # The copy is the requested target itself, under its own name.
        return translate_text("이번 Import에서 새로 만든 대상(일부만 들어갔을 수 있음, 지우지 않고 남겨 둠): {}").format(name)
    return translate_text("이번 Import에서 만든 복사본(지우지 않고 남겨 둠): {}").format(name)


# Remembered between runs (F5). The other name and the timezone are never remembered: a wrong one
# writes into another database or shifts TIMESTAMP values.
_PERSISTED_IMPORT_OPTIONS = ("rust_dump_import_threads", "rust_dump_import_verify_in_copy")
VERIFY_TOGGLE_TEXT = "먼저 복사본에서 검증하고 교체"
_MODE_TEXT = {"replace": "덤프 테이블 교체", "safe": VERIFY_TOGGLE_TEXT}


def _mode_text(mode: str) -> str:
    return translate_text(_MODE_TEXT.get(mode, "확인 불가"))


# A verified copy the core still wants reviewed (ready_for_review) is never swapped.
_REVIEW_REASON = "대상에 덤프에 담을 수 없는 객체가 있어, 교체한 뒤에도 그대로 남는지 확인할 수 없습니다."
# The core's promote-plan reasons reach only the run log, not the Import report.
_PLAN_BLOCKED_REASON = "교체 직전에 다시 확인해 보니 교체를 막는 조건이 있었습니다. 아래 실행 로그에서 확인하세요."
_PLAN_BLOCKED_UNKNOWN = "교체를 막는 조건이 있습니다. 아래 실행 로그에서 확인하세요."
# A failure the core sent without a code: the window says so, the raw text opens from '자세히'.
_UNCODED_FAILURE_PREFIXES = ("Rust DB Core import 미리보기 오류:", "Import 미리보기 오류:",
                             "Rust DB Core import 오류:", "Import 오류:")


def _is_uncoded_core_failure(message: str) -> bool:
    return str(message).startswith(_UNCODED_FAILURE_PREFIXES)


def _is_preview(preview: object) -> bool:
    """A dump.import_preview result the plan dialog can read; anything else starts nothing (fail closed)."""
    return (isinstance(preview, dict) and preview.get("dry_run") is True
            and all(isinstance(preview.get(key), list) for key in ("replace", "create", "blockers")))


def _engine_word(engine: str) -> str:
    """What the user calls the place the tables go: a MySQL DB or a PostgreSQL schema."""
    return "스키마" if engine == "postgresql" else "DB"


_IMPORT_TELEMETRY_NUMERIC_FIELDS = {
    "phase": (),
    "table_progress": ("current", "total"),
    "row_progress": (
        "rows",
        "total",
        "overall_rows_done",
        "overall_rows_total",
        "chunk_rows",
        "chunks_done",
        "chunks_total",
        "stream_ms",
        "read_ms",
        "load_ms",
    ),
    "error": (),
}
_IMPORT_TELEMETRY_TEXT_FIELDS = {
    "phase": ("message", "strategy"),
    "table_progress": ("table", "status", "message"),
    "row_progress": ("table", "strategy"),
    "error": ("message",),
}


def _normalized_import_telemetry(event: object) -> Optional[dict]:
    """Return bounded recognized Import telemetry or reject malformed values."""
    if type(event) is not dict:
        return None
    event_type = event.get("event")
    if type(event_type) is not str or event_type not in _IMPORT_TELEMETRY_NUMERIC_FIELDS:
        return None
    normalized = {"event": event_type}
    for key in _IMPORT_TELEMETRY_TEXT_FIELDS[event_type]:
        value = event.get(key, "")
        if value is None:
            value = ""
        if type(value) is not str or len(value) > 2_000:
            return None
        normalized[key] = sanitize_local_diagnostic(value, max_length=2_000)
    for key in _IMPORT_TELEMETRY_NUMERIC_FIELDS[event_type]:
        value = event.get(key, 0)
        if value is None:
            value = 0
        if type(value) is not int or value < 0 or value > (2**64 - 1):
            return None
        normalized[key] = value

    if event_type == "phase" and not normalized["message"]:
        return None
    if event_type in {"table_progress", "row_progress"} and not normalized["table"]:
        return None
    if event_type == "table_progress":
        if normalized["status"] not in {"importing", "completed", "error"}:
            return None
        if normalized["total"] and normalized["current"] > normalized["total"]:
            return None
    elif event_type == "row_progress":
        if normalized["total"] and normalized["rows"] > normalized["total"]:
            return None
        if (
            normalized["overall_rows_total"]
            and normalized["overall_rows_done"]
            > normalized["overall_rows_total"]
        ):
            return None
        if (
            normalized["chunks_total"]
            and normalized["chunks_done"] > normalized["chunks_total"]
        ):
            return None
    return normalized


def format_import_row_labels(info: dict) -> tuple[str, str, str]:
    """Format import progress as row and chunk metrics instead of byte counters."""
    table = str(info.get("table") or "-")
    rows_done = max(0, int(info.get("rows_done") or 0))
    rows_total = max(0, int(info.get("rows_total") or 0))
    overall_rows_done = max(0, int(info.get("overall_rows_done") or 0))
    overall_rows_total = max(0, int(info.get("overall_rows_total") or 0))
    chunk_rows = max(0, int(info.get("chunk_rows") or 0))
    chunks_done = int(info.get("chunks_done") or 0)
    chunks_total = int(info.get("chunks_total") or 0)
    rows_sec = max(0, int(info.get("rows_sec") or 0))
    avg_rows_sec = max(0, int(info.get("avg_rows_sec") or 0))
    eta_seconds = max(0, int(info.get("eta_seconds") or 0))
    current_phase = str(info.get("current_phase") or "")
    strategy = str(info.get("strategy") or "")
    strategy_labels = {
        "insert_fallback": "안전 INSERT fallback",
        "load_data_local_infile": "LOAD DATA LOCAL",
        "parallel_load_data_local_infile": "병렬 LOAD DATA LOCAL",
        "insert_rows": "Rust INSERT",
    }
    strategy_label = strategy_labels.get(strategy, strategy)

    display_rows_done = overall_rows_done or rows_done
    display_rows_total = overall_rows_total or rows_total
    if display_rows_total:
        data_label = f"📦 처리 rows: {display_rows_done:,} / {display_rows_total:,} rows"
    else:
        data_label = f"📦 처리 rows: {display_rows_done:,} rows"

    if avg_rows_sec:
        current_speed = f"{rows_sec:,} rows/s" if rows_sec else "-"
        speed_label = f"⚡ 평균: {avg_rows_sec:,} rows/s · 현재: {current_speed}"
    else:
        speed_label = f"⚡ 속도: {rows_sec:,} rows/s" if rows_sec else "⚡ 속도: 계산 중..."

    if current_phase in {"post_load_ddl", "dump_import_post_load"}:
        return (
            data_label,
            speed_label,
            "🔄 현재 단계: 인덱스/FK 생성 중 · 데이터 Import 완료, 후처리 진행 중",
        )
    if current_phase in {"dump_import_switch", "safe_recreate_switch"}:
        return (
            data_label,
            speed_label,
            "🔄 현재 단계: 마무리 교체 중 · 데이터 Import 완료",
        )

    if chunks_done and chunks_total:
        status = f"{chunks_done}/{chunks_total} chunks"
    else:
        status = "chunk 진행 중"
    if chunk_rows:
        status = f"{status}, +{chunk_rows:,} rows"
    if strategy_label:
        status = f"{status}, {strategy_label}"
    if eta_seconds and display_rows_total and display_rows_done < display_rows_total:
        status = f"{status} · ETA {_format_compact_duration(eta_seconds)}"
    return data_label, speed_label, f"🔄 현재: {table} {status}"


def _format_compact_duration(seconds: int) -> str:
    remaining = max(0, int(seconds))
    hours, remainder = divmod(remaining, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def import_overall_percent(table_rows_done: dict, table_rows_total: dict) -> int:
    done = sum(max(0, int(value or 0)) for value in table_rows_done.values())
    total = sum(max(0, int(value or 0)) for value in table_rows_total.values())
    if total <= 0:
        return 0
    return min(int((done / total) * 100), 100)


def displayed_import_percent(table_rows_done: dict, table_rows_total: dict, event_percent: int = 0) -> int:
    """Return the visible import percent without mistaking table progress for overall progress."""
    if table_rows_total:
        percent = import_overall_percent(table_rows_done, table_rows_total)
        done = sum(max(0, int(value or 0)) for value in table_rows_done.values())
        return 1 if percent == 0 and done > 0 else percent
    return min(max(0, int(event_percent or 0)), 100)


def resolve_timezone_sql(engine: str, tz_mode: str) -> Optional[str]:
    """Return engine-specific timezone SQL for non-auto modes."""
    normalized_engine = (engine or "mysql").lower()
    normalized_mode = (tz_mode or "none").lower()
    if normalized_mode == "kst":
        return (
            "SET TIME ZONE '+09:00'"
            if normalized_engine == "postgresql"
            else "SET SESSION time_zone = '+09:00'"
        )
    if normalized_mode == "utc":
        return (
            "SET TIME ZONE '+00:00'"
            if normalized_engine == "postgresql"
            else "SET SESSION time_zone = '+00:00'"
        )
    return None


def format_import_visible_telemetry(event: dict) -> Optional[str]:
    """Convert Rust import telemetry into a concise visible log line."""
    event_type = event.get("event")
    table = str(event.get("table") or "")
    if event_type == "phase":
        message = str(event.get("message") or "")
        if "local_infile" in message or "LOAD DATA LOCAL" in message:
            return None
        return message
    if event_type == "table_progress" and table:
        status = str(event.get("status") or "")
        current = int(event.get("current") or 0)
        total = int(event.get("total") or 0)
        suffix = f" ({current}/{total})" if current and total else ""
        if status == "importing":
            return f"테이블 Import 시작: {table}{suffix}"
        if status == "completed":
            return f"테이블 Import 완료: {table}{suffix}"
        if status == "error":
            message = str(event.get("message") or "")
            return f"테이블 Import 오류: {table} - {message}" if message else f"테이블 Import 오류: {table}"
    if event_type == "row_progress" and table:
        rows = int(event.get("rows") or 0)
        total = int(event.get("total") or 0)
        overall_rows = int(event.get("overall_rows_done") or 0)
        overall_total = int(event.get("overall_rows_total") or 0)
        chunk_rows = int(event.get("chunk_rows") or 0)
        chunks_done = int(event.get("chunks_done") or 0)
        chunks_total = int(event.get("chunks_total") or 0)
        strategy = str(event.get("strategy") or "")
        elapsed_ms = int(event.get("stream_ms") or event.get("read_ms") or event.get("load_ms") or 0)
        rows_sec = int((chunk_rows * 1000) / elapsed_ms) if chunk_rows and elapsed_ms else 0
        parts = []
        if chunks_done and chunks_total:
            parts.append(f"{chunks_done}/{chunks_total} chunks")
        elif chunk_rows:
            parts.append(f"+{chunk_rows:,} rows")
        if overall_rows and overall_total:
            if rows and total:
                table_percent = min(100, int((rows / total) * 100))
                parts.append(f"table {rows:,}/{total:,} rows ({table_percent}%)")
            overall_percent = min(100, int((overall_rows / overall_total) * 100))
            parts.append(f"전체 {overall_rows:,}/{overall_total:,} rows ({overall_percent}%)")
        elif rows and total:
            percent = min(100, int((rows / total) * 100))
            parts.append(f"{rows:,}/{total:,} rows ({percent}%)")
        elif rows:
            parts.append(f"{rows:,} rows")
        if rows_sec:
            speed_prefix = "현재 " if overall_rows and overall_total else ""
            parts.append(f"{speed_prefix}{rows_sec:,} rows/s")
        if strategy:
            parts.append(strategy)
        return f"{table}: {', '.join(parts)}" if parts else f"{table}: row progress"
    if event_type == "error":
        return f"Import 오류: {event.get('message') or event}"
    return None


# ============================================================
# Rust DB Core 기반 Import 다이얼로그
# ============================================================

class RustDumpImportDialog(CollapsibleConfigDialog, ErrorReportingMixin, QDialog):
    """Rust DB Core Import 다이얼로그"""

    def __init__(self, parent=None, connector: RustDbConnector = None, config_manager=None,
                 tunnel_config: dict = None):
        super().__init__(parent)
        self.setWindowTitle("데이터 Import")
        self.resize(600, 700)

        self.connector = connector
        self.config_manager = config_manager
        self.tunnel_config = tunnel_config  # Production 환경 보호용
        self.worker: Optional[RustDumpWorker] = None

        # 익명 오류 보고 워커 목록 (완료 전까지 참조 유지)
        self._error_report_workers: List[object] = []
        self._cancel_requested = False
        self._close_after_cancel = False
        self._job_id = None  # 작업 목록 기록 (Import)
        self._promotion_job_id = None  # 작업 목록 기록 (안전 전환)

        self.rust_dump_installed, self.rust_dump_msg = check_rust_dump()

        # Import 결과 저장 (재시도용)
        self.import_results: dict = {}
        self.table_items: dict = {}  # 테이블명 -> QListWidgetItem 매핑
        self.last_input_dir: str = ""  # 마지막 사용한 input_dir
        self.last_target_schema: str = ""  # 마지막 사용한 target_schema
        self.last_import_mode: str = ""  # 마지막 실행에서 Core에 전달한 import mode
        self.last_import_context: dict = {}
        self.import_audit: dict = {}
        self.restore_config = None
        self._promotion_action = ""
        self._previewing = False  # 실행 전 대상 확인(dump.import_preview) 중
        self._preview_worker: Optional[RustDumpWorker] = None
        self._pending_request: dict = {}
        self._dump_original = ""  # 덤프에 적힌 원래 DB(스키마) 이름
        self._name_forced = False
        self._verify_allowed = True

        # 로그 수집용 변수
        self.log_entries: List[str] = []
        self.import_start_time: Optional[datetime] = None
        self.import_end_time: Optional[datetime] = None
        self.import_success: Optional[bool] = None

        # 메타데이터 정보
        self.dump_metadata: Optional[dict] = None
        self.import_table_rows_done: dict = {}
        self.import_table_rows_total: dict = {}

        # 테이블별 chunk 진행률 추적
        self.table_chunk_progress: dict = {}  # {table_name: (completed, total)}

        self.init_ui()
        self._load_import_options()
        self._load_default_input_dir()
        self.load_schemas()
        self._update_target_options()

    def init_ui(self):
        layout = QVBoxLayout(self)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(self.splitter)

        config_widget = QWidget()
        config_layout = QVBoxLayout(config_widget)
        config_layout.setContentsMargins(0, 0, 0, 0)

        collapse_layout = QHBoxLayout()
        self.btn_collapse = QPushButton("🔽 설정 접기")
        self.btn_collapse.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        self.btn_collapse.clicked.connect(self.toggle_config_section)
        self.btn_collapse.setVisible(False)  # 초기에는 숨김
        collapse_layout.addWidget(self.btn_collapse)
        collapse_layout.addStretch()
        config_layout.addLayout(collapse_layout)

        self.config_container = QWidget()
        container_layout = QVBoxLayout(self.config_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.addWidget(self._build_status_group())
        container_layout.addWidget(self._build_input_dir_group())
        container_layout.addWidget(self._build_upgrade_check_group())
        container_layout.addWidget(self._build_target_group())
        container_layout.addWidget(self._build_advanced_section())

        config_layout.addWidget(self.config_container)
        config_layout.addStretch()

        scroll_area = QScrollArea()
        scroll_area.setWidget(config_widget)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.splitter.addWidget(scroll_area)
        self.splitter.addWidget(self._build_progress_section())

        self.splitter.setStretchFactor(0, 60)
        self.splitter.setStretchFactor(1, 40)

        layout.addLayout(self._build_button_row())

    def _build_status_group(self):
        status_group = QGroupBox("Import 엔진 상태")
        status_layout = QVBoxLayout(status_group)

        if self.rust_dump_installed:
            status_label = QLabel(f"✅ {self.rust_dump_msg}")
            status_label.setStyleSheet("color: green;")
        else:
            status_label = QLabel(f"❌ {self.rust_dump_msg}")
            status_label.setStyleSheet("color: red;")

        status_layout.addWidget(status_label)
        return status_group

    def _build_input_dir_group(self):
        input_group = QGroupBox("덤프 폴더")
        input_layout = QHBoxLayout(input_group)

        self.input_dir = QLineEdit()
        self.input_dir.setPlaceholderText("Export로 만든 덤프 폴더를 고르세요")
        self.input_dir.editingFinished.connect(self._run_upgrade_check_for_current_input_dir)

        self.btn_browse = QPushButton("선택")
        self.btn_browse.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        self.btn_browse.clicked.connect(self.browse_input_dir)

        input_layout.addWidget(self.input_dir)
        input_layout.addWidget(self.btn_browse)
        return input_group

    def _build_upgrade_check_group(self):
        self.upgrade_check_group = QGroupBox("MySQL 8.4 호환성 검사")
        upgrade_check_layout = QVBoxLayout(self.upgrade_check_group)

        status_line = QHBoxLayout()
        self.lbl_upgrade_status = QLabel("📋 덤프 폴더를 고르면 자동으로 검사합니다.")
        self.lbl_upgrade_status.setStyleSheet("color: #7f8c8d;")
        status_line.addWidget(self.lbl_upgrade_status)
        status_line.addStretch()

        self.btn_view_issues = QPushButton("📊 상세 보기")
        self.btn_view_issues.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white;
                padding: 4px 12px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        self.btn_view_issues.setVisible(False)
        self.btn_view_issues.clicked.connect(self._show_upgrade_issues_dialog)
        status_line.addWidget(self.btn_view_issues)

        upgrade_check_layout.addLayout(status_line)

        self._upgrade_issues: List[CompatibilityIssue] = []

        return self.upgrade_check_group

    def _build_target_group(self):
        """Where the dump goes: the dump's own DB (PostgreSQL: schema) unless another name is given."""
        target_group = QGroupBox("넣을 곳")
        target_layout = QVBoxLayout(target_group)

        target_line = QHBoxLayout()
        self.lbl_target_endpoint = QLabel()
        self.lbl_target_name = QLabel()
        self.lbl_target_name.setStyleSheet("font-weight: bold;")
        # The name field sits on this line whenever a name is needed: the other-name toggle is on, or
        # the dump does not name its own DB (another engine, or an old dump).
        self.combo_target_schema = QComboBox()
        self.combo_target_schema.setEditable(True)
        self.combo_target_schema.setMinimumWidth(200)
        self.combo_target_schema.setVisible(False)
        self.combo_target_schema.currentTextChanged.connect(self._update_advanced_summary)
        target_line.addWidget(self.lbl_target_endpoint)
        target_line.addWidget(self.lbl_target_name)
        target_line.addWidget(self.combo_target_schema)
        target_line.addStretch()
        target_layout.addLayout(target_line)
        # Why the name field appeared without the toggle: the dump names no DB of this engine.
        self.lbl_name_forced = QLabel()
        self.lbl_name_forced.setStyleSheet("color: #b45309;")
        self.lbl_name_forced.setVisible(False)
        target_layout.addWidget(self.lbl_name_forced)

        # Follows the verify-in-copy toggle (_update_advanced_summary).
        self.lbl_mode_description = QLabel()
        self.lbl_mode_description.setWordWrap(True)
        self.lbl_mode_description.setStyleSheet("color: #7f8c8d;")
        target_layout.addWidget(self.lbl_mode_description)
        return target_group

    def _build_advanced_section(self):
        """'고급 설정 열기/닫기' (as in the migration dialog), closed at first; the line beside it names what is on."""
        section = QWidget()
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)

        button_line = QHBoxLayout()
        self.btn_advanced = QPushButton("고급 설정 열기")
        self.btn_advanced.clicked.connect(self._toggle_advanced_options)
        self.lbl_advanced_summary = QLabel()
        self.lbl_advanced_summary.setStyleSheet("color: #2c3e50;")
        button_line.addWidget(self.btn_advanced)
        button_line.addWidget(self.lbl_advanced_summary)
        button_line.addStretch()
        section_layout.addLayout(button_line)

        self.advanced_panel = QGroupBox("고급 설정")
        panel_layout = QVBoxLayout(self.advanced_panel)

        self.chk_other_name = QCheckBox("다른 DB 이름으로 넣기")
        self.chk_other_name.toggled.connect(self._on_other_name_toggled)
        panel_layout.addWidget(self.chk_other_name)

        self.chk_verify_copy = QCheckBox(VERIFY_TOGGLE_TEXT)
        self.chk_verify_copy.setToolTip("덤프를 먼저 새 복사본 DB(PostgreSQL은 스키마)에 넣어 검증하고, 문제가 없을 때만 "
                                        "원래 DB의 테이블과 바꿉니다. 바꾸기 전 테이블은 백업 DB(스키마)에 남습니다.")
        self.chk_verify_copy.toggled.connect(self._update_advanced_summary)
        self.lbl_verify_note = QLabel("다른 DB 종류에서 만든 덤프는 복사본에서 검증할 수 없습니다.")
        self.lbl_verify_note.setStyleSheet("color: #7f8c8d; margin-left: 20px;")
        self.lbl_verify_note.setVisible(False)
        panel_layout.addWidget(self.chk_verify_copy)
        panel_layout.addWidget(self.lbl_verify_note)

        self.chk_skip_triggers = QCheckBox("트리거 없이 테이블만 가져오기")
        self.chk_skip_triggers.setChecked(False)
        self.chk_skip_triggers.setToolTip("이 옵션을 켜면 덤프의 트리거를 만들지 않습니다. 교체하는 대상 테이블의 기존 트리거도 삭제되며 다시 만들지 않습니다.")
        self.chk_skip_triggers.toggled.connect(self._update_advanced_summary)
        panel_layout.addWidget(self.chk_skip_triggers)
        self.chk_verify_copy.toggled.connect(self._update_trigger_options)
        self.input_dir.textChanged.connect(self._update_trigger_options)

        timezone_line = QHBoxLayout()
        self.chk_timezone = QCheckBox("시간대 직접 정하기")
        self.chk_timezone.toggled.connect(self._on_timezone_toggled)
        self.combo_timezone = QComboBox()
        # QComboBox items are not translated by the Qt hooks.
        for text, mode in (("한국 시간(+09:00)", "kst"), ("세계 표준시(UTC)", "utc"), ("대상 서버 기본값", "none")):
            self.combo_timezone.addItem(translate_text(text), mode)
        self.combo_timezone.setEnabled(False)
        self.combo_timezone.currentIndexChanged.connect(self._update_advanced_summary)
        timezone_line.addWidget(self.chk_timezone)
        timezone_line.addWidget(self.combo_timezone)
        timezone_line.addStretch()
        panel_layout.addLayout(timezone_line)
        self.lbl_timezone_note = QLabel("이 덤프에는 시간대가 기록되어 있어 그대로 씁니다.")
        self.lbl_timezone_note.setStyleSheet("color: #7f8c8d; margin-left: 20px;")
        self.lbl_timezone_note.setVisible(False)
        panel_layout.addWidget(self.lbl_timezone_note)

        threads_line = QHBoxLayout()
        threads_line.addWidget(QLabel("동시 작업 수:"))
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(DEFAULT_DUMP_THREADS)
        self.spin_threads.valueChanged.connect(self._update_advanced_summary)
        threads_line.addWidget(self.spin_threads)
        threads_line.addStretch()
        panel_layout.addLayout(threads_line)

        self.advanced_panel.setVisible(False)
        section_layout.addWidget(self.advanced_panel)
        return section

    def _toggle_advanced_options(self):
        visible = self.advanced_panel.isHidden()
        self.advanced_panel.setVisible(visible)
        self.btn_advanced.setText("고급 설정 닫기" if visible else "고급 설정 열기")

    def _on_other_name_toggled(self, checked: bool):
        if checked:  # also when a dump forces it: the name is typed, never a DB picked for the user
            self.combo_target_schema.setCurrentIndex(-1)
            self.combo_target_schema.clearEditText()
        self._refresh_target_line()

    def _on_timezone_toggled(self, checked: bool):
        self.combo_timezone.setEnabled(checked and self.chk_timezone.isEnabled())
        self._update_advanced_summary()

    def _build_progress_section(self):
        progress_widget = QWidget()
        progress_main_layout = QVBoxLayout(progress_widget)
        progress_main_layout.setContentsMargins(0, 0, 0, 0)

        self.progress_group = QGroupBox("진행 상황")
        self.progress_group.setVisible(False)
        progress_layout = QVBoxLayout(self.progress_group)

        detail_layout = QHBoxLayout()

        left_detail = QVBoxLayout()
        self.label_percent = QLabel("📊 진행률: 0%")
        self.label_percent.setStyleSheet("font-weight: bold; font-size: 12pt;")
        self.label_data = QLabel("📦 데이터: 0 MB / 0 MB")
        self.label_speed = QLabel("⚡ 속도: 0 rows/s")
        self.label_tables = QLabel("📋 테이블: 0 / 0 완료")
        self.label_fk_status = QLabel("🔗 FK: 대기 중")
        self.label_fk_status.setStyleSheet("color: #7f8c8d;")
        left_detail.addWidget(self.label_percent)
        left_detail.addWidget(self.label_data)
        left_detail.addWidget(self.label_speed)
        left_detail.addWidget(self.label_tables)
        left_detail.addWidget(self.label_fk_status)

        detail_layout.addLayout(left_detail)
        detail_layout.addStretch()
        progress_layout.addLayout(detail_layout)

        # 프로그레스 바 (퍼센트 기준)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                text-align: center;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #27ae60;
                border-radius: 3px;
            }
        """)
        progress_layout.addWidget(self.progress_bar)

        self.label_status = QLabel("준비 중...")
        self.label_status.setStyleSheet("color: #27ae60; font-weight: bold;")
        progress_layout.addWidget(self.label_status)

        # 종료(완료/실패/중지) 후 Core 보고서 파일로 가는 링크
        self.label_report_link = QLabel()
        self.label_report_link.setOpenExternalLinks(True)
        self.label_report_link.setVisible(False)
        progress_layout.addWidget(self.label_report_link)

        progress_main_layout.addWidget(self.progress_group)

        self.table_status_group = QGroupBox("테이블 Import 상태")
        self.table_status_group.setVisible(False)
        table_status_layout = QVBoxLayout(self.table_status_group)

        self.table_list = QListWidget()
        self.table_list.setMinimumHeight(150)
        self.table_list.setMaximumHeight(200)
        # 일부 테이블 재시도는 지원하지 않으므로 선택할 수 있다는 오해를 주지 않는다.
        self.table_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table_list.setStyleSheet("""
            QListWidget {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                background-color: #fafafa;
            }
            QListWidget::item {
                padding: 4px 8px;
                border-bottom: 1px solid #ecf0f1;
            }
            QListWidget::item:selected {
                background-color: #e8f4f8;
            }
        """)
        table_status_layout.addWidget(self.table_list)

        retry_layout = QHBoxLayout()
        # 일부 테이블만 재시도하면 인덱스/FK 복원이 누락되므로, 원래 범위 전체를 같은 설정으로 다시 실행한다.
        self.btn_rerun = QPushButton("🔄 같은 설정으로 다시 Import")
        self.btn_rerun.setVisible(False)
        self.btn_rerun.setToolTip("덤프 전체를 지금 설정으로 다시 Import합니다. 시작 전에 다시 확인합니다.")
        self.btn_rerun.clicked.connect(lambda: self.do_import())
        self.label_rerun_hint = QLabel("대상 데이터 상태를 확인한 뒤 다시 실행하세요.")
        self.label_rerun_hint.setStyleSheet("color: gray; font-size: 11px;")
        self.label_rerun_hint.setVisible(False)
        # After a run that made a copy: manage it with this session's credentials. The job list's
        # route needs a saved profile with saved credentials, which a main-menu Import may not have.
        self.btn_manage_backups = QPushButton("복원 백업 관리")
        self.btn_manage_backups.setVisible(False)
        self.btn_manage_backups.clicked.connect(self.open_backup_lifecycle)

        retry_layout.addWidget(self.btn_rerun)
        retry_layout.addWidget(self.label_rerun_hint)
        retry_layout.addWidget(self.btn_manage_backups)
        retry_layout.addStretch()
        table_status_layout.addLayout(retry_layout)

        progress_main_layout.addWidget(self.table_status_group)

        self.log_group = QGroupBox("실행 로그")
        self.log_group.setVisible(False)
        log_layout = QVBoxLayout(self.log_group)

        self.txt_log = QListWidget()
        self.txt_log.setMinimumHeight(80)
        self.txt_log.setMaximumHeight(120)
        self.txt_log.setStyleSheet("""
            QListWidget {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 9pt;
                background-color: #2c3e50;
                color: #ecf0f1;
                border: 1px solid #34495e;
                border-radius: 4px;
            }
            QListWidget::item {
                padding: 2px 4px;
            }
        """)
        log_layout.addWidget(self.txt_log)

        progress_main_layout.addWidget(self.log_group)
        return progress_widget

    def _build_button_row(self):
        button_layout = QHBoxLayout()

        self.btn_import = QPushButton("📥 Import 시작")
        self.btn_import.setStyleSheet("""
            QPushButton {
                background-color: #e67e22; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #d35400; }
        """)
        self.btn_import.clicked.connect(lambda: self.do_import())
        self.btn_import.setEnabled(self.rust_dump_installed)

        self.btn_save_log = QPushButton("📄 로그 저장")
        self.btn_save_log.setStyleSheet("""
            QPushButton {
                background-color: #9b59b6; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #8e44ad; }
            QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }
        """)
        self.btn_save_log.clicked.connect(self.save_log)
        self.btn_save_log.setEnabled(False)
        self.btn_save_log.setToolTip("Import 완료 후 로그를 파일로 저장할 수 있습니다.")

        # 실행 중에만 보인다. 중지를 요청해도 창은 닫지 않고 최종 상태와 보고서를 보여 준다.
        self.btn_stop = QPushButton("⏹ 중지")
        self.btn_stop.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #c0392b; }
            QPushButton:disabled { background-color: #f5b7b1; color: #f8f9f9; }
        """)
        self.btn_stop.setVisible(False)
        self.btn_stop.clicked.connect(lambda: self.request_stop(close_after=False))

        btn_cancel = QPushButton("닫기")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_cancel.clicked.connect(self.close)

        button_layout.addWidget(self.btn_save_log)
        button_layout.addStretch()
        button_layout.addWidget(self.btn_import)
        button_layout.addWidget(self.btn_stop)
        button_layout.addWidget(btn_cancel)
        return button_layout

    def load_schemas(self):
        self.combo_target_schema.clear()
        if not self.connector:
            return
        schemas = self.connector.get_schemas()
        for schema in schemas:
            self.combo_target_schema.addItem(schema)
        # Starts empty: the first existing DB is no choice the user made (_import_request refuses an empty name).
        self.combo_target_schema.setCurrentIndex(-1)

    def _load_import_options(self):
        """Remembered toggles; a saved value of the wrong type (older or edited settings) is ignored."""
        if not self.config_manager:
            return
        threads = self.config_manager.get_app_setting(_PERSISTED_IMPORT_OPTIONS[0], DEFAULT_DUMP_THREADS)
        if type(threads) is int and 1 <= threads <= 16:
            self.spin_threads.setValue(threads)
        self.chk_verify_copy.setChecked(self.config_manager.get_app_setting(_PERSISTED_IMPORT_OPTIONS[1], False) is True)

    def _persisted_import_options(self) -> dict:
        return dict(zip(_PERSISTED_IMPORT_OPTIONS, (self.spin_threads.value(), self.chk_verify_copy.isChecked())))

    @staticmethod
    def _read_manifest(dump_dir: str) -> dict:
        try:
            with open(os.path.join(dump_dir, '_tunnelforge_dump.json'), 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            return {}
        return manifest if isinstance(manifest, dict) else {}

    def _update_target_options(self):
        """Follow the chosen dump: the name it gives the target, and which toggles it allows."""
        path = self.input_dir.text().strip()
        manifest = self._read_manifest(path) if path else {}
        engine = build_rust_dump_config(self.connector).engine
        self._dump_original = dump_original_namespace(manifest, engine) if manifest else ""
        # The dump names no DB of this engine (another engine, an old dump): the name must be typed.
        forced = bool(manifest) and not self._dump_original
        if forced:
            self.chk_other_name.setChecked(True)
        elif self._name_forced:
            self.chk_other_name.setChecked(False)
        self._name_forced = forced
        self.chk_other_name.setEnabled(not forced)
        pg = engine == "postgresql"
        self.chk_other_name.setText("다른 스키마 이름으로 넣기" if pg else "다른 DB 이름으로 넣기")
        forced_text = ("이 덤프에는 여기서 쓸 스키마 이름이 없어 직접 정해야 합니다." if pg
                       else "이 덤프에는 여기서 쓸 DB 이름이 없어 직접 정해야 합니다.")
        self.chk_other_name.setToolTip(
            forced_text if forced else "덤프에 적힌 원래 이름 대신 직접 정한 이름에 넣습니다. 없는 이름이면 새로 만듭니다.")
        self.lbl_name_forced.setText(forced_text)
        self.lbl_name_forced.setVisible(forced)
        self.combo_target_schema.setToolTip(
            "기존 스키마를 고르거나 새 이름을 입력하세요. 없는 이름이면 새로 만듭니다." if pg
            else "기존 DB를 고르거나 새 이름을 입력하세요. 없는 이름이면 새로 만듭니다.")
        # Verify-in-copy needs a same-engine dump (safe_restore.rs copy_refusals).
        self._verify_allowed = not manifest or normalize_db_engine(manifest.get("source_engine") or engine) == engine
        self.chk_verify_copy.setEnabled(self._verify_allowed)
        self.lbl_verify_note.setVisible(not self._verify_allowed)
        # Current dumps are written in UTC and record it; another timezone would shift their TIMESTAMP values.
        legacy = bool(manifest) and not manifest.get("source_timezone")
        if not legacy:
            self.chk_timezone.setChecked(False)
        self.chk_timezone.setEnabled(legacy)
        self.combo_timezone.setEnabled(legacy and self.chk_timezone.isChecked())
        self.lbl_timezone_note.setVisible(bool(manifest) and not legacy)
        self._update_trigger_options()
        self._refresh_target_line()

    def _update_trigger_options(self, *_):
        path = self.input_dir.text().strip()
        manifest = self._read_manifest(path) if path else {}
        has_triggers = not path or any(isinstance(p, dict) and p.get("kind") == "TRIGGER" for p in manifest.get("stored_programs") or [])
        allowed = getattr(self, "_ui_enabled", True) and has_triggers and build_rust_dump_config(self.connector).engine == "mysql"
        self.chk_skip_triggers.setEnabled(allowed)
        if not allowed:
            self.chk_skip_triggers.setChecked(False)

    def _uses_other_name(self) -> bool:
        return self.chk_other_name.isChecked()

    def _target_endpoint_text(self) -> str:
        config = build_rust_dump_config(self.connector)
        text = f"{config.host}:{config.port} /"
        profile = self.tunnel_config.get("name") if isinstance(self.tunnel_config, dict) else ""
        if profile:  # over a tunnel the host is a local port: the profile says which server it is
            text = f"{profile} ({config.host}:{config.port}) /"
        if config.engine == "postgresql":
            # Without a configured database the core uses `postgres`.
            text += f" {config.database or 'postgres'} /"
        return text

    def _refresh_target_line(self):
        other = self._uses_other_name()
        self.lbl_target_endpoint.setText(self._target_endpoint_text())
        self.lbl_target_name.setText(self._dump_original or translate_text("덤프 폴더를 고르면 표시됩니다"))
        self.lbl_target_name.setVisible(not other)
        self.combo_target_schema.setVisible(other)
        self._update_advanced_summary()

    def _advanced_summary(self) -> str:
        """Every toggle that is on or forced, in one line."""
        items = []
        if self.chk_skip_triggers.isChecked():
            items.append(translate_text("트리거 없이 테이블만 가져오기"))
        if self._uses_other_name():
            word = _engine_word(build_rust_dump_config(self.connector).engine)
            name = self.combo_target_schema.currentText().strip()
            items.append(translate_text(f"다른 {word} 이름({{}})").format(name) if name
                         else translate_text(f"다른 {word} 이름(입력 필요)"))
        if self.chk_verify_copy.isChecked() and self._verify_allowed:
            items.append(translate_text("먼저 복사본에서 검증"))
        if self.chk_timezone.isChecked():
            items.append(translate_text("시간대 {}").format(self.combo_timezone.currentText()))
        if self.spin_threads.value() != DEFAULT_DUMP_THREADS:
            items.append(translate_text("동시 작업 {}개").format(self.spin_threads.value()))
        return translate_text("적용 중인 고급 설정: {}").format(", ".join(items)) if items else ""

    def _mode_description(self) -> str:
        word = _engine_word(build_rust_dump_config(self.connector).engine)
        if self.chk_verify_copy.isChecked() and self._verify_allowed:
            return translate_text(f"덤프를 먼저 복사본 {word}에 넣어 검증하고, 문제가 없을 때만 원래 {word}의 테이블과 바꿉니다. "
                                  f"바꾸기 전 테이블은 백업 {word}에 남습니다. 검증 중에 멈추면 원본은 그대로입니다.")
        return translate_text("덤프의 테이블을 하나씩 지우고 다시 만들어 넣습니다. 덤프에 없는 테이블은 그대로 둡니다. "
                              "중간에 멈추면 이미 덮어쓴 테이블은 되돌리지 않고, 작업 중이던 테이블은 비어 있을 수 있습니다.")

    def _update_advanced_summary(self, *_):
        self.lbl_advanced_summary.setText(self._advanced_summary())
        self.lbl_mode_description.setText(self._mode_description())

    def browse_input_dir(self):
        start_dir = self._get_input_browse_start_dir()
        folder = QFileDialog.getExistingDirectory(
            self, "덤프 폴더 선택", start_dir
        )
        if folder:
            self.input_dir.setText(folder)
            if self.config_manager:
                self.config_manager.set_app_setting('rust_dump_import_dir', folder)
                self.config_manager.set_app_setting('rust_dump_last_dump_dir', folder)
            # 폴더 선택 시 자동으로 MySQL 8.4 호환성 검사 실행
            self._run_upgrade_check(folder)

    def _is_valid_dump_dir(self, path: str) -> bool:
        if not path:
            return False
        return (
            os.path.isdir(path)
            and os.path.exists(os.path.join(path, "_tunnelforge_dump.json"))
        )

    def _existing_dir(self, path: str) -> str:
        if path and os.path.isdir(path):
            return path
        return ""

    def _configured_dump_dirs(self) -> List[str]:
        if not self.config_manager:
            return []
        keys = (
            'rust_dump_last_dump_dir',
            'rust_dump_export_dir',
            'rust_dump_import_dir',
        )
        return [
            str(self.config_manager.get_app_setting(key, "") or "")
            for key in keys
        ]

    def _load_default_input_dir(self):
        """마지막 Export/Import dump 폴더를 Import 기본값으로 사용."""
        for path in self._configured_dump_dirs():
            if self._is_valid_dump_dir(path):
                self.input_dir.setText(path)
                self._run_upgrade_check(path)
                return

    def _run_upgrade_check_for_current_input_dir(self):
        """input_dir 텍스트가 직접 수정된 경우(자동완성/붙여넣기 포함) 호환성 검사를 갱신."""
        path = self.input_dir.text().strip()
        if self._is_valid_dump_dir(path):
            self._run_upgrade_check(path)
        else:
            self._upgrade_issues = []
            self.btn_view_issues.setVisible(False)
            self.lbl_upgrade_status.setText("📋 덤프 폴더를 고르면 자동으로 검사합니다.")
            self.lbl_upgrade_status.setStyleSheet("color: #7f8c8d;")
            self._update_target_options()

    def _get_input_browse_start_dir(self) -> str:
        """Dump 선택창 시작 위치. 빈 값이면 Windows가 설치 폴더를 잡으므로 항상 명시한다."""
        current = self._existing_dir(self.input_dir.text().strip())
        if current:
            return current

        for path in self._configured_dump_dirs():
            existing = self._existing_dir(path)
            if existing:
                return existing

        if self.config_manager:
            base_dir = self.config_manager.get_app_setting('rust_dump_export_base_dir', "")
            existing = self._existing_dir(str(base_dir or ""))
            if existing:
                return existing

        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        return desktop if os.path.isdir(desktop) else os.path.expanduser("~")

    def _run_upgrade_check(self, dump_path: str):
        """Import 전 MySQL 8.4 호환성 검사"""
        self._update_target_options()
        self.lbl_upgrade_status.setText("🔍 호환성 검사 중...")
        self.lbl_upgrade_status.setStyleSheet("color: #3498db;")
        self.btn_view_issues.setVisible(False)

        try:
            analyzer = DumpFileAnalyzer()
            result = analyzer.analyze_dump_folder(dump_path)

            self._upgrade_issues = result.compatibility_issues
            error_count = sum(1 for i in self._upgrade_issues if i.severity == "error")
            warning_count = sum(1 for i in self._upgrade_issues if i.severity == "warning")

            if error_count > 0:
                self.lbl_upgrade_status.setText(
                    f"⚠️ 호환성 이슈: {error_count}개 오류, {warning_count}개 경고"
                )
                self.lbl_upgrade_status.setStyleSheet("color: #e74c3c; font-weight: bold;")
                self.btn_view_issues.setVisible(True)
            elif warning_count > 0:
                self.lbl_upgrade_status.setText(
                    f"⚠️ 호환성 경고: {warning_count}개 (Import 가능)"
                )
                self.lbl_upgrade_status.setStyleSheet("color: #f39c12;")
                self.btn_view_issues.setVisible(True)
            else:
                self.lbl_upgrade_status.setText("✅ 호환성 검사 통과")
                self.lbl_upgrade_status.setStyleSheet("color: #27ae60;")
                self.btn_view_issues.setVisible(False)

        except Exception as e:
            self.lbl_upgrade_status.setText(f"❌ 검사 실패: {str(e)}")
            self.lbl_upgrade_status.setStyleSheet("color: #e74c3c;")
            self._upgrade_issues = []

    def _show_upgrade_issues_dialog(self):
        """호환성 이슈 상세 다이얼로그 표시"""
        if not self._upgrade_issues:
            return

        from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView

        dialog = QDialog(self)
        dialog.setWindowTitle("MySQL 8.4 호환성 이슈 상세")
        dialog.resize(800, 500)

        layout = QVBoxLayout(dialog)

        # 요약
        error_count = sum(1 for i in self._upgrade_issues if i.severity == "error")
        warning_count = sum(1 for i in self._upgrade_issues if i.severity == "warning")
        info_count = sum(1 for i in self._upgrade_issues if i.severity == "info")

        summary_label = QLabel(
            f"<b>총 {len(self._upgrade_issues)}개 이슈</b>: "
            f"<span style='color:red'>❌ 오류 {error_count}</span>, "
            f"<span style='color:orange'>⚠️ 경고 {warning_count}</span>, "
            f"<span style='color:blue'>ℹ️ 정보 {info_count}</span>"
        )
        layout.addWidget(summary_label)

        # 테이블
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["심각도", "유형", "위치", "설명", "권장 조치"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setRowCount(len(self._upgrade_issues))

        severity_icons = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}

        for i, issue in enumerate(self._upgrade_issues):
            severity_text = f"{severity_icons.get(issue.severity, '')} {issue.severity.upper()}"
            table.setItem(i, 0, QTableWidgetItem(severity_text))
            table.setItem(i, 1, QTableWidgetItem(issue.issue_type.value))
            table.setItem(i, 2, QTableWidgetItem(issue.location))
            table.setItem(i, 3, QTableWidgetItem(issue.description))
            table.setItem(i, 4, QTableWidgetItem(issue.suggestion))

        layout.addWidget(table)

        # 닫기 버튼
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(dialog.close)
        layout.addWidget(btn_close)

        dialog.exec()

    def set_ui_enabled(self, enabled: bool):
        """Import 진행 중 UI 요소 활성화/비활성화"""
        self._ui_enabled = enabled
        for widget in (self.input_dir, self.btn_browse, self.chk_other_name, self.combo_target_schema,
                       self.chk_verify_copy, self.chk_timezone, self.combo_timezone, self.spin_threads, self.btn_import,
                       self.btn_rerun, self.btn_manage_backups, self.chk_skip_triggers):
            widget.setEnabled(enabled)
        self.btn_stop.setVisible(not enabled)
        if not enabled:
            self.btn_stop.setEnabled(True)
            self.btn_stop.setText("⏹ 중지")
        else:
            self._update_target_options()  # what the dump allows (forced name, verify-in-copy, timezone)

    def _add_log(self, msg: str):
        """로그 항목 추가 (수집용, 최대 500개 유지)"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = f"[{timestamp}] {sanitize_local_diagnostic(msg)}"
        self.log_entries.append(log_entry)
        if len(self.log_entries) > MAX_LOG_ENTRIES:
            del self.log_entries[:-MAX_LOG_ENTRIES]

    def _log_visible(self, msg: str):
        """A line the stop window points to: in the run log on screen as well as in the saved log."""
        self.txt_log.addItem(sanitize_local_diagnostic(msg))
        self.txt_log.scrollToBottom()
        self._add_log(msg)

    def _get_dump_schema_name(self, dump_dir: str) -> str:
        """Read the destination namespace from the Rust dump manifest."""
        try:
            done_json_path = os.path.join(dump_dir, '_tunnelforge_dump.json')
            if not os.path.exists(done_json_path):
                return ""
            with open(done_json_path, 'r', encoding='utf-8') as f:
                done_data = json.load(f)
            engine = getattr(self.connector, "engine", None) or done_data.get("source_engine") or "mysql"
            return dump_original_namespace(done_data, engine)
        except Exception:
            logger.debug("Failed to read dump schema name from %s", dump_dir, exc_info=True)
            return ""

    def _import_request(self) -> Optional[dict]:
        """What this run sends, used by the preview, the import, the logs and the job label.

        None after the input check has shown why the run cannot start.
        """
        input_dir = self.input_dir.text()
        if not input_dir:
            QMessageBox.warning(self, "오류", "덤프 폴더를 고르세요.")
            return None
        if not os.path.exists(input_dir):
            QMessageBox.warning(self, "오류", "폴더가 존재하지 않습니다.")
            return None
        if not self._is_valid_dump_dir(input_dir):
            QMessageBox.warning(self, "오류", "이 폴더에는 TunnelForge 덤프가 없습니다. Export로 만든 덤프 폴더를 선택하세요.")
            return None
        self._update_target_options()
        other = self._uses_other_name()
        name = self.combo_target_schema.currentText().strip() if other else self._dump_original
        engine = build_rust_dump_config(self.connector).engine
        if not name:
            word = _engine_word(engine)
            if self._name_forced:
                QMessageBox.warning(self, translate_text("이름 입력 필요"), translate_text(
                    f"넣을 {word} 이름을 정할 수 없습니다. '넣을 곳'에 {word} 이름을 입력하세요."))
            else:
                QMessageBox.warning(self, translate_text("이름 입력 필요"), translate_text(f"넣을 {word} 이름을 입력하세요."))
            return None
        timezone = self.combo_timezone.currentData() if self.chk_timezone.isChecked() else None
        verify = self.chk_verify_copy.isChecked() and self._verify_allowed
        return {
            "input_dir": input_dir,
            "namespace": name,
            "other_name": name if other else "",
            "verify": verify,
            "import_mode": "safe" if verify else "replace",
            "skip_triggers": self.chk_skip_triggers.isEnabled() and self.chk_skip_triggers.isChecked(),
            "threads": self.spin_threads.value(),
            # 대상 서버 기본값 turns the dump's own timezone off; the other two choices set one.
            "timezone_sql": resolve_timezone_sql(engine, timezone) if timezone else None,
            "use_source_timezone": timezone != "none",
            "timezone_label": self.combo_timezone.currentText() if timezone else "",
        }

    @staticmethod
    def _worker_options(request: dict) -> dict:
        """The payload the preview and the import share; the import adds its mode."""
        return {key: request[key] for key in ("input_dir", "threads", "timezone_sql", "use_source_timezone")} | {
            "target_schema": request["namespace"], "skip_triggers": request.get("skip_triggers", False)}

    def do_import(self):
        """Check the input, ask the core what the run would do, confirm when needed, then import."""
        if self.worker and self.worker.isRunning():  # a second click (e.g. the rerun button) while a run is going
            return
        self._cancel_requested = False
        self._close_after_cancel = False
        self._promotion_action = ""
        request = self._import_request()
        if request is None:
            return
        self._pending_request = request
        self.restore_config = build_rust_dump_config(self.connector)
        self.set_ui_enabled(False)
        self.btn_stop.setVisible(False)  # the check only reads the target: nothing to stop
        self.btn_import.setText("⏳ 대상 확인 중…")
        self._previewing = True
        # Kept apart from self.worker, which the import replaces while this thread may still be ending.
        self._preview_worker = RustDumpWorker("import_preview", self.restore_config, import_mode=request["import_mode"], **self._worker_options(request))
        self.worker = self._preview_worker
        self.worker.preview_finished.connect(self._on_preview_finished)
        self.worker.start()

    def _on_preview_finished(self, success: bool, message: str, preview: dict):
        sender = self.sender()
        if sender is not None and sender is not self._preview_worker:
            return  # an earlier preview's late answer
        self._previewing = False
        self.btn_import.setText("📥 Import 시작")
        if self._cancel_requested or self._close_after_cancel:
            self.set_ui_enabled(True)
            if self._close_after_cancel:
                QTimer.singleShot(0, self.close)
            return
        if success and not _is_preview(preview):
            # The one confirmation must not open on a result it cannot read.
            success, message = False, f"Import 미리보기 오류: unexpected preview result {sanitize_local_diagnostic_json(preview)}"
        if not success:
            self.set_ui_enabled(True)
            cause = describe_core_failure(message)
            uncoded = not cause and _is_uncoded_core_failure(message)
            text = cause or (translate_text("실행 전 확인 중에 오류가 나서 Import를 시작하지 않았습니다. '자세히'에서 원문을 확인하세요.")
                             if uncoded else sanitize_local_diagnostic(message))
            self._show_failure("Import를 시작하지 않았습니다", text, message if cause or uncoded else "")
            return
        if not self._confirm_plan(self._pending_request, preview):
            self.set_ui_enabled(True)
            return
        self._accepted_program_preview = preview
        self._start_import(self._pending_request)

    def _plan_notes(self, request: dict, preview: dict) -> List[str]:
        """Warnings only the application knows: FK parents added at export, the PostgreSQL `postgres` fallback."""
        notes = []
        try:
            with open(os.path.join(request["input_dir"], "_export_metadata.json"), encoding="utf-8") as f:
                metadata = json.load(f)
        except (OSError, ValueError):
            metadata = {}
        added = [name for name in (metadata.get("added_fk_tables") if isinstance(metadata, dict) else None) or []
                 if isinstance(name, str)]
        dumped = {str(name) for key in ("replace", "create") for name in preview.get(key) or []}
        kept = [name for name in added if name in dumped]
        if kept:
            notes.append("- " + translate_text("내보낼 때 연결된 부모 테이블이라 함께 담은 테이블: {}").format(_names_preview(kept)))
        if len(kept) < len(added):
            notes.append("- " + translate_text("내보낼 때 함께 담으려 했지만 덤프에 들어가지 않은 부모 테이블: {}").format(
                _names_preview([name for name in added if name not in dumped])))
        config = build_rust_dump_config(self.connector)
        if config.engine == "postgresql" and not config.database:
            notes.append("- " + translate_text("연결에 DB가 정해져 있지 않아 postgres DB에 넣습니다."))
        return notes

    def _confirm_plan(self, request: dict, preview: dict) -> bool:
        """The one confirmation before a run (F12): only when it overwrites an existing table or view, or has
        something to show (a blocker, a warning that the data may be off). Production must type the name in it.
        """
        environment = ProductionGuard.get_environment(self.tunnel_config or {})
        plan = describe_plan(preview, request["verify"], request["timezone_label"])
        views = preview.get("views") if isinstance(preview.get("views"), dict) else {}
        if not preview["replace"] and not views.get("replace") and not plan["must_see"]:
            return True
        plan["warnings"] += self._plan_notes(request, preview)
        dialog = ImportPlanDialog(self, preview, plan, environment,
                                  f"{self._target_endpoint_text()} {request['namespace']}", request["namespace"],
                                  self._advanced_summary())
        # The start button is off with a blocker; a dialog that returns Accepted anyway still starts nothing.
        return dialog.exec() == QDialog.DialogCode.Accepted and not plan["blockers"]

    def _start_import(self, request: dict):
        self._begin_error_report_operation()
        input_dir = request["input_dir"]
        namespace = request["namespace"]
        self.last_input_dir = input_dir
        self.last_target_schema = namespace
        self.last_import_mode = request["import_mode"]
        if self.config_manager:
            self.config_manager.set_app_settings({'rust_dump_import_dir': input_dir, 'rust_dump_last_dump_dir': input_dir,
                                                  **self._persisted_import_options()})

        # UI 상태 변경 - 모든 입력 비활성화
        self.set_ui_enabled(False)
        self.label_report_link.setVisible(False)
        self.btn_rerun.setVisible(False)
        self.label_rerun_hint.setVisible(False)
        self.btn_manage_backups.setVisible(False)
        self.btn_save_log.setEnabled(False)

        # 설정 섹션 접기
        self.collapse_config_section()

        # 로그 및 진행 상황 UI 표시
        self.progress_group.setVisible(True)
        self.table_status_group.setVisible(True)
        self.log_group.setVisible(True)

        self.txt_log.clear()
        self.table_list.clear()
        self.table_items.clear()
        self.import_results.clear()
        # 이전 실행의 잔여 진행 상태 초기화 (누적 방지)
        self.import_table_rows_done.clear()
        self.import_table_rows_total.clear()
        self.table_chunk_progress.clear()
        self.dump_metadata = None
        # 로그 수집 초기화
        self.log_entries.clear()
        self.import_start_time = datetime.now()
        self.import_end_time = None
        self.import_success = None
        self.import_audit = {}

        # 로그 헤더 추가
        self._add_log(f"{'='*60}")
        self._add_log("Rust DB Core Import 시작")
        self._add_log(f"시작 시간: {self.import_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._add_log(f"덤프 폴더: {input_dir}")
        self._add_log(f"넣을 곳: {namespace}")
        self._add_log(f"실행 방식: {_mode_text(request['import_mode'])}")
        if self._advanced_summary():
            self._add_log(self._advanced_summary())
        self._add_log(f"동시 작업 수: {request['threads']}")
        self._add_log(f"{'='*60}")

        # 프로그레스 바 초기화
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(100)
        self.label_percent.setText("📊 진행률: 0%")
        self.label_data.setText("📦 데이터: 0 MB / 0 MB")
        self.label_speed.setText("⚡ 속도: 0 rows/s")
        self.label_tables.setText("📋 테이블: 0 / 0 완료")
        self.label_status.setText("Import 준비 중...")

        config = self.restore_config
        self.last_import_context = {
            "engine": config.engine,
            "host": config.host,
            "port": config.port,
            "database": (config.database or "postgres") if config.engine == "postgresql" else namespace,
            "schema": namespace if config.engine == "postgresql" else "",
            "namespace": namespace,
            "other_name": request["other_name"],
        }
        profile = self.tunnel_config if isinstance(self.tunnel_config, dict) else {}
        self.last_import_context["profile"] = {
            key: profile[key]
            for key in ("name", "environment", "connection_mode", "remote_host", "remote_port", "bastion_host")
            if isinstance(profile.get(key), (str, int))
        }

        # 작업 스레드 시작
        self._job_id = begin_import_job(self, namespace)
        self.worker = RustDumpWorker("import", config, import_mode=request["import_mode"], **self._worker_options(request))

        # 시그널 연결
        self.worker.progress.connect(self.on_progress)
        self.worker.table_progress.connect(self.on_table_progress)
        self.worker.detail_progress.connect(self.on_detail_progress)
        self.worker.table_status.connect(self.on_table_status)
        self.worker.raw_output.connect(self.on_raw_output)
        self.worker.import_finished.connect(self.on_import_finished)
        self.worker.finished.connect(self.on_finished)
        self.worker.metadata_analyzed.connect(self.on_metadata_analyzed)
        self.worker.table_chunk_progress.connect(self.on_table_chunk_progress)
        self.worker.start()

    def on_progress(self, msg: str):
        """일반 진행 메시지 처리"""
        msg = sanitize_local_diagnostic(msg)
        self.txt_log.addItem(msg)
        self.txt_log.scrollToBottom()
        self.label_status.setText(msg)
        self._add_log(msg)

        # FK 관련 메시지 감지 시 FK 상태 라벨 업데이트
        if "FK 제약조건" in msg or "FK 재연결" in msg:
            self.label_fk_status.setText(msg)
            if "백업 중" in msg or "재연결 중" in msg:
                self.label_fk_status.setStyleSheet("color: #3498db; font-weight: bold;")
            elif "완료" in msg:
                if "실패 0" in msg or ("성공" in msg and "실패" not in msg):
                    self.label_fk_status.setStyleSheet("color: #27ae60; font-weight: bold;")
                else:
                    self.label_fk_status.setStyleSheet("color: #e67e22; font-weight: bold;")

    def on_table_progress(self, current: int, total: int, table_name: str):
        """테이블별 진행률 업데이트"""
        self.label_tables.setText(f"📋 테이블: {current} / {total} 완료")
        display_table = sanitize_local_diagnostic(table_name)
        self._add_log(f"테이블 완료: {display_table} ({current}/{total})")

    def on_detail_progress(self, info: dict):
        """상세 진행 정보 업데이트"""
        info = dict(info)
        table = str(info.get('table') or "")
        if table:
            self.import_table_rows_done[table] = max(0, int(info.get('rows_done') or 0))
        if table and table not in self.import_table_rows_total:
            self.import_table_rows_total[table] = max(0, int(info.get('rows_total') or 0))
        overall_done = max(0, int(info.get('overall_rows_done') or 0))
        overall_total = max(0, int(info.get('overall_rows_total') or 0))
        if not overall_total and self.import_table_rows_total:
            overall_done = sum(max(0, int(value or 0)) for value in self.import_table_rows_done.values())
            overall_total = sum(max(0, int(value or 0)) for value in self.import_table_rows_total.values())
            info["overall_rows_done"] = overall_done
            info["overall_rows_total"] = overall_total
        if self.import_start_time and overall_done:
            elapsed_seconds = max((datetime.now() - self.import_start_time).total_seconds(), 0.001)
            avg_rows_sec = int(overall_done / elapsed_seconds)
            if avg_rows_sec:
                info["avg_rows_sec"] = avg_rows_sec
                if overall_total and overall_done < overall_total:
                    info["eta_seconds"] = int((overall_total - overall_done) / avg_rows_sec)
        if overall_total:
            percent = min(int((overall_done / overall_total) * 100), 100)
        else:
            percent = displayed_import_percent(
                self.import_table_rows_done,
                self.import_table_rows_total,
                int(info.get('percent') or 0),
            )
        display_info = dict(info)
        for key in ("table", "current_phase", "strategy"):
            if key in display_info:
                display_info[key] = sanitize_local_diagnostic(
                    display_info[key]
                )
        data_label, speed_label, status_label = format_import_row_labels(
            display_info
        )

        self.progress_bar.setValue(percent)
        self.label_percent.setText(f"📊 전체 진행률: {percent}%")
        self.label_data.setText(data_label)
        self.label_speed.setText(speed_label)
        self.label_status.setText(status_label)

    def _format_bytes(self, size_bytes: int) -> str:
        size_mb = size_bytes / (1024 * 1024)
        if size_mb < 1024:
            return f"{size_mb:.1f} MB"
        return f"{size_mb / 1024:.2f} GB"

    def _table_results(self) -> dict:
        return {
            table: result
            for table, result in self.import_results.items()
            if table != 'fk_restore' and isinstance(result, dict)
        }

    def _count_by_status(self, results: dict, status: str) -> int:
        return sum(1 for result in results.values() if result.get('status') == status)

    def on_table_status(self, table_name: str, status: str, message: str):
        """테이블 상태 업데이트 (메타데이터 정보 포함)"""
        icon = '⏸' if status == 'blocked' else TABLE_STATUS_ICONS.get(status, '❓')
        display_table = sanitize_local_diagnostic(table_name)
        display_message = sanitize_local_diagnostic(message)

        # 메타데이터에서 테이블 정보 가져오기
        size_info = ""
        chunk_info = ""
        if self.dump_metadata and 'table_sizes' in self.dump_metadata:
            size_bytes = self.dump_metadata['table_sizes'].get(table_name, 0)
            chunk_count = self.dump_metadata['chunk_counts'].get(table_name, 1)

            if size_bytes > 0:
                size_info = f" ({self._format_bytes(size_bytes)})"

                # chunk 진행률 표시 (loading 상태이고 chunk가 2개 이상인 경우)
                if status == 'loading' and chunk_count > 1 and table_name in self.table_chunk_progress:
                    completed, total = self.table_chunk_progress[table_name]
                    chunk_percent = (completed / total * 100) if total > 0 else 0
                    chunk_info = f" [{completed}/{total} chunks, {chunk_percent:.0f}%]"

        # 기존 아이템이 있으면 업데이트, 없으면 새로 생성
        if table_name in self.table_items:
            item = self.table_items[table_name]
            display_text = f"{icon} {display_table}{size_info}{chunk_info}"
            if status in ('error', 'blocked') and message:
                display_text += f" - {display_message[:50]}..."
            item.setText(display_text)
            item.setForeground(Qt.GlobalColor.black)
        else:
            display_text = f"{icon} {display_table}{size_info}{chunk_info}"
            if status in ('error', 'blocked') and message:
                display_text += f" - {display_message[:50]}..."
            item = QListWidgetItem(display_text)
            self.table_list.addItem(item)
            self.table_items[table_name] = item

        # 결과 저장
        self.import_results[table_name] = {'status': status, 'message': message}

        # 로그에 테이블 상태 변경 기록 (done/error만)
        if status in ('done', 'error'):
            status_text = '완료' if status == 'done' else f'오류: {display_message}'
            self._add_log(f"테이블 [{display_table}] {status_text}")

    def on_table_chunk_progress(self, table_name: str, completed_chunks: int, total_chunks: int):
        """
        테이블별 chunk 진행률 업데이트 (다중 파일 병렬 다운로드 스타일)

        Args:
            table_name: 테이블명
            completed_chunks: 완료된 chunk 수
            total_chunks: 전체 chunk 수
        """
        # 진행률 저장
        self.table_chunk_progress[table_name] = (completed_chunks, total_chunks)

        # 테이블 아이템이 존재하면 업데이트
        if table_name in self.table_items:
            item = self.table_items[table_name]

            # 현재 상태 확인
            current_status = self.import_results.get(table_name, {}).get('status', 'loading')
            icon = TABLE_STATUS_ICONS.get(current_status, '❓')

            # 크기 정보
            size_info = ""
            if self.dump_metadata and 'table_sizes' in self.dump_metadata:
                size_bytes = self.dump_metadata['table_sizes'].get(table_name, 0)
                if size_bytes > 0:
                    size_info = f" ({self._format_bytes(size_bytes)})"

            # chunk 진행률 표시
            chunk_percent = (completed_chunks / total_chunks * 100) if total_chunks > 0 else 0
            if total_chunks > 1:
                # 다중 chunk 테이블: "🔄 df_subs (1.29 GB) [45/81 chunks, 55%]"
                chunk_info = f" [{completed_chunks}/{total_chunks} chunks, {chunk_percent:.0f}%]"
            else:
                # 단일 chunk 테이블: 진행률 표시 안 함
                chunk_info = ""

            display_table = sanitize_local_diagnostic(table_name)
            display_text = f"{icon} {display_table}{size_info}{chunk_info}"

            # error 상태이면 메시지 추가
            if current_status == 'error':
                message = self.import_results.get(table_name, {}).get('message', '')
                if message:
                    display_message = sanitize_local_diagnostic(message)
                    display_text += f" - {display_message[:50]}..."

            item.setText(display_text)

    def on_raw_output(self, line: str):
        """Rust Core 실시간 출력 처리.

        원시 JSONL 라인은 자격 증명을 포함할 수 있으므로 화면/로그에 그대로
        남기지 않는다. 표시/저장은 정제된 요약(visible_summary)만 사용한다.
        """
        # 너무 많은 로그 방지
        if self.txt_log.count() > MAX_VISIBLE_LOG_LINES:
            self.txt_log.takeItem(0)

        visible_summary = None
        sanitized_event = None
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                sanitized_event = {key: sanitize_local_diagnostic_data(value) for key, value in event.items() if key != "plan"}
                self.import_audit = getattr(self, "import_audit", {})
                known = (self.import_audit.get("report_path"), self.import_audit.get("candidate_created"))
                _capture_import_audit(self.import_audit, sanitized_event)
                # The report and, once a copy exists, the label that opens backup management are on the
                # running record at once: a run the app never finishes (crash, power loss) still has both.
                if (self.import_audit.get("report_path"), self.import_audit.get("candidate_created")) != known:
                    record_import_report(self)
                normalized_event = _normalized_import_telemetry(sanitized_event)
                if normalized_event is None:
                    visible_summary = sanitize_local_diagnostic_json(
                        sanitized_event
                    )
                else:
                    visible_summary = format_import_visible_telemetry(
                        normalized_event
                    )
            else:
                visible_summary = sanitize_local_diagnostic_json(event)
        except (
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            visible_summary = (
                sanitize_local_diagnostic_json(sanitized_event)
                if sanitized_event is not None
                else _sanitize_plain_rust_line(line)
            )

        if visible_summary:
            visible_summary = sanitize_local_diagnostic(visible_summary)
            self.txt_log.addItem(visible_summary)
            self.txt_log.scrollToBottom()
            self._add_log(visible_summary)

    def on_metadata_analyzed(self, metadata: dict):
        """
        Dump 메타데이터 분석 결과 처리

        메타데이터 구조:
        {
            'chunk_counts': {'table_name': chunk_count, ...},
            'table_sizes': {'table_name': bytes, ...},
            'total_bytes': int,
            'schema': str
        }
        """
        self.dump_metadata = metadata
        self.import_table_rows_total = {
            str(table): int(rows or 0)
            for table, rows in (metadata.get('table_rows') or {}).items()
        }

        # 대용량 테이블 정보를 테이블 상태 목록에 표시
        if metadata and 'table_sizes' in metadata:
            large_tables = [
                (name, size, metadata['chunk_counts'].get(name, 1))
                for name, size in metadata['table_sizes'].items()
                if size > 50_000_000  # 50MB 이상
            ]
            large_tables.sort(key=lambda x: -x[1])

            if large_tables:
                # 상위 대용량 테이블을 미리 표시 (pending 상태로)
                for table_name, size_bytes, chunk_count in large_tables[:10]:
                    size_str = self._format_bytes(size_bytes)

                    display_table = sanitize_local_diagnostic(table_name)
                    display = f"⏳ {display_table} ({size_str}, {chunk_count} chunks)"
                    item = QListWidgetItem(display)
                    item.setForeground(Qt.GlobalColor.gray)
                    self.table_list.addItem(item)
                    self.table_items[table_name] = item

    def on_import_finished(self, success: bool, message: str, results: dict):
        """Import 완료 처리 (결과 저장 및 다시 Import 버튼 표시)"""
        self.import_results = results

        failed_tables = [
            table for table, result in self._table_results().items()
            if result.get('status') == 'error'
        ]

        if failed_tables:
            self.btn_rerun.setVisible(True)
            self.label_rerun_hint.setVisible(True)
            self.txt_log.addItem(f"⚠️ {len(failed_tables)}개 테이블 Import 실패")

    def _only_accepted_program_omissions(self):
        audit = self.import_audit
        if audit.get("views_failed") or audit.get("programs_failed"):
            return False
        preview = getattr(self, "_accepted_program_preview", {})
        expected_functions = {name for warning in preview.get("warnings", [])
                              if warning.get("code") == "routine_binlog_unsafe" for name in warning.get("names", [])}
        omitted_functions = {warning.get("name") for warning in audit.get("program_warnings", [])
                             if warning.get("code") == "routine_binlog_unsafe"}
        if not omitted_functions.issubset(expected_functions):
            return False
        any_omitted = bool(omitted_functions)
        for key in ("programs_skipped_cross_engine", "programs_skipped_partial", "programs_skipped_toggle"):
            omitted = set(audit.get(key) or [])
            if not omitted.issubset(set(preview.get(key) or [])):
                return False
            any_omitted |= bool(omitted)
        return any_omitted

    def _report_error_anonymously(self):
        """Submit a privacy-allowlisted report in the background."""
        report_operation_error(self, "import", "dump.import")

    def on_finished(self, success: bool, message: str):
        """작업 완료 처리"""
        new_target = (self.import_audit.get("restore_status") == "completed_new_target"
                      and self.import_audit.get("namespace_existed") is False
                      and self.import_audit.get("namespace_created") is True)
        if self.last_import_mode == "safe" and success and not (
            self.import_audit.get("verified") is True
            and self.import_audit.get("original_unchanged") is True
            and self.import_audit.get("cutover_pending") is (not new_target)
            and (new_target or self.import_audit.get("restore_status") in ("ready_for_switch", "ready_for_review"))
        ):
            success = False
            message = translate_text("복사본 검증 결과를 확인할 수 없습니다. 원본과 복사본의 상태를 Import 보고서에서 확인하세요.")
        # Line by line: the sanitizer writes a newline as a literal backslash-n, and the
        # failure window lists one reason per line.
        message = "\n".join(sanitize_local_diagnostic(line) for line in str(message).splitlines())
        # 로그 기록
        self.import_end_time = datetime.now()
        self.import_success = success

        table_results = self._table_results()
        done_count = self._count_by_status(table_results, 'done')
        error_count = self._count_by_status(table_results, 'error')
        blocked_count = self._count_by_status(table_results, 'blocked')
        total_count = len(table_results)

        partial = (not success and not self._cancel_requested and self.last_import_mode != "safe"
                   and self.import_audit.get("status") == "partial" and self.import_audit.get("data_imported") is True and error_count == 0)
        finish_import_job(self, success, message, done_count, error_count, blocked_count)

        self._add_log(f"{'='*60}")
        self._add_log(translate_text("Import 일부 완료") if partial else f"Import {'성공' if success else '실패'}")
        self._add_log(f"종료 시간: {self.import_end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        if self.import_start_time:
            elapsed = self.import_end_time - self.import_start_time
            self._add_log(f"소요 시간: {elapsed}")
        self._add_log(f"성공: {done_count}개 테이블")
        self._add_log(f"실패: {error_count}개 테이블")
        self._add_log(f"미실행: {blocked_count}개 테이블")
        if not success:
            self._add_log("전체 Import 검증 및 후처리가 완료되지 않았습니다. 완료 표시는 테이블 데이터 적재 상태입니다.")

        # FK 복원 결과 표시
        fk_restore = self.import_results.get('fk_restore', {})
        if fk_restore:
            fk_success = fk_restore.get('success', 0)
            fk_fail = fk_restore.get('fail', 0)
            fk_msg = f"🔗 FK 재연결: 성공 {fk_success}, 실패 {fk_fail}"
            self.label_fk_status.setText(fk_msg)
            self._add_log(f"FK 재연결: 성공 {fk_success}, 실패 {fk_fail}")

            if fk_fail > 0:
                self.label_fk_status.setStyleSheet("color: #e74c3c; font-weight: bold;")
                self._add_log("⚠️ 일부 FK 연결에 실패했습니다:")
                for err in fk_restore.get('errors', []):
                    self._add_log(f"  - {err.get('constraint_name', 'unknown')}: {err.get('error', '')}")
            else:
                self.label_fk_status.setStyleSheet("color: #27ae60; font-weight: bold;")
        else:
            self.label_fk_status.setText("🔗 FK: -")
            self.label_fk_status.setStyleSheet("color: #7f8c8d;")

        self._add_log(f"결과 메시지: {message}")
        self._add_log(f"{'='*60}")

        # UI 상태 복구
        self.set_ui_enabled(True)
        self.btn_save_log.setEnabled(True)  # 로그 저장 버튼 활성화
        self._show_report_link()
        self.btn_manage_backups.setVisible(made_copy(self.import_audit))

        if success and self.last_import_mode == "safe" and not new_target:
            self.progress_bar.setValue(100)
            if self.import_audit.get("restore_status") == "ready_for_switch":
                # Verify-in-copy swaps a clean copy in without asking again: the plan dialog was the one confirmation.
                self.label_status.setText(translate_text("검증 완료 · 교체할 수 있는지 확인합니다"))
                self._add_log(self.label_status.text())
                QTimer.singleShot(0, self._plan_promotion)
            else:
                self._stop_and_explain(["- " + translate_text(_REVIEW_REASON)])
        elif partial:
            self.progress_bar.setValue(100)
            sentence = translate_text("Import 일부 완료: 테이블 데이터는 가져왔지만 일부 객체는 복원하지 못했습니다.")
            self.label_status.setText("⚠️ " + sentence)
            visible = {self.txt_log.item(i).text() for i in range(self.txt_log.count())}
            for line in message.splitlines():
                if line not in visible:
                    self._log_visible(line)
            self._show_failure("Import 일부 완료", message)
            if not self._only_accepted_program_omissions():
                self._report_error_anonymously()
        elif success:
            sentence = self._success_sentence(new_target, total_count)
            self.label_status.setText(f"✅ {sentence}")
            self.progress_bar.setValue(100)
            self.txt_log.addItem(f"✅ {sentence}")
            self._add_log(sentence)
        else:
            self.label_status.setText(
                f"⏹ Import 중지됨: {done_count}/{total_count} 테이블 완료"
                if self._cancel_requested
                else f"❌ Import 실패: {error_count}/{total_count} 테이블 오류"
            )
            if self.last_import_mode == "safe" and not self._cancel_requested:
                self.label_status.setText(
                    translate_text("Import 실패 · 원본은 그대로입니다")
                    if self.import_audit.get("original_unchanged") is True
                    else translate_text("Import 실패 · 원본 상태는 Import 보고서에서 확인하세요")
                )
            # The core's own text stays behind '자세히'; the window shows its code's sentence.
            cause = describe_core_failure(message)
            self.txt_log.addItem(f"❌ 실패: {cause or message}")

            if self._cancel_requested:
                self._add_log("Import가 중지되었습니다.")
            else:
                if self.last_import_mode == "safe":
                    # A stop before the copy arrives as plain sentences; the core's own text is its error.
                    core_text = str(self.import_audit.get("error") or "")
                    text = self.label_status.text() + "\n\n" + (cause or message)
                    copy_left = _safe_copy_left(self.import_audit)
                    self._show_failure("Import 실패", f"{text}\n\n{copy_left}" if copy_left else text,
                                       message if cause else ("" if core_text in message else core_text))
                else:
                    details = _replace_failure_details(getattr(self, "import_audit", {}))
                    uncoded = not cause and _is_uncoded_core_failure(message)
                    if error_count > 0:
                        text = translate_text("❌ Import 중 오류가 발생했습니다.\n\n"
                            "{}\n\n"
                            "성공: {}개 테이블\n"
                            "실패: {}개 테이블\n"
                            "미실행: {}개 테이블\n\n"
                            "대상 데이터를 확인한 뒤 '같은 설정으로 다시 Import'로 다시 실행하세요.").format(
                                cause or translate_text("원인을 분류하지 못했습니다. '자세히'에서 원문을 확인하세요."),
                                done_count, error_count, blocked_count)
                    elif uncoded:
                        text = "❌ " + translate_text("Import 중에 오류가 나서 멈췄습니다. '자세히'에서 원문을 확인하세요.")
                    else:
                        text = f"❌ {cause or message}"
                    self._show_failure("Import 실패", f"{text}\n\n{details}" if details else text,
                                       message if cause or uncoded or error_count > 0 else "")

                self._report_error_anonymously()

        if self._close_after_cancel:
            QTimer.singleShot(0, self.close)

    def _show_failure(self, title: str, text: str, details: str = ""):
        """Failure window; ``details`` (the core's own text) opens only from its '자세히' button."""
        if not details:
            QMessageBox.warning(self, title, text)
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle(title)
        box.setText(text)
        # Not a standard button or setDetailedText: their Qt texts are never translated.
        box.addButton(translate_text("닫기"), QMessageBox.ButtonRole.RejectRole)
        more = box.addButton(translate_text("자세히"), QMessageBox.ButtonRole.ActionRole)
        more.clicked.disconnect()  # QMessageBox closes on any of its buttons; this one keeps the window open.
        more.clicked.connect(lambda: QMessageBox.information(self, title, details))
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def _show_report_link(self):
        """Core 가 이번 실행에 쓴 Import 보고서가 있으면 링크로 보여 준다 (중지/실패 후에도 상태 확인용).
        실행마다 보고서 파일이 따로 생기므로 Core 가 알려 준 파일만 쓴다. Dump 폴더의 다른 보고서는 이전 실행의 것이다."""
        path = str(self.import_audit.get("report_path") or "")
        if path and os.path.isfile(path):
            self.label_report_link.setText('<a href="{}">{}</a>'.format(
                QUrl.fromLocalFile(path).toString(), translate_text("📄 Import 보고서 열기")))
            self.label_report_link.setToolTip(path)
            self.label_report_link.setVisible(True)
        else:
            self.label_report_link.setVisible(False)

    def _success_sentence(self, new_target: bool, table_count: int) -> str:
        """One sentence per result, for the status line and the log; success opens no window."""
        word = _engine_word(self.last_import_context.get("engine"))
        created = (_namespace_name(self.import_audit.get("candidate_target")) if new_target
                   else self.import_audit.get("created_namespace"))
        if created:
            return translate_text(f"새 {word}를 만들어 넣었습니다: {{}}").format(created)
        # The count covers every dump table, overwritten or newly created.
        return translate_text("덤프 테이블 {}개를 넣었습니다").format(table_count)

    def _stop_and_explain(self, reasons: List[str]):
        """Verify-in-copy ended without a swap: one window with a Close button. The copy stays; its cleanup
        opens from this window, the dialog's own button or the job list."""
        text = "\n".join([
            translate_text("복사본 {}에 넣고 검증했지만 다음 이유로 교체하지 않았습니다.").format(
                _namespace_name(self.import_audit.get("candidate_target"))),
            *reasons, "",
            translate_text("원본은 그대로입니다. 복사본은 '복원 백업 관리'에서 정리할 수 있습니다."),
        ])
        self.import_audit["operator_choice"] = "not_swapped"
        self.label_status.setText(translate_text("교체하지 않았습니다 · 원본은 그대로입니다"))
        self._add_log(text)
        self.btn_manage_backups.setVisible(True)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle(translate_text("교체하지 않았습니다"))
        box.setText(text)
        manage = box.addButton(translate_text("복원 백업 관리"), QMessageBox.ButtonRole.ActionRole)
        manage.clicked.connect(self.open_backup_lifecycle)
        box.addButton(translate_text("닫기"), QMessageBox.ButtonRole.RejectRole)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def open_backup_lifecycle(self):
        """The copy (and after a swap, the backup) this run made, with this session's credentials:
        the job list's route needs a saved profile with saved credentials."""
        original = restore_target_connection_info(self.import_audit.get("original_target"))
        report_path = self.import_audit.get("report_path")
        if not self.restore_config or not original or not report_path:
            QMessageBox.information(self, "복원 백업 관리", "복사본을 만든 Import 보고서가 없어 열 수 없습니다.")
            return
        original.update(user=self.restore_config.user, password=self.restore_config.password)
        dialog = BackupLifecycleDialog(apply_registered_tls(original), [os.path.dirname(str(report_path))], self)
        dialog.refresh()
        dialog.exec()

    def _promotion_payload(self, action: str) -> dict:
        original = restore_target_connection_info(self.import_audit.get("original_target"))
        if not self.restore_config or not original or not self.import_audit.get("restore_id") or not self.import_audit.get("report_path"):
            raise ValueError(translate_text("복사본 검증 기록이 없습니다. Import 보고서를 확인하세요."))
        original.update(user=self.restore_config.user, password=self.restore_config.password)
        return {"action": action, "restore_id": self.import_audit["restore_id"],
                "report_path": self.import_audit["report_path"], "target": apply_registered_tls(original)}

    def _plan_promotion(self):
        try:
            self._start_promotion(self._promotion_payload("plan"))
        except ValueError as exc:
            self._stop_and_explain([f"- {exc}"])

    def _start_promotion(self, payload: dict):
        if self._close_after_cancel:
            return
        if self.worker and self.worker.isRunning():
            self.set_ui_enabled(False)
            QTimer.singleShot(50, lambda: self._start_promotion(payload) if not self._cancel_requested else None)
            return
        self._cancel_requested = False
        self._promotion_action = payload["action"]
        self.set_ui_enabled(False)
        if self._promotion_action == "plan":
            self.btn_stop.setVisible(False)  # 읽기 전용 비교 단계: 중지할 변경이 없다
        if self._promotion_action == "confirm":
            self.import_success = None
            self.import_audit["original_unchanged"] = None
            self.import_audit["restore_status"] = "promotion_in_progress"
            self._add_log("교체 시작. 결과가 확인될 때까지 원본 상태는 알 수 없습니다.")
            self._promotion_job_id = begin_promotion_job(self, payload)
        self.worker = RustDumpWorker("promote", self.restore_config, payload=payload)
        self.worker.raw_output.connect(self.on_raw_output)
        self.worker.promotion_finished.connect(self._on_promotion_finished)
        self.worker.start()

    def _on_promotion_finished(self, success: bool, message: str, result: dict):
        action = self._promotion_action
        if action == "confirm":
            finish_promotion_job(self, success, message, result)
        self.set_ui_enabled(True)
        self.btn_save_log.setEnabled(True)
        if action == "confirm":
            self._show_report_link()
            self.import_audit["promotion_result"] = {key: sanitize_local_diagnostic_data(result.get(key)) for key in (
                "status", "success", "original_unchanged", "cutover_pending", "backup_namespace", "report_path", "phase", "message", "missing_programs", "pending_program", "programs_failed", "programs_imported", "program_warnings", "program_verification_error")}

        if action == "plan":
            if self._close_after_cancel:
                pass
            elif self._cancel_requested:
                self.label_status.setText(translate_text("⏹ 교체 확인을 중지했습니다 · 원본은 그대로입니다"))
            elif success and self._plan_is_clean(result):
                self._remember_promotion_plan(result)
                self.import_audit["operator_choice"] = "overwrite_confirmed_before_import"
                self._add_log("검증된 복사본으로 교체를 시작합니다.")
                payload = self._promotion_payload("confirm")
                payload.update(plan_digest=result["plan_digest"], overwrite_confirmed=True)
                self._start_promotion(payload)
            else:
                reasons = []
                if success:
                    self._remember_promotion_plan(result)
                    reasons = describe_copy_blockers(result.get("copy_blockers"), unknown=_PLAN_BLOCKED_UNKNOWN)
                    for blocker in result.get("blockers") or []:
                        self._log_visible(f"교체 차단: {blocker}")
                else:
                    self._log_visible(message or "Promotion plan failed")
                cause = describe_core_failure(message) if not success else None
                self._stop_and_explain(reasons or ["- " + (cause or translate_text(_PLAN_BLOCKED_REASON))])
        elif action == "confirm" and success and result.get("status") == "promoted":
            self.import_success = True
            self.import_end_time = datetime.now()
            self.import_audit.update(restore_status="promoted", original_unchanged=False, cutover_pending=False)
            self.import_audit["backup_namespace"] = str(result.get("backup_namespace") or "unknown")
            self.import_audit["candidate_target"] = restore_target_connection_info(result.get("active_target") or self.import_audit.get("original_target"))
            word = _engine_word((self.import_audit.get("original_target") or {}).get("engine"))
            self.label_status.setText("✅ " + translate_text(f"교체했습니다. 이전 테이블은 백업 {word} {{}}에 있습니다").format(
                sanitize_local_diagnostic(self.import_audit["backup_namespace"])))
            self.progress_bar.setValue(100)
            self._add_log(self.label_status.text())
            for line in describe_plan(result, True)["warnings"]:
                self._log_visible(line)
            for line in describe_program_warnings(result.get("program_warnings")):
                self._log_visible(line)
        elif action == "confirm" and result.get("status") in ("promoted_incomplete", "failed_original_triggers_missing"):
            self.import_success = False
            self.import_end_time = datetime.now()
            status = result["status"]
            self.import_audit.update(restore_status=status, original_unchanged=False,
                                     cutover_pending=False if status == "promoted_incomplete" else None)
            self.import_audit["missing_programs"] = result.get("missing_programs") or []
            sentence = ("테이블은 교체했지만 저장 프로그램이 빠져 있습니다. 백업을 보존하고 복구하세요."
                        if status == "promoted_incomplete" else
                        "테이블은 교체하지 않았지만 원래 트리거를 되살리지 못했습니다. 보고서에 저장된 정의로 복구하세요.")
            self.label_status.setText(translate_text(sentence))
            names = ", ".join(display_program_name(item) for item in result.get("missing_programs") or [])
            program_lines, details = describe_program_results(result)
            if not names:
                names = ", ".join(str(item.get("name") or "") for item in result.get("programs_failed") or [] if isinstance(item, dict))
            for line in program_lines:
                self._log_visible(line)
            for detail in details:
                self._log_visible(detail)
            self._show_failure(translate_text("저장 프로그램 복원이 끝나지 않았습니다"), "\n".join([translate_text(sentence), names] + program_lines), "\n".join([sanitize_local_diagnostic(message)] + details))
        elif action == "confirm":
            self.import_success = False
            self.import_end_time = datetime.now()
            known_unchanged = result.get("original_unchanged") is True
            self.import_audit["restore_status"] = "promotion_failed_original_unchanged" if known_unchanged else "promotion_outcome_unknown"
            self.import_audit["original_unchanged"] = True if known_unchanged else None
            self.import_audit["cutover_pending"] = True if known_unchanged else None
            self._log_visible(message or "Promotion result could not be verified")
            cause = describe_core_failure(message)
            if self._cancel_requested:
                self.label_status.setText(translate_text(
                    "⏹ 교체 중지됨 · 원본은 그대로입니다" if known_unchanged
                    else "⏹ 교체 중지됨 · 원본·백업·복사본 상태를 Import 보고서에서 확인하세요"))
            elif known_unchanged:
                self._stop_and_explain(["- " + (cause or translate_text(_PLAN_BLOCKED_REASON))])
            else:
                self.label_status.setText(translate_text("교체 결과를 확인하지 못했습니다 · 원본·백업·복사본 상태를 확인하세요"))
            if not known_unchanged:  # stopped or not: what the swap did is unknown, so it is never only a status line
                self._show_failure(translate_text("교체 결과를 확인하지 못했습니다"), "\n\n".join(filter(None, [
                    translate_text("교체가 끝났는지 확인하지 못했습니다. 원본·백업·복사본 상태를 Import 보고서와 DB에서 확인하세요."),
                    cause])), message)
        if self._close_after_cancel:
            QTimer.singleShot(0, self.close)

    def _plan_is_clean(self, plan: dict) -> bool:
        """Swap without asking again only a verified copy whose plan the core can carry out as it stands."""
        clean = (self.import_audit.get("restore_status") == "ready_for_switch"
                 and plan.get("can_promote") is True and bool(plan.get("plan_digest"))
                 and not plan.get("blockers") and not plan.get("copy_blockers"))
        if not clean:
            self._add_log("자동 교체 불가: 교체 계획에 막는 조건이 있습니다.")
        return clean

    def _remember_promotion_plan(self, plan: dict):
        safe_plan = _sanitized_rust_event(plan)
        self.import_audit["promotion_plan"] = {key: safe_plan.get(key) for key in (
            "plan_digest", "can_promote", "original_target", "candidate_target", "backup_namespace", "summary", "blockers")}
        # The target changed since the copy was made: the same reasons a stop before the copy names.
        stops = describe_copy_blockers(safe_plan.get("copy_blockers"), unknown=_PLAN_BLOCKED_UNKNOWN)
        if stops:
            self.import_audit["promotion_plan"]["blockers"] = stops

    def save_log(self):
        """로그를 파일로 저장"""
        if not self.log_entries:
            QMessageBox.warning(self, "로그 없음", "저장할 로그가 없습니다.")
            return

        # 기본 파일명 생성
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if self.import_success is None:
            status = "running"
        else:
            status = "success" if self.import_success else "failed"

        # import "대상" 스키마명으로 파일명을 만든다.
        # 소스 덤프 폴더명의 첫 단어(예: "PROD_admin_example" → "PROD")를 쓰면 소스가
        # 대상인 것처럼 오해된다(import은 "어디에 넣었는지"가 핵심). 그래서:
        #   - target_schema 지정 시 → 그 스키마명
        #   - "원본 스키마명 사용" 시 → 덤프의 원본(=실제 대상) 스키마명
        schema_name = self.last_import_context.get("namespace") or "unknown"
        if self.last_import_mode == "safe":
            candidate = self.import_audit.get("candidate_target") or {}
            schema_name = (candidate.get("schema") if candidate.get("engine") == "postgresql"
                           else candidate.get("database")) or schema_name
        if not self.last_import_context and self.last_target_schema:
            schema_name = self.last_target_schema
        schema_name = safe_filename_component(sanitize_local_diagnostic(schema_name), "unknown")
        default_filename = f"import_log_{schema_name}_{status}_{timestamp}.txt"

        # 기본 저장 경로
        default_dir = os.path.dirname(self.last_input_dir) if self.last_input_dir else os.path.expanduser("~")

        # 파일 저장 대화상자
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "로그 파일 저장",
            os.path.join(default_dir, default_filename),
            "텍스트 파일 (*.txt);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            safe = sanitize_local_diagnostic
            # 결과 요약
            table_results = self._table_results()
            done_count = self._count_by_status(table_results, 'done')
            error_count = self._count_by_status(table_results, 'error')
            blocked_count = self._count_by_status(table_results, 'blocked')
            total_count = len(table_results)

            with open(file_path, 'w', encoding='utf-8') as f:
                # 헤더 정보
                f.write("=" * 70 + "\n")
                f.write("Rust DB Core Import Log\n")
                f.write("=" * 70 + "\n\n")

                f.write(f"덤프 폴더: {safe(self.last_input_dir)}\n")
                target_schema = (
                    safe(self.last_import_context.get("namespace") or self.last_target_schema or "unknown")
                )
                f.write(f"넣을 곳: {target_schema}\n")
                context = self.last_import_context
                f.write(f"Target engine: {safe(context.get('engine') or 'unknown')}\n")
                f.write(f"Effective endpoint: {safe(context.get('host') or 'unknown')}:{safe(context.get('port') or 'unknown')}\n")
                profile = context.get("profile") or {}
                if profile:
                    f.write(f"Connection profile: {safe(profile.get('name') or 'unknown')}\n")
                    f.write(f"Environment: {safe(profile.get('environment') or 'unknown')}\n")
                    f.write(f"Connection mode: {safe(profile.get('connection_mode') or 'unknown')}\n")
                    f.write(f"Configured remote DB: {safe(profile.get('remote_host') or 'unknown')}:{safe(profile.get('remote_port') or 'unknown')}\n")
                    if profile.get('bastion_host') and profile.get('connection_mode') != 'direct':
                        f.write(f"Bastion host: {safe(profile['bastion_host'])}\n")
                f.write(f"Target database: {safe(context.get('database') or 'unknown')}\n")
                f.write(f"Target schema: {safe(context.get('schema') or ('not applicable' if context.get('engine') == 'mysql' else 'unknown'))}\n")
                f.write(f"Last phase: {safe(self.import_audit.get('phase') or 'unknown')}\n")
                f.write(f"Destructive phase: {safe(self.import_audit.get('destructive_phase') or 'unknown')}\n")
                drops = self.import_audit.get('confirmed_drops')
                f.write(f"Confirmed completed DROP commands: {len(drops) if drops is not None else 'unknown (no events recorded)'}\n")
                for table in sorted(drops or []):
                    f.write(f"  DROP completed: {safe(table)}\n")
                f.write(f"Core report status: {safe(self.import_audit.get('status') or 'unknown')}\n")
                f.write(f"Core report path: {safe(self.import_audit.get('report_path') or 'unknown')}\n")
                if self.import_audit.get("ui_cancel_requested"):
                    f.write(f"UI cancellation requested: {safe(self.import_audit.get('ui_cancel_stage') or 'unknown')}\n")
                if self.last_import_mode == "safe":
                    for key in ("original_target", "candidate_target"):
                        f.write(f"{key}: {safe(json.dumps(self.import_audit.get(key) or {}, ensure_ascii=False))}\n")
                    f.write(f"Safe restore status: {safe(self.import_audit.get('restore_status') or 'unknown')}\n")
                    original_unchanged = self.import_audit.get('original_unchanged')
                    cutover_pending = self.import_audit.get('cutover_pending')
                    f.write(f"Original unchanged: {original_unchanged if isinstance(original_unchanged, bool) else 'unknown'}\n")
                    f.write(f"Cutover pending: {cutover_pending if isinstance(cutover_pending, bool) else 'unknown'}\n")
                    f.write(f"Switch review blockers: {safe(json.dumps(self.import_audit.get('blockers') or [], ensure_ascii=False))}\n")
                    f.write(f"Promotion plan: {safe(json.dumps(self.import_audit.get('promotion_plan') or {}, ensure_ascii=False))}\n")
                    f.write(f"Backup namespace: {safe(self.import_audit.get('backup_namespace') or 'unknown')}\n")
                    f.write(f"Operator choice: {safe(self.import_audit.get('operator_choice') or 'pending')}\n")
                    f.write(f"Promotion result: {safe(json.dumps(self.import_audit.get('promotion_result') or {}, ensure_ascii=False))}\n")
                f.write(f"실행 방식: {safe(_mode_text(self.last_import_mode))}\n")
                if self.import_success is None:
                    result_label = "진행 중"
                else:
                    result_label = "성공 ✅" if self.import_success else "실패 ❌"
                f.write(f"결과: {result_label}\n")
                f.write(f"테이블 통계: 성공 {done_count}개, 실패 {error_count}개, 총 {total_count}개\n")
                f.write(f"미실행: {blocked_count}개 테이블\n")
                if self.import_success is not True:
                    f.write("전체 Import 검증 및 후처리가 완료되지 않았습니다. 완료 표시는 테이블 데이터 적재 상태입니다.\n")
                for table_name, result in table_results.items():
                    if result.get('status') == 'blocked':
                        f.write(f"  ⏸ {safe(table_name)}: not started\n")

                if self.import_start_time:
                    f.write(f"시작 시간: {self.import_start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                if self.import_end_time:
                    f.write(f"종료 시간: {self.import_end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                if self.import_start_time and self.import_end_time:
                    elapsed = self.import_end_time - self.import_start_time
                    f.write(f"소요 시간: {elapsed}\n")

                # 실패한 테이블 목록
                if error_count > 0:
                    f.write("\n" + "-" * 70 + "\n")
                    f.write("실패한 테이블 목록\n")
                    f.write("-" * 70 + "\n")
                    for table_name, result in table_results.items():
                        if result.get('status') == 'error':
                            f.write(
                                f"  ❌ {safe(table_name)}: "
                                f"{safe(result.get('message', 'Unknown error'))}\n"
                            )

                f.write("\n" + "=" * 70 + "\n")
                f.write("상세 로그\n")
                f.write("=" * 70 + "\n\n")

                for entry in self.log_entries:
                    f.write(safe(entry) + "\n")

            QMessageBox.information(
                self, "저장 완료",
                f"✅ 로그가 저장되었습니다.\n\n{file_path}"
            )
        except Exception as e:
            QMessageBox.critical(
                self, "저장 실패",
                f"❌ 로그 저장 중 오류가 발생했습니다.\n\n{str(e)}"
            )

    def request_stop(self, close_after: bool = False) -> bool:
        """실행 중인 Import/전환에 중지를 요청한다.

        close_after=False(중지 버튼)면 창은 그대로 두고, 워커가 끝나면 최종 상태와 보고서 링크를 보여 준다.
        """
        if not (self.worker and self.worker.isRunning()):
            return False
        if self._cancel_requested:  # 이미 중지 요청됨: 닫기만 추가로 예약할 수 있다
            self._close_after_cancel = self._close_after_cancel or close_after
            return True
        stage = getattr(self, "_promotion_action", "")
        # The check before a run and the check before a swap only read the target: they stop without asking.
        if getattr(self, "_previewing", False) or stage == "plan":
            self._cancel_requested = True
            self._close_after_cancel = close_after
            self.worker.cancel()
            return True
        cancel_text = (
            "교체하는 중입니다. 지금 중지하면 교체가 끝났는지 알 수 없게 될 수 있습니다. "
            "원본·백업·복사본 상태를 Import 보고서와 DB에서 확인해야 합니다. 중지하시겠습니까?"
            if stage == "confirm" else
            "복사본에 넣는 중입니다. 지금 중지해도 원본은 그대로입니다. 중지하시겠습니까?"
            if getattr(self, "last_import_mode", "") == "safe" else
            "Import가 실행 중입니다.\n지금 중지하면 이미 덮어쓴 테이블은 되돌리지 않고, 작업 중이던 테이블은 비어 있을 수 있습니다.\n\n"
            "중지하시겠습니까?"
        )
        reply = QMessageBox.question(
            self,
            translate_text("Import 실행 중"),
            translate_text(cancel_text),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False
        self._cancel_requested = True
        self._close_after_cancel = close_after
        self.import_audit = getattr(self, "import_audit", {})
        self.import_audit["ui_cancel_requested"] = True
        self.import_audit["ui_cancel_stage"] = (
            "promotion_confirm" if stage == "confirm"
            else "safe_restore" if getattr(self, "last_import_mode", "") == "safe" else "import"
        )
        self._add_log("Import 중지 요청")
        self.worker.cancel()
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("중지 중…")
        self.label_status.setText(translate_text("⏹ 중지 중…"))
        return True

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.btn_save_log.setEnabled(True)
            self.request_stop(close_after=True)
            event.ignore()
            return
        if self.connector:
            self.connector.disconnect()
        self._close_after_cancel = True
        event.accept()
