"""
Rust DB Core 기반 Import 다이얼로그
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QSpinBox, QPushButton, QComboBox,
    QCheckBox, QListWidget, QListWidgetItem, QGroupBox,
    QFileDialog, QMessageBox, QProgressBar, QApplication,
    QRadioButton, QButtonGroup, QWidget, QAbstractItemView,
    QSplitter, QScrollArea
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from typing import List, Optional
from datetime import datetime
import json
import os

from src.core.connection_trust import apply_registered_tls
from src.core.constants import MAX_LOG_ENTRIES, MAX_VISIBLE_LOG_LINES, TABLE_STATUS_ICONS
from src.core.db_connector import MySQLConnector
from src.core.error_report_sanitizer import (
    sanitize_local_diagnostic,
    sanitize_local_diagnostic_data,
)
from src.core.i18n import translate_text
from src.core.logger import get_logger
from src.core.path_safety import safe_filename_component
from src.exporters.rust_dump_exporter import (
    build_rust_dump_config, check_rust_dump, dump_original_namespace, restore_target_connection_info
)
from src.ui.dialogs.collapsible_config_dialog import CollapsibleConfigDialog
from src.ui.workers.error_reporting_worker import ErrorReportingMixin
from src.ui.workers.rust_dump_worker import RustDumpWorker
from src.core.migration_analyzer import DumpFileAnalyzer, CompatibilityIssue

logger = get_logger('db_dialogs')

def _escape_local_diagnostic_text(value: object) -> str:
    return sanitize_local_diagnostic(value)


def _structured_local_diagnostic_text(value: object) -> str:
    try:
        serialized = json.dumps(
            sanitize_local_diagnostic_data(value),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except BaseException:
        serialized = "REDACTED"
    return sanitize_local_diagnostic(serialized)


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


    if str(event.get("event", "")).startswith("safe_restore") or event.get("candidate_target"):
        for key in ("original_target", "candidate_target"):
            if isinstance(event.get(key), dict):
                audit[key] = restore_target_connection_info(event[key])
        for key in ("verified", "original_unchanged", "cutover_pending", "ready_for_switch", "namespace_existed", "namespace_created"):
            if isinstance(event.get(key), bool):
                audit[key] = event[key]
        if isinstance(event.get("status"), str):
            audit["restore_status"] = event["status"]
        if isinstance(event.get("blockers"), list):
            audit["blockers"] = event["blockers"]
        for key in ("restore_id", "report_path", "plan_digest"):
            if isinstance(event.get(key), str):
                audit[key] = event[key]


def _promotion_review_text(plan: dict, profile: dict) -> str:
    def endpoint_text(value):
        endpoint = restore_target_connection_info(value)
        namespace = str(endpoint.get("database") or "?")
        if endpoint.get("engine") == "postgresql":
            namespace += "/" + str(endpoint.get("schema") or "?")
        return _escape_local_diagnostic_text(f"{endpoint.get('engine', '?')} {endpoint.get('host', '?')}:{endpoint.get('port', '?')} / {namespace}")

    lines = [translate_text("기존 대상") + ": " + endpoint_text(plan.get("original_target")),
             translate_text("새 복원 대상") + ": " + endpoint_text(plan.get("candidate_target")),
             translate_text("이전 데이터 백업 이름") + ": " + _escape_local_diagnostic_text(plan.get("backup_namespace") or "?")]
    if profile:
        lines.append(translate_text("연결 프로필") + ": " + _escape_local_diagnostic_text(f"{profile.get('name', '?')} / {profile.get('environment', '?')} / {profile.get('remote_host', '?')}:{profile.get('remote_port', '?')}"))
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    for key, label in (("selected_tables", "교체할 테이블"), ("new_tables", "새 테이블"),
                       ("target_only_tables_preserved", "기존 대상에만 있는 테이블 (유지)"),
                       ("schema_changed_tables", "구조가 다른 테이블"), ("data_changed_tables", "데이터 차이가 확인된 테이블"),
                       ("data_not_compared", "행 내용을 비교하지 않은 테이블"),
                       ("views_replaced", "교체할 뷰")):
        values = summary.get(key)
        if isinstance(values, list):
            preview = ", ".join(_escape_local_diagnostic_text(value) for value in values[:6]) or "-"
            if len(values) > 6:
                preview += f" … (+{len(values) - 6})"
            lines.append(f"{translate_text(label)} ({len(values)}): {preview}")
    if summary.get("data_not_compared"):
        lines.append(translate_text("행 수만으로 데이터 일치를 보장하지 않습니다."))
    if summary.get("write_pause"):
        lines.append(translate_text("전환 중 서비스 영향") + ": " + _escape_local_diagnostic_text(summary["write_pause"]))
    if isinstance(summary.get("row_counts"), dict):
        lines.append(translate_text("행 수 비교 (기존 / 새 대상)"))
        for table, counts in list(summary["row_counts"].items())[:6]:
            if isinstance(counts, dict):
                old, new = counts.get("original"), counts.get("candidate")
                old = old if isinstance(old, int) else translate_text("미확인")
                new = new if isinstance(new, int) else translate_text("미확인")
                lines.append(f"  {_escape_local_diagnostic_text(table)}: {old} / {new}")
    for key, label in (("warnings", "주의 사항"), ("blockers", "전환 차단 사유")):
        items = plan.get(key) or summary.get(key) or []
        if items:
            lines.append(translate_text(label) + ":\n" + "\n".join(_escape_local_diagnostic_text(item) for item in items))
    lines.append(translate_text("전체 목록과 비교 내용은 상세 정보를 펼쳐 확인하세요."))
    return "\n".join(lines)


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
            "🔄 현재 단계: 최종 스키마 전환 중 · 데이터 Import 완료, 후처리 진행 중",
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

    def __init__(self, parent=None, connector: MySQLConnector = None, config_manager=None,
                 tunnel_config: dict = None):
        super().__init__(parent)
        self.setWindowTitle("Rust DB Core Import (병렬 처리)")
        self.resize(600, 700)

        self.connector = connector
        self.config_manager = config_manager
        self.tunnel_config = tunnel_config  # Production 환경 보호용
        self.worker: Optional[RustDumpWorker] = None

        # 익명 오류 보고 워커 목록 (완료 전까지 참조 유지)
        self._error_report_workers: List[object] = []
        self._cancel_requested = False
        self._close_after_cancel = False

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
        self._load_default_input_dir()
        self.load_schemas()

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
        container_layout.addWidget(self._build_schema_group())
        container_layout.addWidget(self._build_import_options_group())
        container_layout.addWidget(self._build_timezone_group())
        container_layout.addWidget(self._build_import_mode_group())

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
        status_group = QGroupBox("Rust DB Core 상태")
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
        input_group = QGroupBox("Dump 폴더")
        input_layout = QHBoxLayout(input_group)

        self.input_dir = QLineEdit()
        self.input_dir.setPlaceholderText("rust_dump dump 폴더 선택...")
        self.input_dir.editingFinished.connect(self._run_upgrade_check_for_current_input_dir)

        btn_browse = QPushButton("선택")
        btn_browse.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_browse.clicked.connect(self.browse_input_dir)

        input_layout.addWidget(self.input_dir)
        input_layout.addWidget(btn_browse)
        return input_group

    def _build_upgrade_check_group(self):
        self.upgrade_check_group = QGroupBox("MySQL 8.4 호환성 검사")
        upgrade_check_layout = QVBoxLayout(self.upgrade_check_group)

        status_line = QHBoxLayout()
        self.lbl_upgrade_status = QLabel("📋 Dump 폴더를 선택하면 자동 검사됩니다.")
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

    def _build_schema_group(self):
        schema_group = QGroupBox("대상 스키마")
        schema_layout = QVBoxLayout(schema_group)

        self.chk_use_original = QCheckBox("원본 스키마명 사용")
        self.chk_use_original.setChecked(True)
        self.chk_use_original.toggled.connect(self.on_schema_option_changed)
        schema_layout.addWidget(self.chk_use_original)

        target_layout = QHBoxLayout()
        target_layout.addWidget(QLabel("대상 스키마:"))
        self.combo_target_schema = QComboBox()
        self.combo_target_schema.setEditable(True)
        self.combo_target_schema.setToolTip("기존 대상을 선택하거나 새 이름을 입력하세요. 안전 복원은 대상이 없으면 입력한 이름으로 새 대상을 만듭니다.")
        self.combo_target_schema.setMinimumWidth(200)
        self.combo_target_schema.setEnabled(False)
        target_layout.addWidget(self.combo_target_schema)
        target_layout.addStretch()
        schema_layout.addLayout(target_layout)

        return schema_group

    def _build_import_options_group(self):
        option_group = QGroupBox("Import 옵션")
        option_layout = QFormLayout(option_group)

        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(8)
        option_layout.addRow("병렬 스레드:", self.spin_threads)

        return option_group

    def _build_timezone_group(self):
        tz_group = QGroupBox("타임존 설정")
        tz_layout = QVBoxLayout(tz_group)

        self.btn_tz_group = QButtonGroup(self)

        # 1. 자동 감지 (권장)
        self.radio_tz_auto = QRadioButton("Dump 타임존 사용 (권장)")
        self.radio_tz_auto.setChecked(True)
        self.radio_tz_auto.setToolTip("Dump에 기록된 타임존을 사용합니다. 기록이 없는 경우 서버 기본값을 유지합니다.")

        # 2. 강제 KST
        self.radio_tz_kst = QRadioButton("강제 KST (+09:00)")

        # 3. 강제 UTC
        self.radio_tz_utc = QRadioButton("강제 UTC (+00:00)")

        # 4. 설정 안 함
        self.radio_tz_none = QRadioButton("설정 안 함 (서버 기본값)")

        self.btn_tz_group.addButton(self.radio_tz_auto)
        self.btn_tz_group.addButton(self.radio_tz_kst)
        self.btn_tz_group.addButton(self.radio_tz_utc)
        self.btn_tz_group.addButton(self.radio_tz_none)

        tz_layout.addWidget(self.radio_tz_auto)
        tz_layout.addWidget(self.radio_tz_kst)
        tz_layout.addWidget(self.radio_tz_utc)
        tz_layout.addWidget(self.radio_tz_none)

        return tz_group

    def _build_import_mode_group(self):
        mode_group = QGroupBox("Import 모드 선택")
        mode_layout = QVBoxLayout(mode_group)

        self.btn_import_mode = QButtonGroup(self)

        self.radio_safe = QRadioButton("안전 복원: 새 대상에 복원·검증")
        self.radio_safe.setChecked(True)
        safe_description = QLabel("   기존 대상은 변경하지 않고 새 데이터베이스/스키마에 복원합니다.\n   검증 후 연결 정보를 제공하며, 실제 연결 전환은 별도로 결정합니다.")
        safe_description.setWordWrap(True)
        mode_layout.addWidget(self.radio_safe)
        mode_layout.addWidget(safe_description)
        self.btn_import_mode.addButton(self.radio_safe)

        # 1. 증분 Import (병합)
        mode_merge_layout = QVBoxLayout()
        self.radio_merge = QRadioButton("데이터 추가 Import")
        mode_merge_desc = QLabel("   기존 행을 유지하고 Dump의 행을 추가합니다.\n   ⚠️ 변경분 동기화가 아니며 중복 또는 제약 조건 오류가 발생할 수 있습니다.")
        mode_merge_desc.setStyleSheet("color: #7f8c8d; font-size: 10pt; margin-left: 20px;")
        mode_merge_layout.addWidget(self.radio_merge)
        mode_merge_layout.addWidget(mode_merge_desc)
        mode_layout.addLayout(mode_merge_layout)

        # 2. Replace selected tables; failure cannot roll back the whole restore.
        mode_replace_layout = QVBoxLayout()
        self.radio_replace = QRadioButton("고급: 전체 교체 Import (선택 테이블 삭제 후 재생성)")
        self.radio_replace.setToolTip("선택한 테이블의 기존 구조와 데이터를 삭제합니다. 실패해도 자동으로 되돌리지 않습니다.")
        mode_replace_desc = QLabel(
            "   선택한 테이블을 삭제한 후 Dump의 구조와 데이터로 다시 만듭니다.\n"
            "   ⚠️ 기존 데이터가 손실되며, 실패 시 일부만 복원될 수 있습니다.\n"
            "   자동으로 되돌리지 않습니다. 실행 전 대상 백업을 확인하세요."
        )
        mode_replace_desc.setStyleSheet("color: #b45309; font-size: 10pt; margin-left: 20px;")
        mode_replace_layout.addWidget(self.radio_replace)
        mode_replace_layout.addWidget(mode_replace_desc)
        mode_layout.addLayout(mode_replace_layout)

        # 3. Retain the recreate protocol option as an explicit legacy alias.
        mode_recreate_layout = QVBoxLayout()
        self.radio_recreate = QRadioButton("고급: 완전 재생성 Import (이전 모드 · 교체와 동일)")
        self.radio_recreate.setToolTip("전체 교체 Import와 동일하게 선택한 테이블만 삭제 후 재생성합니다. 데이터베이스 전체를 삭제하는 모드가 아닙니다.")
        mode_recreate_desc = QLabel("   전체 교체와 동일한 동작입니다. 기존 설정과의 호환성을 위해 유지됩니다.\n   같은 데이터 손실 및 부분 복원 위험이 적용됩니다.")
        mode_recreate_desc.setStyleSheet("color: #b45309; font-size: 10pt; margin-left: 20px;")
        mode_recreate_layout.addWidget(self.radio_recreate)
        mode_recreate_layout.addWidget(mode_recreate_desc)
        mode_layout.addLayout(mode_recreate_layout)

        self.btn_import_mode.addButton(self.radio_merge)
        self.btn_import_mode.addButton(self.radio_replace)
        self.btn_import_mode.addButton(self.radio_recreate)

        return mode_group

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

        progress_main_layout.addWidget(self.progress_group)

        self.table_status_group = QGroupBox("테이블 Import 상태")
        self.table_status_group.setVisible(False)
        table_status_layout = QVBoxLayout(self.table_status_group)

        self.table_list = QListWidget()
        self.table_list.setMinimumHeight(150)
        self.table_list.setMaximumHeight(200)
        self.table_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
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
        self.btn_retry = QPushButton("🔄 선택한 테이블 재시도")
        self.btn_retry.setVisible(False)
        self.btn_retry.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        self.btn_retry.clicked.connect(self.do_retry)

        self.btn_select_failed = QPushButton("실패한 테이블 모두 선택")
        self.btn_select_failed.setVisible(False)
        self.btn_select_failed.setStyleSheet("""
            QPushButton {
                background-color: #95a5a6; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #7f8c8d; }
        """)
        self.btn_select_failed.clicked.connect(self.select_failed_tables)

        retry_layout.addWidget(self.btn_select_failed)
        retry_layout.addWidget(self.btn_retry)
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

        self.btn_copy_restore_target = QPushButton("복원 대상 연결 정보 복사")
        self.btn_copy_restore_target.setEnabled(False)
        self.btn_copy_restore_target.clicked.connect(self.copy_restore_target)
        self.btn_review_restore = QPushButton("기존 대상과 비교 / 전환 검토")
        self.btn_review_restore.setEnabled(False)
        self.btn_review_restore.clicked.connect(self.review_restore_target)

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
        button_layout.addWidget(self.btn_copy_restore_target)
        button_layout.addWidget(self.btn_review_restore)
        button_layout.addStretch()
        button_layout.addWidget(self.btn_import)
        button_layout.addWidget(btn_cancel)
        return button_layout

    def load_schemas(self):
        self.combo_target_schema.clear()
        if not self.connector:
            return
        schemas = self.connector.get_schemas()
        for schema in schemas:
            self.combo_target_schema.addItem(schema)

    def on_schema_option_changed(self):
        use_original = self.chk_use_original.isChecked()
        self.combo_target_schema.setEnabled(not use_original)

    def browse_input_dir(self):
        start_dir = self._get_input_browse_start_dir()
        folder = QFileDialog.getExistingDirectory(
            self, "Dump 폴더 선택", start_dir
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
            self.lbl_upgrade_status.setText("📋 Dump 폴더를 선택하면 자동 검사됩니다.")
            self.lbl_upgrade_status.setStyleSheet("color: #7f8c8d;")

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
        self._update_original_schema_option(dump_path)
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
        self.input_dir.setEnabled(enabled)
        self.chk_use_original.setEnabled(enabled)
        self.combo_target_schema.setEnabled(enabled and not self.chk_use_original.isChecked())
        self.spin_threads.setEnabled(enabled)
        self.radio_merge.setEnabled(enabled)
        self.radio_safe.setEnabled(enabled)
        self.radio_replace.setEnabled(enabled)
        self.radio_recreate.setEnabled(enabled)
        self.radio_tz_auto.setEnabled(enabled)
        self.radio_tz_kst.setEnabled(enabled)
        self.radio_tz_utc.setEnabled(enabled)
        self.radio_tz_none.setEnabled(enabled)
        self.btn_import.setEnabled(enabled)
        if enabled and self._is_valid_dump_dir(self.input_dir.text()):
            self._update_original_schema_option(self.input_dir.text())

    def check_timezone_support(self) -> bool:
        """
        서버가 'Asia/Seoul' 같은 지역명 타임존을 지원하는지 확인
        """
        if not self.connector:
            return False

        try:
            # mysql.time_zone_name 테이블에서 Asia/Seoul 조회
            # 단순히 테이블 존재 여부만 보지 않고 실제 데이터가 있는지 확인
            query = "SELECT 1 FROM mysql.time_zone_name WHERE Name = 'Asia/Seoul' LIMIT 1"
            rows = self.connector.execute(query)
            return len(rows) > 0
        except Exception:
            logger.debug("Timezone support check failed", exc_info=True)
            return False

    def _add_log(self, msg: str):
        """로그 항목 추가 (수집용, 최대 500개 유지)"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = f"[{timestamp}] {_escape_local_diagnostic_text(msg)}"
        self.log_entries.append(log_entry)
        if len(self.log_entries) > MAX_LOG_ENTRIES:
            del self.log_entries[:-MAX_LOG_ENTRIES]

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

    def _update_original_schema_option(self, dump_dir: str):
        known = bool(self._get_dump_schema_name(dump_dir))
        self.chk_use_original.setEnabled(known)
        if known:
            self.chk_use_original.setToolTip("")
        if not known:
            self.chk_use_original.setChecked(False)
            self.combo_target_schema.setEnabled(True)
            self.chk_use_original.setToolTip("원본 스키마가 기록되지 않았거나 다른 DB 엔진입니다. 대상 데이터베이스/스키마를 직접 선택하세요.")

    def _get_selected_import_mode(self) -> str:
        if self.radio_safe.isChecked():
            return "safe"
        if self.radio_replace.isChecked():
            return "replace"
        if self.radio_recreate.isChecked():
            return "recreate"
        return "merge"

    def _get_import_mode_text(self, import_mode: Optional[str] = None) -> str:
        """선택했거나 실제 실행한 Import 모드의 표시 텍스트 반환"""
        mode = import_mode or self._get_selected_import_mode()
        return {
            "safe": "안전 복원: 새 대상에 복원·검증",
            "merge": "데이터 추가 Import",
            "replace": "전체 교체 Import",
            "recreate": "완전 재생성 Import",
        }.get(mode, "확인 불가")

    def _confirm_production_guard(self, input_dir: str, target_schema: Optional[str]) -> bool:
        from src.core.production_guard import ProductionGuard

        guard = ProductionGuard(self)
        if target_schema:
            schema_name = target_schema
        else:
            schema_name = self._get_dump_schema_name(input_dir) or "(원본 스키마)"
        details = (
            f"Dump 폴더: {input_dir}<br>"
            f"Import 모드: {self._get_import_mode_text()}"
        )
        return guard.confirm_dangerous_operation(
            self.tunnel_config or {}, "데이터 Import", schema_name, details
        )

    def do_import(self, retry_tables: list = None):
        """Import 실행 (retry_tables가 주어지면 해당 테이블만 재시도)"""
        self._cancel_requested = False
        self._close_after_cancel = False
        self._promotion_action = ""
        if retry_tables:
            self.do_retry()
            return
        input_dir = self.input_dir.text()

        if not input_dir:
            QMessageBox.warning(self, "오류", "Dump 폴더를 선택하세요.")
            return

        if not os.path.exists(input_dir):
            QMessageBox.warning(self, "오류", "폴더가 존재하지 않습니다.")
            return

        target_schema = None
        if self.chk_use_original.isChecked() and os.path.isfile(os.path.join(input_dir, '_tunnelforge_dump.json')) and not self._get_dump_schema_name(input_dir):
            self._update_original_schema_option(input_dir)
            QMessageBox.warning(self, "대상 선택 필요", "원본 스키마를 확정할 수 없습니다. 대상 데이터베이스/스키마를 직접 선택하세요.")
            return
        if not self.chk_use_original.isChecked():
            target_schema = self.combo_target_schema.currentText()
            if not target_schema:
                QMessageBox.warning(self, "오류", "대상 스키마를 선택하세요.")
                return

        if not self._confirm_production_guard(input_dir, target_schema):
            return
        if self._get_selected_import_mode() in ("replace", "recreate"):
            reply = QMessageBox.question(
                self, "기존 대상 삭제 확인",
                "선택한 기존 테이블과 데이터를 삭제하고 다시 만듭니다. 실패해도 자동으로 되돌리지 않습니다. 안전 복원 대신 기존 대상을 직접 변경하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._begin_error_report_operation()

        # 저장 (재시도용)
        self.last_input_dir = input_dir
        self.last_target_schema = target_schema
        import_mode = self._get_selected_import_mode()
        self.last_import_mode = import_mode
        if self.config_manager:
            self.config_manager.set_app_setting('rust_dump_import_dir', input_dir)
            self.config_manager.set_app_setting('rust_dump_last_dump_dir', input_dir)

        # UI 상태 변경 - 모든 입력 비활성화
        self.set_ui_enabled(False)
        self.btn_retry.setVisible(False)
        self.btn_select_failed.setVisible(False)
        self.btn_save_log.setEnabled(False)

        # 설정 섹션 접기
        self.collapse_config_section()

        # 로그 및 진행 상황 UI 표시
        self.progress_group.setVisible(True)
        self.table_status_group.setVisible(True)
        self.log_group.setVisible(True)

        # 재시도가 아닌 경우 초기화
        if not retry_tables:
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
            self.last_import_context = {}
            self.btn_copy_restore_target.setEnabled(False)
            self.btn_review_restore.setEnabled(False)

            # 로그 헤더 추가
            self._add_log(f"{'='*60}")
            self._add_log("Rust DB Core Import 시작")
            self._add_log(f"시작 시간: {self.import_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            self._add_log(f"Dump 폴더: {input_dir}")
            self._add_log(f"대상 스키마: {target_schema if target_schema else '원본 스키마명 사용'}")
            self._add_log(f"Import 모드: {self._get_import_mode_text(import_mode)}")
            self._add_log(f"병렬 스레드: {self.spin_threads.value()}")
            self._add_log(f"{'='*60}")

        # 프로그레스 바 초기화
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(100)
        self.label_percent.setText("📊 진행률: 0%")
        self.label_data.setText("📦 데이터: 0 MB / 0 MB")
        self.label_speed.setText("⚡ 속도: 0 rows/s")
        self.label_tables.setText("📋 테이블: 0 / 0 완료")
        self.label_status.setText("Import 준비 중...")

        config = build_rust_dump_config(self.connector)
        self.restore_config = config
        namespace = target_schema or self._get_dump_schema_name(input_dir)
        self.last_import_context = {
            "engine": config.engine,
            "host": config.host,
            "port": config.port,
            "database": (config.database or "postgres") if config.engine == "postgresql" else namespace,
            "schema": namespace if config.engine == "postgresql" else "",
            "namespace": namespace,
        }
        profile = self.tunnel_config if isinstance(self.tunnel_config, dict) else {}
        self.last_import_context["profile"] = {
            key: profile[key]
            for key in ("name", "environment", "connection_mode", "remote_host", "remote_port", "bastion_host")
            if isinstance(profile.get(key), (str, int))
        }

        # 타임존 설정 결정
        db_engine = config.engine
        timezone_sql = None

        if self.radio_tz_auto.isChecked():
            self.txt_log.addItem("ℹ️ Dump 타임존을 사용합니다. 기록이 없으면 서버 기본값을 유지합니다.")

        elif self.radio_tz_kst.isChecked():
            timezone_sql = resolve_timezone_sql(db_engine, "kst")
            self.txt_log.addItem("ℹ️ 타임존을 강제로 '+09:00' (KST)로 설정합니다.")

        elif self.radio_tz_utc.isChecked():
            timezone_sql = resolve_timezone_sql(db_engine, "utc")
            self.txt_log.addItem("ℹ️ 타임존을 강제로 '+00:00' (UTC)로 설정합니다.")

        # 재시도 시 모드 표시
        if retry_tables:
            self.txt_log.addItem(f"🔄 재시도 모드: {len(retry_tables)}개 테이블")

        # 작업 스레드 시작
        self.worker = RustDumpWorker(
            "import", config,
            input_dir=input_dir,
            target_schema=namespace or target_schema,
            threads=self.spin_threads.value(),
            import_mode=import_mode,
            timezone_sql=timezone_sql,
            use_source_timezone=not self.radio_tz_none.isChecked(),
            retry_tables=retry_tables
        )

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
        msg = _escape_local_diagnostic_text(msg)
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
        display_table = _escape_local_diagnostic_text(table_name)
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
                display_info[key] = _escape_local_diagnostic_text(
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
        display_table = _escape_local_diagnostic_text(table_name)
        display_message = _escape_local_diagnostic_text(message)

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

            display_table = _escape_local_diagnostic_text(table_name)
            display_text = f"{icon} {display_table}{size_info}{chunk_info}"

            # error 상태이면 메시지 추가
            if current_status == 'error':
                message = self.import_results.get(table_name, {}).get('message', '')
                if message:
                    display_message = _escape_local_diagnostic_text(message)
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
                sanitized_event = _sanitized_rust_event(event)
                self.import_audit = getattr(self, "import_audit", {})
                _capture_import_audit(self.import_audit, sanitized_event)
                normalized_event = _normalized_import_telemetry(sanitized_event)
                if normalized_event is None:
                    visible_summary = _structured_local_diagnostic_text(
                        sanitized_event
                    )
                else:
                    visible_summary = format_import_visible_telemetry(
                        normalized_event
                    )
            else:
                visible_summary = _structured_local_diagnostic_text(event)
        except (
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            visible_summary = (
                _structured_local_diagnostic_text(sanitized_event)
                if sanitized_event is not None
                else _sanitize_plain_rust_line(line)
            )

        if visible_summary:
            visible_summary = _escape_local_diagnostic_text(visible_summary)
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

                    display_table = _escape_local_diagnostic_text(table_name)
                    display = f"⏳ {display_table} ({size_str}, {chunk_count} chunks)"
                    item = QListWidgetItem(display)
                    item.setForeground(Qt.GlobalColor.gray)
                    self.table_list.addItem(item)
                    self.table_items[table_name] = item

    def on_import_finished(self, success: bool, message: str, results: dict):
        """Import 완료 처리 (결과 저장 및 재시도 버튼 표시)"""
        self.import_results = results

        failed_tables = [
            table for table, result in self._table_results().items()
            if result.get('status') == 'error'
        ]

        if failed_tables:
            self.btn_retry.setVisible(False)
            self.btn_select_failed.setVisible(True)
            self.txt_log.addItem(f"⚠️ {len(failed_tables)}개 테이블 Import 실패")

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
            message = "안전 복원의 검증 결과가 없습니다. 원본과 새 대상의 상태를 보고서에서 확인하세요."
        message = _escape_local_diagnostic_text(message)
        # 로그 기록
        self.import_end_time = datetime.now()
        self.import_success = success

        table_results = self._table_results()
        done_count = self._count_by_status(table_results, 'done')
        error_count = self._count_by_status(table_results, 'error')
        blocked_count = self._count_by_status(table_results, 'blocked')
        total_count = len(table_results)

        self._add_log(f"{'='*60}")
        self._add_log(f"Import {'성공' if success else '실패'}")
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

        safe_ready = (self.last_import_mode == "safe" and success
                      and self.import_audit.get("verified") is True
                      and (new_target or self.import_audit.get("restore_status") in ("ready_for_switch", "ready_for_review")))
        if safe_ready:
            self.label_status.setText(
                "✅ 요청한 새 대상 복원·검증 완료 · 기존 대상 변경 없음"
                if new_target else "✅ 새 대상 검증 완료 · 원본 미변경 · 전환 전 검토 필요"
                if self.import_audit.get("restore_status") == "ready_for_review"
                else "✅ 새 대상 검증 완료 · 원본 미변경 · 전환 대기"
            )
            self.progress_bar.setValue(100)
            self.btn_copy_restore_target.setEnabled(bool(self.import_audit.get("candidate_target")))
            self.btn_review_restore.setEnabled(not new_target and bool(self.import_audit.get("restore_id") and self.import_audit.get("report_path")))
            QMessageBox.information(self, "안전 복원 검증 완료", message)
            if self.btn_review_restore.isEnabled():
                QTimer.singleShot(0, self.review_restore_target)
        elif success:
            self.label_status.setText(f"✅ Import 완료: {done_count}/{total_count} 테이블 성공")
            self.progress_bar.setValue(100)
            self.txt_log.addItem(f"✅ 완료: {message}")
            QMessageBox.information(self, "Import 완료", f"✅ Import가 완료되었습니다.\n\n성공: {done_count}개 테이블\n\n{message}")
        else:
            self.label_status.setText(
                f"⏹ Import 취소됨: {done_count}/{total_count} 테이블 완료"
                if self._cancel_requested
                else f"❌ Import 실패: {error_count}/{total_count} 테이블 오류"
            )
            if self.last_import_mode == "safe":
                self.label_status.setText(
                    "안전 복원 실패 · 원본 미변경"
                    if self.import_audit.get("original_unchanged") is True
                    else "안전 복원 실패 · 원본 상태는 보고서에서 확인하세요"
                )
            self.txt_log.addItem(f"❌ 실패: {message}")

            if self._cancel_requested:
                self._add_log("Import가 취소되었습니다.")
            else:
                if self.last_import_mode == "safe":
                    QMessageBox.warning(self, "안전 복원 실패", self.label_status.text() + "\n\n" + message)
                elif error_count > 0:
                    QMessageBox.warning(
                        self, "Import 실패",
                        translate_text("❌ Import 중 오류가 발생했습니다.\n\n"
                        "성공: {}개 테이블\n"
                        "실패: {}개 테이블\n\n"
                        "미실행: {}개 테이블\n\n"
                        "대상 데이터를 확인한 후 원래 범위로 전체 Import를 다시 시작하세요.").format(done_count, error_count, blocked_count)
                    )
                else:
                    QMessageBox.warning(self, "Import 실패", f"❌ {message}")

                self._report_error_anonymously()

        if self._close_after_cancel:
            QTimer.singleShot(0, self.close)

    def _report_error_anonymously(self):
        """Submit a privacy-allowlisted report in the background."""
        if not self.config_manager:
            return
        self._start_error_report_worker(
            operation_kind="import",
            db_engine=getattr(self.connector, "engine", ""),
            phase="dump.import",
        )

    def select_failed_tables(self):
        """실패한 테이블 모두 선택"""
        for table_name, result in self._table_results().items():
            if result.get('status') in ('error', 'blocked'):
                if table_name in self.table_items:
                    self.table_items[table_name].setSelected(True)

    def copy_restore_target(self):
        candidate = restore_target_connection_info(self.import_audit.get("candidate_target"))
        if (candidate and self.import_audit.get("verified") is True
                and self.import_audit.get("restore_status") in ("ready_for_switch", "ready_for_review", "completed_new_target", "promoted")):
            QApplication.clipboard().setText(json.dumps(candidate, ensure_ascii=False, indent=2))

    def _promotion_payload(self, action: str) -> dict:
        original = restore_target_connection_info(self.import_audit.get("original_target"))
        if not self.restore_config or not original or not self.import_audit.get("restore_id") or not self.import_audit.get("report_path"):
            raise ValueError("검증된 복원 계획 정보가 없습니다. 안전 복원 보고서를 확인하세요.")
        original.update(user=self.restore_config.user, password=self.restore_config.password)
        return {"action": action, "restore_id": self.import_audit["restore_id"],
                "report_path": self.import_audit["report_path"], "target": apply_registered_tls(original)}

    def review_restore_target(self):
        try:
            self._start_promotion(self._promotion_payload("plan"))
        except ValueError as exc:
            QMessageBox.warning(self, "전환 검토 불가", str(exc))

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
        self.btn_review_restore.setEnabled(False)
        if self._promotion_action == "confirm":
            self.import_success = None
            self.import_audit["original_unchanged"] = None
            self.import_audit["restore_status"] = "promotion_in_progress"
            self.btn_copy_restore_target.setEnabled(False)
            self._add_log("원래 이름으로 전환 시작. 결과가 확인될 때까지 원본 상태는 알 수 없습니다.")
        self.worker = RustDumpWorker("promote", self.restore_config, payload=payload)
        self.worker.raw_output.connect(self.on_raw_output)
        self.worker.promotion_finished.connect(self._on_promotion_finished)
        self.worker.start()

    def _on_promotion_finished(self, success: bool, message: str, result: dict):
        action = self._promotion_action
        self.set_ui_enabled(True)
        self.btn_save_log.setEnabled(True)
        if action == "confirm":
            sanitized = _sanitized_rust_event(result)
            self.import_audit["promotion_result"] = {key: sanitized.get(key) for key in (
                "status", "success", "original_unchanged", "cutover_pending", "backup_namespace", "report_path", "phase", "message")}
        if action == "plan" and success and not self._close_after_cancel:
            self.btn_review_restore.setEnabled(True)
            self._review_promotion_plan(result)
        elif action == "confirm" and success and result.get("status") == "promoted":
            self.import_success = True
            self.import_end_time = datetime.now()
            self.import_audit.update(restore_status="promoted", original_unchanged=False, cutover_pending=False)
            self.import_audit["backup_namespace"] = str(result.get("backup_namespace") or "unknown")
            self.import_audit["candidate_target"] = restore_target_connection_info(result.get("active_target") or self.import_audit.get("original_target"))
            self.btn_copy_restore_target.setEnabled(True)
            self.label_status.setText("✅ 기존 이름으로 전환 완료 · 교체한 테이블 데이터는 백업으로 보존")
            self._add_log(self.label_status.text())
            QMessageBox.information(self, "전환 완료", _structured_local_diagnostic_text(result))
        else:
            if action == "plan":
                self.btn_review_restore.setEnabled(True)
            if action == "confirm":
                self.import_success = False
                self.import_end_time = datetime.now()
                known_unchanged = result.get("original_unchanged") is True
                self.import_audit["restore_status"] = "promotion_failed_original_unchanged" if known_unchanged else "promotion_outcome_unknown"
                self.import_audit["original_unchanged"] = True if known_unchanged else None
                self.import_audit["cutover_pending"] = True if known_unchanged else None
                self.label_status.setText("전환 차단 · 원본 미변경" if known_unchanged else "전환 결과 미확인 · 원본/백업/복원 대상의 상태를 확인하세요")
                self.btn_review_restore.setEnabled(known_unchanged)
            self._add_log(message or "Promotion result could not be verified")
            QMessageBox.warning(self, "전환 작업 확인 필요", _escape_local_diagnostic_text(message) or self.label_status.text())
        if self._close_after_cancel:
            QTimer.singleShot(0, self.close)

    def _review_promotion_plan(self, plan: dict):
        safe_plan = _sanitized_rust_event(plan)
        self.import_audit["promotion_plan"] = {key: safe_plan.get(key) for key in (
            "plan_digest", "can_promote", "original_target", "candidate_target", "backup_namespace", "summary", "blockers")}
        review = QMessageBox(self)
        review.setTextFormat(Qt.TextFormat.PlainText)
        review.setWindowTitle("복원 대상 비교 및 전환 선택")
        review.setText("기존 대상은 아직 변경되지 않았습니다. 비교 결과를 검토하고 다음 작업을 선택하세요.")
        review.setInformativeText(_promotion_review_text(self.import_audit["promotion_plan"], self.last_import_context.get("profile") or {}))
        review.setDetailedText(json.dumps(self.import_audit["promotion_plan"], ensure_ascii=False, indent=2))
        keep = review.addButton("기존 대상 유지", QMessageBox.ButtonRole.RejectRole)
        use_candidate = review.addButton("검증된 새 대상 사용", QMessageBox.ButtonRole.ActionRole)
        overwrite = review.addButton("기존 이름으로 교체", QMessageBox.ButtonRole.DestructiveRole)
        cancel = review.addButton(QMessageBox.StandardButton.Cancel)
        overwrite.setEnabled(plan.get("can_promote") is True and bool(plan.get("plan_digest")))
        review.setDefaultButton(cancel)
        review.exec()
        if review.clickedButton() == use_candidate:
            self.import_audit["operator_choice"] = "use_candidate"
            self.copy_restore_target()
            self.label_status.setText("새 대상 연결 정보를 복사했습니다. 애플리케이션 연결 설정은 별도로 변경하세요.")
        elif review.clickedButton() == overwrite and overwrite.isEnabled():
            self._confirm_promotion(plan)
        elif review.clickedButton() == keep:
            self.import_audit["operator_choice"] = "keep_original"
            self.label_status.setText("기존 대상 유지 선택 · 검증된 새 대상은 보존됩니다")
            self._add_log("기존 대상을 유지합니다. 검증된 새 대상은 그대로 보존됩니다.")
        else:
            self.import_audit["operator_choice"] = "cancelled_review"

    def _confirm_promotion(self, plan: dict):
        if plan.get("can_promote") is not True or not plan.get("plan_digest"):
            return
        reply = QMessageBox.question(self, "기존 이름으로 교체 확인",
            "검증된 복원 대상을 기존 이름으로 전환하고 이전 대상은 백업 이름으로 보존합니다. 실제 애플리케이션 연결 설정은 변경하지 않습니다. 진행하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            self.import_audit["operator_choice"] = "cancelled_overwrite"
            return
        if not self._confirm_promotion_guard(plan):
            return
        self.import_audit["operator_choice"] = "promote_confirmed"
        payload = self._promotion_payload("confirm")
        payload.update(plan_digest=plan["plan_digest"], overwrite_confirmed=True)
        self._start_promotion(payload)

    def _confirm_promotion_guard(self, plan: dict) -> bool:
        from html import escape
        from src.core.production_guard import ProductionGuard
        details = _structured_local_diagnostic_text({
            "operation": "promote verified candidate", "original_target": plan.get("original_target"),
            "candidate_target": plan.get("candidate_target"), "backup_namespace": plan.get("backup_namespace"),
        })
        return ProductionGuard(self).confirm_dangerous_operation(
            self.last_import_context.get("profile") or {}, "복원 대상 전환",
            self.last_import_context.get("namespace") or "unknown", escape(details),
        )

    def do_retry(self):
        """A partial rerun cannot restore deferred indexes/FKs on completed tables."""
        QMessageBox.warning(
            self, "재시도 불가",
            "일부 테이블만 재시도하면 인덱스와 외래 키 복원이 누락될 수 있습니다. 대상 데이터를 확인한 후 원래 범위로 전체 Import를 다시 시작하세요.",
        )

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
        schema_name = safe_filename_component(_escape_local_diagnostic_text(schema_name), "unknown")
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
            safe = _escape_local_diagnostic_text
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

                f.write(f"Dump 폴더: {safe(self.last_input_dir)}\n")
                target_schema = (
                    safe(self.last_import_context.get("namespace") or self.last_target_schema or "unknown")
                )
                target_label = "요청한 원본 스키마" if self.last_import_mode == "safe" else "대상 스키마"
                f.write(f"{target_label}: {target_schema}\n")
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
                import_mode = (
                    self._get_import_mode_text(self.last_import_mode)
                    if self.last_import_mode
                    else '확인 불가'
                )
                f.write(f"Import 모드: {safe(import_mode)}\n")
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

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.btn_save_log.setEnabled(True)
            cancel_text = (
                "대상 전환이 실행 중입니다. 취소하면 전환 결과를 확인하지 못할 수 있습니다. 원본·백업·복원 대상의 상태를 보고서와 DB에서 확인해야 합니다. 취소하시겠습니까?"
                if getattr(self, "_promotion_action", "") == "confirm" else
                "Import가 실행 중입니다.\n취소하면 전용 Rust DB Core 프로세스를 종료합니다.\n대상 스키마에 일부 데이터가 반영되었을 수 있습니다.\n\n취소하시겠습니까?"
            )
            reply = QMessageBox.question(
                self,
                translate_text("Import 실행 중"),
                translate_text(cancel_text),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._cancel_requested = True
                self._close_after_cancel = True
                self.import_audit = getattr(self, "import_audit", {})
                self.import_audit["ui_cancel_requested"] = True
                self.import_audit["ui_cancel_stage"] = (
                    "promotion_confirm" if getattr(self, "_promotion_action", "") == "confirm"
                    else "safe_restore" if getattr(self, "last_import_mode", "") == "safe" else "import"
                )
                self._add_log("Import 취소 요청: 전용 Rust DB Core 프로세스 종료를 요청했습니다.")
                self.worker.cancel()
                self.label_status.setText(translate_text("⏹ Import 취소 요청 중..."))
            event.ignore()
            return
        if self.connector:
            self.connector.disconnect()
        self._close_after_cancel = True
        event.accept()
