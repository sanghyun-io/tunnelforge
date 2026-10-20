"""
Rust DB Core 기반 Export 다이얼로그
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
from PyQt6.QtGui import QDesktopServices
from typing import Dict, List, Optional
from datetime import datetime
import json
import os

from src.core.constants import MAX_LOG_ENTRIES, MAX_VISIBLE_LOG_LINES, TABLE_STATUS_ICONS
from src.core.db_core_service import RustDbConnector
from src.core.error_report_sanitizer import (
    sanitize_local_diagnostic,
)
from src.core.i18n import VerbatimText, translate_text
from src.core.logger import get_logger
from src.core.path_safety import safe_filename_component, safe_output_dir
from src.exporters.rust_dump_exporter import (
    RustDumpChecker, build_rust_dump_config, check_rust_dump,
    DEFAULT_DUMP_COMPRESSION, DEFAULT_DUMP_THREADS,
    describe_export_refusal, describe_core_failure, fk_parent_closure, fk_parents_outside, LOSSY_REFUSAL_KINDS,
)
from src.ui.dialogs.collapsible_config_dialog import CollapsibleConfigDialog
from src.ui.workers.error_reporting_worker import (
    ErrorReportingMixin,
    report_operation_error,
    sanitize_local_diagnostic_json,
)
from src.ui.dialogs.job_recording import begin_export_job, finish_export_job
from src.ui.workers.rust_dump_worker import FkParentsWorker, RustDumpWorker

logger = get_logger("db_dialogs")


_UNSYNCHRONIZED_EXPORT_WARNING = (
    "테이블마다 다른 시점에 읽었습니다. 테이블 간 시점 일관성이 보장되지 않습니다."
)
_INCOMPLETE_EXPORT_WARNING = (
    "일부 객체를 빼고 내보냈습니다(불완전 Export). 나머지 테이블의 구조와 데이터는 모두 담았습니다. "
    "빠진 객체는 실행 로그의 'Export 경고' 줄에서 볼 수 있습니다."
)
_INCOMPLETE_EXPORT_TOOLTIP_MYSQL = "원본 정의를 확인하지 못한 트리거·루틴·이벤트와 InnoDB가 아닌 테이블(MyISAM, MEMORY 등)을 묻지 않고 빼고 내보냅니다. InnoDB가 아닌 테이블은 구조와 데이터가 통째로 빠집니다. 결과는 완전한 백업이 아닙니다. 계산 열·CHECK 제약처럼 빼면 테이블 정의가 달라지는 것은 빼고 내보낼 수 없습니다."
_INCOMPLETE_EXPORT_TOOLTIP_POSTGRESQL = "트리거·함수·구체화 뷰처럼 덤프에 담을 수 없는 객체를 묻지 않고 빼고 내보냅니다. 결과는 완전한 백업이 아닙니다. 계산 열·CHECK 제약처럼 빼면 테이블 정의가 달라지는 것은 빼고 내보낼 수 없습니다."
_INCOMPLETE_EXPORT_LIST_LIMIT = 10
_PARTIAL_EXPORT_NOTE = "일부 테이블만 내보낼 때는 테이블과 그 트리거만 담습니다. 뷰·저장 프로시저·함수·이벤트는 빠집니다."
_PARTIAL_EXPORT_NOTE_POSTGRESQL = "일부 테이블만 내보낼 때는 테이블만 담습니다. 뷰·저장 프로시저·함수는 빠집니다."
_FK_AUTO_TOOLTIP = "체크한 테이블이 이 테이블을 참조해 자동으로 담았습니다. 참조하는 테이블의 체크를 풀면 다시 고를 수 있습니다."
_FK_NOTICE_EXAMPLES = 3
# 연결된 부모 테이블을 읽는 스레드. 창을 닫아도 실행 중인 스레드가 파괴되지 않게 끝날 때까지 잡아 둔다.
# ponytail: 앱을 종료하는 순간 읽는 중이면 기다리지 않는다; 종료 시 크래시가 보이면 quit 가드에 넣을 것.
_RUNNING_FK_LOOKUPS: set = set()

# 기억하는 고급 설정 (Import 대화상자처럼 실행할 때 저장한다). 고른 테이블은 기억하지 않는다.
# '시점 일관성 포기'와 '담을 수 없는 객체 빼기'는 기억하지 않는다: Import 가 다른 DB 이름과 시간대를
# 기억하지 않듯, 한 번 켠 값이 다음 백업을 묻지 않고 덜 완전하게 만들면 안 된다.
_PERSISTED_EXPORT_OPTIONS = (
    "rust_dump_export_threads",
    "rust_dump_export_compression",
    "rust_dump_export_include_fk_parents",
    # 예전 '자동 지정/수동 지정' 설정과 같은 키: 'manual' 이면 '폴더 이름 직접 정하기'가 켜진다.
    "rust_dump_export_folder_mode",
    "rust_dump_export_folder_manual_name",
)
_COMPRESSION_CHOICES = (("압축 안 함", "none"), ("압축(zstd)", "zstd"))


_EXPORT_TELEMETRY_TYPES = {
    "dump_plan",
    "dump_schedule",
    "row_progress",
    "table_progress",
}
_EXPORT_TELEMETRY_NUMERIC_FIELDS = {
    "dump_plan": ("tables_total", "rows_total"),
    "dump_schedule": (
        "threads",
        "table_workers",
        "range_workers_per_table",
        "chunk_size",
    ),
    "row_progress": (
        "rows",
        "total",
        "chunk_rows",
        "chunks_done",
        "chunks_total",
        "chunk_index",
        "stream_ms",
        "read_ms",
        "write_ms",
        "load_ms",
    ),
    "table_progress": ("current", "total"),
}
_EXPORT_TELEMETRY_TEXT_FIELDS = {
    "dump_schedule": ("data_format", "compression", "scheduler"),
    "row_progress": ("table", "strategy"),
    "table_progress": ("table", "status", "strategy"),
}


def _normalized_export_telemetry(event: object) -> Optional[dict]:
    """Return an allowlisted, bounded telemetry event or reject it."""
    if type(event) is not dict:
        return None
    event_type = event.get("event")
    if type(event_type) is not str or event_type not in _EXPORT_TELEMETRY_TYPES:
        return None
    normalized = {"event": event_type}
    for key in _EXPORT_TELEMETRY_NUMERIC_FIELDS[event_type]:
        value = event.get(key, 0)
        if value is None:
            value = 0
        if type(value) is not int or value < 0 or value > (2**64 - 1):
            return None
        normalized[key] = value
    for key in _EXPORT_TELEMETRY_TEXT_FIELDS.get(event_type, ()):
        value = event.get(key, "")
        if value is None:
            value = ""
        if type(value) is not str or len(value) > 2_000:
            return None
        normalized[key] = sanitize_local_diagnostic(value, max_length=2_000)

    if event_type in {"table_progress", "row_progress"} and not normalized["table"]:
        return None
    if event_type == "table_progress":
        if normalized["status"] not in {"dumping", "completed"}:
            return None
        if normalized["total"] and normalized["current"] > normalized["total"]:
            return None
    elif event_type == "row_progress":
        if normalized["total"] and normalized["rows"] > normalized["total"]:
            return None
        if (
            normalized["chunks_total"]
            and normalized["chunks_done"] > normalized["chunks_total"]
        ):
            return None
        if (
            normalized["chunks_total"]
            and normalized["chunk_index"] > normalized["chunks_total"]
        ):
            return None

    collection_key = "tables" if event_type == "dump_plan" else "scheduled_tables"
    if event_type in {"dump_plan", "dump_schedule"}:
        collection = event.get(collection_key, [])
        if type(collection) is not list or len(collection) > 64:
            return None
        normalized_items = []
        for item in collection:
            if type(item) is not dict:
                return None
            name = item.get("name", "")
            rows = item.get("rows", 0)
            if type(name) is not str or len(name) > 2_000:
                return None
            if type(rows) is not int or rows < 0 or rows > (2**64 - 1):
                return None
            normalized_item = {
                "name": sanitize_local_diagnostic(name, max_length=2_000),
                "rows": rows,
            }
            if event_type == "dump_schedule":
                chunks = item.get("estimated_chunks", 0)
                if type(chunks) is not int or chunks < 0 or chunks > (2**64 - 1):
                    return None
                normalized_item["estimated_chunks"] = chunks
            normalized_items.append(normalized_item)
        normalized[collection_key] = normalized_items
    return normalized


def cap_incomplete_export_percent(percent: int, completed_tables: int, total_tables: int) -> int:
    """Avoid inflated estimated-row progress while table completion proves export is still running."""
    bounded = max(0, min(int(percent), 100))
    if total_tables <= 0 or completed_tables >= total_tables:
        return bounded
    table_cap = int(((completed_tables + 1) / total_tables) * 100)
    return min(bounded, max(1, min(table_cap, 99)))

def next_export_percent(
    last_percent: int,
    computed_percent: int,
    completed_tables: int,
    total_tables: int,
) -> int:
    """Advance export progress without letting stale early estimates stick at 99%."""
    candidate = max(max(0, int(last_percent)), max(0, min(int(computed_percent), 100)))
    if total_tables > 0 and completed_tables < total_tables:
        return min(candidate, cap_incomplete_export_percent(100, completed_tables, total_tables))
    return candidate

def export_overall_percent(
    last_percent: int,
    overall_done: int,
    total_rows: int,
    fallback_percent: int,
    completed_tables: int,
    total_tables: int,
) -> int:
    """Return monotonic overall export progress from rows when estimates are available."""
    done = max(0, int(overall_done))
    total = max(0, int(total_rows))
    if total > 0:
        computed = min(int((done / total) * 100), 100)
        return max(max(0, int(last_percent)), computed)

    computed = cap_incomplete_export_percent(
        int(fallback_percent),
        completed_tables,
        total_tables,
    )
    return next_export_percent(last_percent, computed, completed_tables, total_tables)

def format_export_row_labels(processed_rows: int, estimated_total_rows: int) -> tuple[str, str]:
    processed = max(0, int(processed_rows))
    estimated = max(0, int(estimated_total_rows))
    processed_label = f"📦 처리 rows: {processed:,} rows"
    if estimated:
        estimate_label = f"📐 예상 전체: 약 {estimated:,} rows"
    else:
        estimate_label = "📐 예상 전체: 계산 중..."
    return processed_label, estimate_label

def format_export_table_status(table: str, rows_done: int, rows_total: int) -> str:
    table_name = table or "-"
    done = max(0, int(rows_done))
    total = max(0, int(rows_total))
    if total:
        percent = min(int((done / total) * 100), 100)
        return f"🔄 현재: {table_name} {done:,} / {total:,} rows ({percent}%)"
    return f"🔄 현재: {table_name} {done:,} rows"


def format_export_visible_telemetry(event: dict) -> Optional[str]:
    """Convert Rust dump telemetry into a concise visible log line."""
    event_type = event.get("event")
    if event_type == "dump_schedule":
        scheduler = str(event.get("scheduler") or "")
        scheduler_part = f", scheduler={scheduler}" if scheduler else ""
        return (
            f"스케줄: {event.get('data_format') or '-'}"
            f"/{event.get('compression') or '-'}{scheduler_part}, "
            f"threads={int(event.get('threads') or 0)}, "
            f"table_workers={int(event.get('table_workers') or 0)}, "
            f"range_workers/table={int(event.get('range_workers_per_table') or 0)}"
        )

    if event_type == "table_progress":
        table = str(event.get("table") or "-")
        status = str(event.get("status") or "")
        current = int(event.get("current") or 0)
        total = int(event.get("total") or 0)
        strategy = str(event.get("strategy") or "")
        suffix = f", {strategy}" if strategy else ""
        if status == "dumping":
            return f"{table}: export 시작 ({current}/{total}){suffix}"
        if status == "completed":
            return f"{table}: export 완료 ({current}/{total}){suffix}"
        return None

    if event_type != "row_progress":
        return None

    table = str(event.get("table") or "-")
    rows_done = max(0, int(event.get("rows") or 0))
    rows_total = max(0, int(event.get("total") or 0))
    chunk_rows = max(0, int(event.get("chunk_rows") or 0))
    chunks_done = int(event.get("chunks_done") or 0)
    chunks_total = int(event.get("chunks_total") or 0)
    chunk_index = int(event.get("chunk_index") or 0)
    strategy = str(event.get("strategy") or "")
    elapsed_ms = int(
        event.get("stream_ms")
        or event.get("read_ms")
        or event.get("write_ms")
        or event.get("load_ms")
        or 0
    )

    if rows_total:
        percent = min(int((rows_done / rows_total) * 100), 100)
        row_part = f"{rows_done:,} / {rows_total:,} rows ({percent}%)"
    else:
        row_part = f"{rows_done:,} rows"

    if chunks_done and chunks_total:
        chunk_part = f"{chunks_done}/{chunks_total} chunks"
    elif chunk_index:
        chunk_part = f"chunk {chunk_index}"
    else:
        chunk_part = f"{chunk_rows:,} rows"

    detail_parts = []
    if chunk_rows:
        if elapsed_ms:
            detail_parts.append(f"{chunk_rows:,} rows in {elapsed_ms / 1000:.1f}s")
        else:
            detail_parts.append(f"{chunk_rows:,} rows")
    if strategy:
        detail_parts.append(strategy)

    details = f", {', '.join(detail_parts)}" if detail_parts else ""
    return f"{table}: {chunk_part}, {row_part}{details}"


# ============================================================
# Rust DB Core 기반 Export 다이얼로그
# ============================================================

class RustDumpExportDialog(CollapsibleConfigDialog, ErrorReportingMixin, QDialog):
    """Rust DB Core Export 다이얼로그"""

    def __init__(self, parent=None, connector: RustDbConnector = None,
                 config_manager=None, connection_info: str = "", job_context: dict = None):
        super().__init__(parent)
        self.setWindowTitle("데이터 Export")
        self.resize(600, 650)
        self.job_context = job_context or {}  # 작업 목록 기록용 프로필 정보 (id/name)
        self._job_id = None

        self.connector = connector
        self.config_manager = config_manager
        self.connection_info = connection_info  # 터널명 또는 host_port
        self.worker: Optional[RustDumpWorker] = None

        # 익명 오류 보고 워커 목록 (완료 전까지 참조 유지)
        self._error_report_workers: List[object] = []
        self._cancel_requested = False
        self._close_after_cancel = False
        # 이번 실행에서 담을 수 없는 객체를 뺄지: 고급 설정 토글, 또는 거부 뒤 사용자가 고른 경우.
        self._allow_incomplete = False
        # Core 결과에서 정해진다 (RustDumpWorker.export_run): 실제로 쓴 방법, 그 한 줄 설명, 뺀 객체.
        self._snapshot_mode = "auto"
        self._snapshot_method = ""
        self._omitted_objects: List[str] = []

        # 연결된 부모 테이블 (fetch_fk_parents 결과는 스키마마다 한 번만 읽는다; 읽지 못하면 캐시하지 않는다)
        self._fk_cache: Dict[str, Optional[dict]] = {}
        self._fk_pending: set = set()  # 백그라운드에서 읽는 중인 스키마
        self._fk_auto_added: set = set()
        self._fk_outside: Optional[List[dict]] = []
        self._syncing_tables = False

        # 로그 수집용 변수
        self.log_entries: List[str] = []
        self.export_start_time: Optional[datetime] = None
        self.export_end_time: Optional[datetime] = None
        self.export_success: Optional[bool] = None
        self.export_schema: str = ""
        self.export_tables: List[str] = []
        self.export_table_totals: dict = {}
        self.export_table_done: dict = {}
        self.export_table_status: dict = {}
        self.export_total_rows: int = 0
        self.export_completed_tables: int = 0
        self.export_completed_table_names: set = set()
        self.export_total_tables: int = 0
        self.export_last_percent: int = 0
        self.export_telemetry_events: List[dict] = []
        self.export_table_started_at: dict = {}
        self.export_table_finished_at: dict = {}

        # rust_dump 설치 확인
        self.rust_dump_installed, self.rust_dump_msg = check_rust_dump()

        self.init_ui()
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
        container_layout.addWidget(self._build_schema_section())
        container_layout.addWidget(self._build_advanced_section())
        container_layout.addWidget(self._build_output_folder_group())

        self._load_export_options()
        self._update_output_dir_preview()
        self._update_advanced_summary()

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
        status_group = QGroupBox("Export 엔진 상태")
        status_layout = QVBoxLayout(status_group)

        if self.rust_dump_installed:
            status_label = QLabel(f"✅ {self.rust_dump_msg}")
            status_label.setStyleSheet("color: green;")
        else:
            status_label = QLabel(f"❌ {self.rust_dump_msg}")
            status_label.setStyleSheet("color: red;")

            btn_guide = QPushButton("설치 가이드 보기")
            btn_guide.setStyleSheet("""
                QPushButton {
                    background-color: #3498db; color: white;
                    padding: 4px 12px; border-radius: 4px; border: none;
                }
                QPushButton:hover { background-color: #2980b9; }
            """)
            btn_guide.clicked.connect(self.show_install_guide)
            status_layout.addWidget(btn_guide)

        status_layout.addWidget(status_label)
        return status_group

    def _build_schema_section(self):
        section = QWidget()
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)

        schema_layout = QHBoxLayout()
        schema_layout.addWidget(QLabel("스키마:" if self._engine() == "postgresql" else "DB:"))
        self.combo_schema = QComboBox()
        self.combo_schema.setMinimumWidth(300)
        self.combo_schema.currentTextChanged.connect(self.on_schema_changed)
        schema_layout.addWidget(self.combo_schema)
        schema_layout.addStretch()
        section_layout.addLayout(schema_layout)

        self.table_group = QGroupBox("테이블 선택")
        table_layout = QVBoxLayout(self.table_group)

        btn_layout = QHBoxLayout()
        btn_select_all = QPushButton("전체 선택")
        btn_select_all.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_select_all.clicked.connect(self.select_all_tables)
        btn_deselect_all = QPushButton("전체 해제")
        btn_deselect_all.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_deselect_all.clicked.connect(self.deselect_all_tables)
        btn_layout.addWidget(btn_select_all)
        btn_layout.addWidget(btn_deselect_all)
        btn_layout.addStretch()
        table_layout.addLayout(btn_layout)

        filter_layout = QHBoxLayout()
        self.input_table_filter = QLineEdit()
        self.input_table_filter.setPlaceholderText("테이블 이름 필터")
        self.input_table_filter.setClearButtonEnabled(True)
        self.input_table_filter.textChanged.connect(self._apply_table_filter)
        self.label_table_count = QLabel("0/0 선택")
        filter_layout.addWidget(self.input_table_filter, 1)
        filter_layout.addWidget(self.label_table_count)
        table_layout.addLayout(filter_layout)

        # 체크박스만 쓴다: 줄 선택(파란 강조)은 담을지 여부와 상관없어 헷갈린다.
        self.list_tables = QListWidget()
        self.list_tables.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list_tables.setMaximumHeight(150)
        self.list_tables.setMinimumHeight(100)  # 낮은 창에서는 눌리지 않고 설정 영역이 스크롤된다
        self.list_tables.itemChanged.connect(self._refresh_table_state)
        table_layout.addWidget(self.list_tables)

        self.lbl_partial_note = QLabel(self._partial_note())
        self.lbl_partial_note.setWordWrap(True)
        self.lbl_partial_note.setStyleSheet("color: #7f8c8d;")
        self.lbl_partial_note.setVisible(False)
        table_layout.addWidget(self.lbl_partial_note)
        # 연결된 부모 테이블: 자동으로 더한 것, 또는 (토글이 꺼져 있을 때) 고르지 않은 부모를 가리키는 테이블.
        self.lbl_fk_note = QLabel()
        self.lbl_fk_note.setWordWrap(True)
        self.lbl_fk_note.setStyleSheet("color: #b45309;")
        self.lbl_fk_note.setVisible(False)
        table_layout.addWidget(self.lbl_fk_note)

        section_layout.addWidget(self.table_group)
        return section

    def _build_advanced_section(self):
        """'고급 설정 열기/닫기' (Import 대화상자와 같은 모양), 처음에는 닫혀 있고 옆 줄에 켜진 설정을 적는다."""
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

        self.chk_include_fk = QCheckBox("연결된 부모 테이블 함께 담기")
        self.chk_include_fk.setToolTip("고른 테이블이 외래 키로 가리키는 부모 테이블을 자동으로 체크하고, 필요한 동안 잠가 둡니다. "
                                       "모든 테이블을 고른 전체 Export에는 영향이 없습니다.")
        self.chk_include_fk.toggled.connect(self._refresh_table_state)
        self.chk_include_fk.toggled.connect(self._update_advanced_summary)
        panel_layout.addWidget(self.chk_include_fk)

        # MySQL 만: 잠금으로 같은 시점을 맞출 수 없을 때 연결 하나 대신 여러 연결로 나눠 읽는다.
        self.chk_unsynchronized = QCheckBox("시점 일관성 포기하고 더 빠르게")
        self.chk_unsynchronized.setToolTip("잠금으로 모든 테이블을 같은 시점에 맞춰 읽을 수 없을 때, 연결 하나로 천천히 읽는 대신 "
                                           "여러 연결로 나눠 빠르게 읽습니다. 테이블마다 읽은 시점이 달라 서로 맞지 않을 수 있습니다.")
        self.chk_unsynchronized.toggled.connect(self._update_advanced_summary)
        self.chk_unsynchronized.setVisible(self._engine() != "postgresql")
        panel_layout.addWidget(self.chk_unsynchronized)

        self.chk_allow_incomplete = QCheckBox("담을 수 없는 객체는 빼고 내보내기")
        self.chk_allow_incomplete.setToolTip(_INCOMPLETE_EXPORT_TOOLTIP_POSTGRESQL if self._engine() == "postgresql" else _INCOMPLETE_EXPORT_TOOLTIP_MYSQL)
        self.chk_allow_incomplete.toggled.connect(self._update_advanced_summary)
        panel_layout.addWidget(self.chk_allow_incomplete)

        options_line = QHBoxLayout()
        options_line.addWidget(QLabel("동시 작업 수:"))
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(DEFAULT_DUMP_THREADS)
        self.spin_threads.valueChanged.connect(self._update_advanced_summary)
        options_line.addWidget(self.spin_threads)
        options_line.addSpacing(16)
        options_line.addWidget(QLabel("압축:"))
        self.combo_compression = QComboBox()
        # QComboBox 항목은 Qt 번역 훅을 거치지 않는다.
        for text, value in _COMPRESSION_CHOICES:
            self.combo_compression.addItem(translate_text(text), value)
        self.combo_compression.setCurrentIndex(self.combo_compression.findData(DEFAULT_DUMP_COMPRESSION))
        self.combo_compression.setToolTip("압축하면 덤프 폴더가 작아지고, Import 할 때 읽으면서 풉니다.")
        self.combo_compression.currentIndexChanged.connect(self._update_advanced_summary)
        options_line.addWidget(self.combo_compression)
        options_line.addStretch()
        panel_layout.addLayout(options_line)

        folder_line = QHBoxLayout()
        self.chk_manual_folder = QCheckBox("폴더 이름 직접 정하기")
        self.chk_manual_folder.setToolTip("끄면 폴더 이름은 연결 이름, DB 이름, 시작 시각으로 정합니다.")
        self.chk_manual_folder.toggled.connect(self._on_manual_folder_toggled)
        self.input_manual_folder = QLineEdit()
        self.input_manual_folder.setPlaceholderText("폴더 이름 입력")
        self.input_manual_folder.setEnabled(False)
        self.input_manual_folder.textChanged.connect(self._update_output_dir_preview)
        self.input_manual_folder.textChanged.connect(self._update_advanced_summary)
        folder_line.addWidget(self.chk_manual_folder)
        folder_line.addWidget(self.input_manual_folder, 1)
        panel_layout.addLayout(folder_line)

        self.advanced_panel.setVisible(False)
        section_layout.addWidget(self.advanced_panel)
        return section

    def _toggle_advanced_options(self):
        visible = self.advanced_panel.isHidden()
        self.advanced_panel.setVisible(visible)
        self.btn_advanced.setText("고급 설정 닫기" if visible else "고급 설정 열기")

    def _on_manual_folder_toggled(self, checked: bool):
        self.input_manual_folder.setEnabled(checked)
        self._update_output_dir_preview()
        self._update_advanced_summary()

    def _engine(self) -> str:
        return build_rust_dump_config(self.connector).engine

    def _namespace_noun(self) -> str:
        """What the selector picks: a PostgreSQL schema, a MySQL database."""
        return "스키마" if self._engine() == "postgresql" else "DB"

    def _partial_note(self) -> str:
        return _PARTIAL_EXPORT_NOTE_POSTGRESQL if self._engine() == "postgresql" else _PARTIAL_EXPORT_NOTE

    def _compression(self) -> str:
        return self.combo_compression.currentData() or DEFAULT_DUMP_COMPRESSION

    def _snapshot_fallback(self) -> str:
        """Where the core's automatic choice ends when no lock gives one point in time (MySQL)."""
        unsynchronized = self.chk_unsynchronized.isChecked() and self._engine() != "postgresql"
        return "parallel_unsynchronized" if unsynchronized else "single_connection"

    def _advanced_summary(self) -> str:
        """Every Advanced setting that is on (or not at its default), in one line."""
        items = []
        if self.chk_include_fk.isChecked():
            items.append(translate_text("연결된 부모 테이블 함께 담기"))
        if self._snapshot_fallback() == "parallel_unsynchronized":
            items.append(translate_text("시점 일관성 포기 허용"))
        if self.chk_allow_incomplete.isChecked():
            items.append(translate_text("담을 수 없는 객체 빼기"))
        if self.spin_threads.value() != DEFAULT_DUMP_THREADS:
            items.append(translate_text("동시 작업 {}개").format(self.spin_threads.value()))
        if self._compression() != DEFAULT_DUMP_COMPRESSION:
            items.append(translate_text(self.combo_compression.currentText()))
        if self.chk_manual_folder.isChecked():
            name = self.input_manual_folder.text().strip()
            items.append(translate_text("폴더 이름({})").format(name) if name else translate_text("폴더 이름(비우면 export_시각)"))
        return translate_text("적용 중인 고급 설정: {}").format(", ".join(items)) if items else ""

    def _update_advanced_summary(self, *_):
        self.lbl_advanced_summary.setText(self._advanced_summary())

    def _load_export_options(self):
        """기억한 고급 설정. 형식이 맞지 않는 값(예전/손으로 고친 설정)은 무시한다."""
        if not self.config_manager:
            return
        get = self.config_manager.get_app_setting
        threads = get("rust_dump_export_threads", DEFAULT_DUMP_THREADS)
        if type(threads) is int and 1 <= threads <= 16:
            self.spin_threads.setValue(threads)
        index = self.combo_compression.findData(get("rust_dump_export_compression", DEFAULT_DUMP_COMPRESSION))
        if index >= 0:
            self.combo_compression.setCurrentIndex(index)
        self.chk_include_fk.setChecked(get("rust_dump_export_include_fk_parents", False) is True)
        manual_name = get("rust_dump_export_folder_manual_name", "")
        self.input_manual_folder.setText(manual_name if isinstance(manual_name, str) else "")
        self.chk_manual_folder.setChecked(get("rust_dump_export_folder_mode", "auto") == "manual")

    def _persisted_export_options(self) -> dict:
        return dict(zip(_PERSISTED_EXPORT_OPTIONS, (
            self.spin_threads.value(),
            self._compression(),
            self.chk_include_fk.isChecked(),
            "manual" if self.chk_manual_folder.isChecked() else "auto",
            self.input_manual_folder.text(),
        )))

    def _build_output_folder_group(self):
        folder_group = QGroupBox("출력 폴더 설정")
        folder_main_layout = QVBoxLayout(folder_group)

        base_layout = QHBoxLayout()
        base_layout.addWidget(QLabel("기본 위치:"))
        self.input_base_dir = QLineEdit()
        self.input_base_dir.setReadOnly(True)
        self.input_base_dir.setText(self._get_base_output_dir())
        btn_browse_base = QPushButton("선택")
        btn_browse_base.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_browse_base.clicked.connect(self.browse_base_dir)
        base_layout.addWidget(self.input_base_dir)
        base_layout.addWidget(btn_browse_base)
        folder_main_layout.addLayout(base_layout)

        # 폴더 이름은 연결 이름 + DB 이름 + 시작 시각으로 정한다 (고급 설정에서 직접 정할 수 있다).
        preview_layout = QHBoxLayout()
        preview_layout.addWidget(QLabel("최종 경로:"))
        self.input_output_dir = QLineEdit()
        self.input_output_dir.setReadOnly(True)
        self.input_output_dir.setStyleSheet("background-color: #f0f0f0;")
        preview_layout.addWidget(self.input_output_dir)
        folder_main_layout.addLayout(preview_layout)

        return folder_group

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
        self.label_data = QLabel("📦 처리 rows: 0 rows")
        self.label_estimated_rows = QLabel("📐 예상 전체: 계산 중...")
        self.label_speed = QLabel("⚡ 속도: 0 MB/s")
        self.label_tables = QLabel("📋 테이블: 0 / 0 완료")
        left_detail.addWidget(self.label_percent)
        left_detail.addWidget(self.label_data)
        left_detail.addWidget(self.label_estimated_rows)
        left_detail.addWidget(self.label_speed)
        left_detail.addWidget(self.label_tables)

        detail_layout.addLayout(left_detail)
        detail_layout.addStretch()
        progress_layout.addLayout(detail_layout)

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

        self.table_status_group = QGroupBox("테이블 Export 상태")
        self.table_status_group.setVisible(False)
        table_status_layout = QVBoxLayout(self.table_status_group)

        self.table_list = QListWidget()
        self.table_list.setMinimumHeight(150)
        self.table_list.setMaximumHeight(200)
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
        """)
        table_status_layout.addWidget(self.table_list)
        progress_main_layout.addWidget(self.table_status_group)

        self.table_items = {}

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

        self.btn_export = QPushButton("🚀 Export 시작")
        self.btn_export.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #229954; }
        """)
        self.btn_export.clicked.connect(self.do_export)
        self.btn_export.setEnabled(self.rust_dump_installed)

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
        self.btn_save_log.setToolTip("Export 완료 후 로그를 파일로 저장할 수 있습니다.")

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
        button_layout.addWidget(self.btn_export)
        button_layout.addWidget(btn_cancel)
        return button_layout

    def _get_base_output_dir(self) -> str:
        """기본 출력 디렉토리 (부모 폴더)"""
        if self.config_manager:
            saved = self.config_manager.get_app_setting('rust_dump_export_base_dir')
            if saved:
                return saved
        return os.path.join(os.path.expanduser("~"), "Desktop")

    def _generate_output_dir(self, schema: str = "") -> str:
        """출력 폴더: 직접 정한 이름, 아니면 연결 이름 + DB 이름 + 시각."""
        base_dir = self._get_base_output_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.chk_manual_folder.isChecked():
            return safe_output_dir(base_dir, self.input_manual_folder.text().strip() or f"export_{timestamp}")
        return safe_output_dir(base_dir, "_".join([part for part in (self.connection_info, schema) if part] + [timestamp]))

    def _unique_output_dir(self, path: str) -> str:
        """이미 존재하는 폴더면 `_2`, `_3`, ... 을 붙여 충돌하지 않는 경로를 반환."""
        if not path or not os.path.exists(path):
            return path
        counter = 2
        while True:
            candidate = f"{path}_{counter}"
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    def _update_output_dir_preview(self, *_):
        """출력 경로 미리보기 업데이트"""
        self.input_output_dir.setText(self._generate_output_dir(self.combo_schema.currentText()))

    def browse_base_dir(self):
        """기본 위치 선택"""
        import os
        current_path = self.input_base_dir.text()
        default_path = current_path if os.path.exists(current_path) else os.path.expanduser("~")

        folder = QFileDialog.getExistingDirectory(
            self, "Export 기본 폴더 선택", default_path
        )
        if folder:
            self.input_base_dir.setText(folder)
            if self.config_manager:
                self.config_manager.set_app_setting('rust_dump_export_base_dir', folder)
            self._update_output_dir_preview()

    def show_install_guide(self):
        guide = RustDumpChecker.get_install_guide()
        QMessageBox.information(self, "Rust DB Core 설치 가이드", guide)

    def apply_job_rerun(self, rerun: dict) -> None:
        """작업 목록의 이전 실행 설정으로 화면만 채운다 (스키마/고른 테이블/압축/동시 작업 수).

        예전 기록의 내보내는 방법과 '부모 테이블 함께 담기' 값은 쓰지 않는다: 지금 고급 설정을 따른다.
        실행은 사용자가 직접 시작하며, 그때 Core 의 사전 검증(지원 객체 거부 등)을 새로 거친다.
        """
        if not isinstance(rerun, dict):
            return
        schema = rerun.get("schema")
        if isinstance(schema, str):
            index = self.combo_schema.findText(schema)
            if index >= 0:
                self.combo_schema.setCurrentIndex(index)
        wanted = set(rerun.get("tables") or []) if rerun.get("scope") == "tables" else None
        self._set_table_checks(lambda item: wanted is None or item.text() in wanted, every=True)
        compression = rerun.get("compression")
        if isinstance(compression, str) and self.combo_compression.findData(compression) >= 0:
            self.combo_compression.setCurrentIndex(self.combo_compression.findData(compression))
        threads = rerun.get("threads")
        if isinstance(threads, int) and not isinstance(threads, bool):
            self.spin_threads.setValue(threads)

    def load_schemas(self):
        self.combo_schema.clear()
        if not self.connector:
            return
        schemas = self.connector.get_schemas()
        for schema in schemas:
            self.combo_schema.addItem(schema)

    def on_schema_changed(self, schema: str):
        self._fk_auto_added = set()
        self._syncing_tables = True  # 목록을 채우는 동안은 한 번에 다시 계산한다
        try:
            self.list_tables.clear()
            if schema and self.connector:
                for table in self.connector.get_tables(schema):
                    item = QListWidgetItem(table)
                    item.setCheckState(Qt.CheckState.Checked)
                    self.list_tables.addItem(item)
        finally:
            self._syncing_tables = False
        self._apply_table_filter(self.input_table_filter.text())
        self._refresh_table_state()

        # 출력 폴더명 업데이트 (스키마 반영)
        self._update_output_dir_preview()

    def _table_items(self) -> List[QListWidgetItem]:
        return [self.list_tables.item(i) for i in range(self.list_tables.count())]

    def _release_auto_added(self, item: QListWidgetItem) -> None:
        """An FK parent the dialog added becomes an ordinary table again (enabled, no tooltip)."""
        self._fk_auto_added.discard(item.text())
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
        item.setToolTip("")

    def _set_table_checks(self, wanted, every: bool = False) -> None:
        """Checks the (visible, unless ``every``) tables ``wanted(item)`` picks and unchecks the rest, then
        recomputes the FK parents once. A table it picks becomes the user's own pick even when it had been
        added as an FK parent; one it does not pick that was added is left to that recomputation."""
        self._syncing_tables = True
        try:
            if every:  # a new set of picks: no table stays added for the old ones
                for item in self._table_items():
                    self._release_auto_added(item)
            for item in self._table_items():
                if not every and item.isHidden():
                    continue
                if wanted(item):
                    if item.text() in self._fk_auto_added:
                        self._release_auto_added(item)
                    item.setCheckState(Qt.CheckState.Checked)
                elif item.text() not in self._fk_auto_added:
                    item.setCheckState(Qt.CheckState.Unchecked)
        finally:
            self._syncing_tables = False
        self._refresh_table_state()

    def select_all_tables(self):
        """전체 선택/해제는 필터에 보이는 테이블에만 적용한다 (필터가 없으면 전체)."""
        self._set_table_checks(lambda _item: True)

    def deselect_all_tables(self):
        self._set_table_checks(lambda _item: False)

    def _apply_table_filter(self, text: str = ""):
        needle = (text or "").strip().lower()
        for item in self._table_items():
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def get_selected_tables(self) -> List[str]:
        """체크된 테이블 전부 (자동으로 더한 부모 테이블 포함): Export 에 보내는 목록 그대로."""
        return [item.text() for item in self._table_items() if item.checkState() == Qt.CheckState.Checked]

    def _picked_tables(self) -> List[str]:
        """사용자가 직접 고른 테이블 (자동으로 더한 부모 테이블 제외)."""
        return [name for name in self.get_selected_tables() if name not in self._fk_auto_added]

    def _is_full_export(self) -> bool:
        """사용자가 모든 테이블을 체크했으면 전체 Export 다 (뷰·저장 프로시저·예약 이벤트까지).

        체크를 푼 테이블이 부모 테이블로 다시 담겼으면 모두 체크되어 있어도 고른 테이블 Export 다."""
        return not self._fk_auto_added and all(
            item.checkState() == Qt.CheckState.Checked for item in self._table_items())

    def _fk_parent_map(self) -> Optional[dict]:
        """이 스키마의 외래 키 부모 (fetch_fk_parents), 스키마마다 한 번 읽는다.

        큰 스키마는 읽는 데 오래 걸려 첫 체크 해제에서 목록이 멈췄으므로, 처음 필요할 때 백그라운드에서
        자기 core 로 읽는다. 읽는 동안과 읽지 못했을 때는 None 이고, 읽지 못했으면 다음 변경 때 다시 읽는다."""
        schema = self.combo_schema.currentText()
        if schema not in self._fk_cache and schema not in self._fk_pending and self.connector:
            self._fk_pending.add(schema)
            lookup = FkParentsWorker(build_rust_dump_config(self.connector), schema)
            lookup.loaded.connect(self._on_fk_parents_loaded)
            _RUNNING_FK_LOOKUPS.add(lookup)
            lookup.finished.connect(lambda lookup=lookup: (lookup.wait(), _RUNNING_FK_LOOKUPS.discard(lookup)))
            lookup.start()
        return self._fk_cache.get(schema)

    def _on_fk_parents_loaded(self, schema: str, deps) -> None:
        self._fk_pending.discard(schema)
        if deps is not None:
            self._fk_cache[schema] = deps
        if schema != self.combo_schema.currentText():
            return
        if deps is None:  # 실패만 알린다. 여기서 다시 계산하면 곧바로 또 읽어 끝없이 반복한다; 다음 변경 때 다시 읽는다
            self._update_table_notes()
        else:
            self._refresh_table_state()

    def _refresh_table_state(self, *_):
        """Recomputes the FK parents of the checked tables on every change, whatever the toggle says.

        Toggle on: the parents a checked table needs are checked and disabled while it needs them.
        Toggle off: nothing is checked for the user; one line names the tables whose parent stays out."""
        if self._syncing_tables:
            return
        items = self._table_items()
        picks = self._picked_tables()
        # Everything checked by the user: no parent can be missing, so the FKs are not read.
        deps = self._fk_parent_map() if len(picks) < len(items) else {}
        names = {item.text() for item in items}
        added = set()
        if self.chk_include_fk.isChecked() and deps:
            added = set(fk_parent_closure(picks, deps)[1]) & names
        self._syncing_tables = True
        try:
            for item in items:
                name = item.text()
                if name in added:
                    item.setCheckState(Qt.CheckState.Checked)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                    item.setToolTip(translate_text(_FK_AUTO_TOOLTIP))
                elif name in self._fk_auto_added:  # no checked table needs it any more: back to the user's choice
                    item.setCheckState(Qt.CheckState.Unchecked)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
                    item.setToolTip("")
        finally:
            self._syncing_tables = False
        self._fk_auto_added = added
        self._fk_outside = None if deps is None else fk_parents_outside(self.get_selected_tables(), deps)
        self._update_table_notes()

    def _update_table_notes(self):
        total = self.list_tables.count()
        checked = len(self.get_selected_tables())
        self.label_table_count.setText(f"{checked}/{total} 선택")
        partial = not self._is_full_export()
        self.lbl_partial_note.setVisible(partial)
        note = ""
        if partial and self._fk_outside is None and self.combo_schema.currentText() in self._fk_pending:
            note = translate_text("연결된 부모 테이블을 확인하는 중입니다.")
        elif partial and self._fk_outside is None:
            note = translate_text(
                "연결된 부모 테이블을 확인하지 못했습니다. Export를 시작할 때 다시 확인해 함께 담습니다."
                if self.chk_include_fk.isChecked()
                else "연결된 부모 테이블을 확인하지 못했습니다. 고른 테이블만 내보냅니다.")
        elif self._fk_auto_added:
            note = translate_text("연결된 부모 테이블 자동 추가 {}개: {}").format(
                len(self._fk_auto_added), ", ".join(sorted(self._fk_auto_added)))
        elif partial and self._fk_outside:
            children = sorted({item["child"] for item in self._fk_outside})
            pairs = ", ".join(f"{item['child']} → {item['parent']}" for item in self._fk_outside[:_FK_NOTICE_EXAMPLES])
            if len(self._fk_outside) > _FK_NOTICE_EXAMPLES:
                pairs += " " + translate_text("외 {}개").format(len(self._fk_outside) - _FK_NOTICE_EXAMPLES)
            note = translate_text(
                "고른 테이블 {}개의 연결된 부모 테이블이 체크되어 있지 않습니다 ({}). 고른 테이블만 내보냅니다. "
                "함께 담으려면 고급 설정에서 '연결된 부모 테이블 함께 담기'를 켜세요.").format(len(children), pairs)
        self.lbl_fk_note.setText(note)
        self.lbl_fk_note.setVisible(bool(note))

    def set_ui_enabled(self, enabled: bool):
        """Export 진행 중 UI 요소 활성화/비활성화"""
        self.combo_schema.setEnabled(enabled)
        self.table_group.setEnabled(enabled)
        self.advanced_panel.setEnabled(enabled)
        self.btn_export.setEnabled(enabled)
        self.input_base_dir.setEnabled(enabled)

    def do_export(self):
        schema = self.combo_schema.currentText()

        if not schema:
            QMessageBox.warning(self, "오류", "스키마를 선택하세요." if self._engine() == "postgresql" else "DB를 선택하세요.")
            return

        # 모두 체크되어 있으면 전체 Export, 아니면 체크한 테이블만
        if not self._is_full_export() and not self.get_selected_tables():
            QMessageBox.warning(self, "오류", "테이블을 하나 이상 고르세요.")
            return

        output_dir = self._resolve_output_dir(schema)

        if not output_dir:
            QMessageBox.warning(self, "오류", "출력 폴더를 선택하세요.")
            return

        self.input_output_dir.setText(output_dir)

        # 설정 저장 (고급 설정은 Import 처럼 실행할 때 기억한다)
        if self.config_manager:
            self.config_manager.set_app_settings({'rust_dump_export_dir': output_dir, 'rust_dump_last_dump_dir': output_dir,
                                                  **self._persisted_export_options()})

        self._reset_export_state(schema)

        # 로그 헤더 추가
        self._add_log(f"{'='*60}")
        self._add_log("Rust DB Core Export 시작")
        self._add_log(f"시작 시간: {self.export_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._add_log(f"{self._namespace_noun()}: {schema}")
        self._add_log(f"Export 범위: {'전체' if self._is_full_export() else '고른 테이블'}")
        if self.export_tables:
            self._add_log(f"선택 테이블: {', '.join(self.export_tables)}")
        if self.export_tables and self._fk_auto_added:
            self._add_log(f"연결된 부모 테이블 자동 추가: {', '.join(sorted(self._fk_auto_added))}")
        if self._advanced_summary():
            self._add_log(self._advanced_summary())
        self._add_log(f"출력 폴더: {output_dir}")
        self._add_log(f"동시 작업 수: {self.spin_threads.value()}")
        self._add_log(f"압축 방식: {self._compression()}")
        self._add_log(f"{'='*60}")

        # UI 상태 변경 - 모든 입력 비활성화
        self.set_ui_enabled(False)

        # 설정 섹션 접기
        self.collapse_config_section()

        # 진행 상황 UI 표시
        self.progress_group.setVisible(True)
        self.table_status_group.setVisible(True)
        self.log_group.setVisible(True)
        self.txt_log.clear()
        self.table_list.clear()
        self.table_items.clear()

        # 프로그레스 바 초기화
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(100)
        self.label_percent.setText("📊 전체 진행률: 0%")
        data_label, estimate_label = format_export_row_labels(0, 0)
        self.label_data.setText(data_label)
        self.label_estimated_rows.setText(estimate_label)
        self.label_speed.setText("⚡ 속도: 0 rows/s")
        self.label_tables.setText("📋 테이블: 0 / 0 완료")
        self.label_status.setText("Export 준비 중...")

        # 작업 스레드 시작
        self.worker = self._build_worker(schema, output_dir)

        self._start_export_worker(self.worker)

    def _resolve_output_dir(self, schema: str) -> str:
        # 실행 시점 기준으로 출력 폴더를 재생성한다.
        # 미리보기(_update_output_dir_preview)는 스키마 변경 시 등 이전 시점에
        # 생성된 값이라 타임스탬프가 오래됐을 수 있어, 같은 세션에서 연속 Export
        # 시 동일 폴더를 재사용하는 문제가 있었다.
        output_dir = self.input_output_dir.text()
        if not self.chk_manual_folder.isChecked():
            output_dir = self._generate_output_dir(schema)
        return self._unique_output_dir(output_dir)

    def _reset_export_state(self, schema: str):
        self._begin_error_report_operation()
        self._job_id = None
        self.log_entries.clear()
        self.export_start_time = datetime.now()
        self.export_end_time = None
        self.export_success = None
        self.export_schema = schema
        self.export_tables = [] if self._is_full_export() else self.get_selected_tables()
        self.export_table_totals = {}
        self.export_table_done = {}
        self.export_table_status = {}
        self.export_total_rows = 0
        self.export_completed_tables = 0
        self.export_completed_table_names = set()
        self.export_total_tables = 0
        self.export_last_percent = 0
        self.export_telemetry_events = []
        self.export_table_started_at = {}
        self.export_table_finished_at = {}
        self.btn_save_log.setEnabled(False)
        self._cancel_requested = False
        self._close_after_cancel = False
        self._allow_incomplete = self.chk_allow_incomplete.isChecked()
        self._snapshot_mode = "auto"
        self._snapshot_method = ""
        self._omitted_objects = []

    def _build_worker(self, schema: str, output_dir: str):
        """The core picks the MySQL method ("auto"); without a lock that gives one point in time it reads
        over one connection, or in parallel without one when '시점 일관성 포기' is on. No privilege question."""
        config = build_rust_dump_config(self.connector)
        options = dict(
            schema=schema,
            output_dir=output_dir,
            threads=self.spin_threads.value(),
            compression=self._compression(),
            mysql_snapshot_mode="auto",
            mysql_snapshot_fallback=self._snapshot_fallback(),
            allow_incomplete=self._allow_incomplete,
        )
        if self._is_full_export():
            return RustDumpWorker("export_schema", config, **options)
        # Exactly the checked tables: the parents the toggle added are already among them. When the FKs
        # could not be read here, the worker reads them again with the toggle on (and fails if it cannot).
        return RustDumpWorker(
            "export_tables", config,
            tables=self.get_selected_tables(),
            include_fk_parents=self.chk_include_fk.isChecked() and self._fk_outside is None,
            auto_added_tables=sorted(self._fk_auto_added),
            fk_parents_outside_dump=self._fk_outside,
            **options,
        )

    def _start_export_worker(self, worker: RustDumpWorker) -> None:
        if self._job_id is None:  # 불완전 선택 뒤 다시 실행해도 같은 작업으로 기록한다
            self._job_id = begin_export_job(self)
        self.worker = worker
        worker.progress.connect(self.on_progress)
        worker.table_progress.connect(self.on_table_progress)
        worker.detail_progress.connect(self.on_detail_progress)
        worker.table_status.connect(self.on_table_status)
        worker.raw_output.connect(self.on_raw_output)
        worker.finished.connect(self.on_finished)
        worker.start()

    def _refusal_lines(self, objects: List[str]) -> List[str]:
        """One plain sentence per refused object, up to the list limit plus a count of the rest."""
        limit = _INCOMPLETE_EXPORT_LIST_LIMIT
        refusal = getattr(self.worker, "export_refusal", None)
        reasons = refusal.get("reasons") if isinstance(refusal, dict) else None
        tables = refusal.get("object_tables") if isinstance(refusal, dict) else None
        listed = [f"• {line}" for line in describe_export_refusal(objects[:limit], reasons, tables)]
        if len(objects) > limit:
            listed.append(translate_text("외 {}개").format(len(objects) - limit))
        return listed

    def _add_details_button(self, box: QMessageBox, title: str, details: str) -> None:
        """'자세히': the core's own codes, beside the window (setDetailedText's button is never translated)."""
        more = box.addButton(translate_text("자세히"), QMessageBox.ButtonRole.ActionRole)
        more.setObjectName("export_refusal_details")
        more.clicked.disconnect()  # QMessageBox closes on any of its buttons; this one keeps the window open.
        more.clicked.connect(lambda: QMessageBox.information(self, title, details))

    def _prompt_incomplete_export(self, objects: List[str]) -> bool:
        """덤프에 담을 수 없는 객체를 읽을 수 있는 문장으로 보여주고 불완전 Export를 명시적으로 선택받는다.

        '담을 수 없는 객체는 빼고 내보내기'가 꺼져 있을 때만 묻는다."""
        message_box = QMessageBox(self)
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        title = translate_text("덤프에 담을 수 없는 객체가 있습니다")
        message_box.setWindowTitle(title)
        message_box.setText(
            translate_text(
                "덤프에 담을 수 없는 객체가 있어 Export를 시작하지 않았습니다. 아직 파일은 만들지 않았습니다.\n\n"
                "'이 객체 없이 내보내기 (불완전)'를 누르면 아래 객체만 빼고 "
                "나머지 테이블의 구조와 데이터는 모두 내보냅니다. 완전한 백업은 아닙니다. "
                "고급 설정에서 '담을 수 없는 객체는 빼고 내보내기'를 켜 두면 다음부터 묻지 않습니다.\n\n"
                "뺄 객체:"
            )
            + "\n" + "\n".join(self._refusal_lines(objects))
        )
        proceed_button = message_box.addButton(
            translate_text("이 객체 없이 내보내기 (불완전)"),
            QMessageBox.ButtonRole.DestructiveRole,
        )
        proceed_button.setObjectName("export_incomplete_proceed")
        cancel_button = message_box.addButton(
            translate_text("취소"), QMessageBox.ButtonRole.RejectRole
        )
        cancel_button.setObjectName("export_incomplete_cancel")
        self._add_details_button(message_box, title, "\n".join(objects))
        message_box.setDefaultButton(cancel_button)
        message_box.setEscapeButton(cancel_button)
        message_box.exec()
        return message_box.clickedButton() is proceed_button

    def _show_lossy_refusal(self, objects: List[str]) -> QMessageBox:
        """테이블 정의가 바뀌는 객체: 빼고 내보낼 수 없으니 이유만 보여 주고 닫기만 둔다.

        Core 는 뺄 수 있는 객체(트리거, InnoDB 가 아닌 테이블, 루틴·이벤트)도 같은 목록 뒤에 붙인다: 따로 보여 준다."""
        lossy = [item for item in objects if str(item).partition(":")[0] in LOSSY_REFUSAL_KINDS] or objects
        others = [item for item in objects if item not in lossy]
        box = QMessageBox(self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        title = translate_text("Export를 시작하지 않았습니다")
        box.setWindowTitle(title)
        hint = translate_text("목록에서 이 테이블의 체크를 풀면 나머지 테이블은 내보낼 수 있습니다.")
        if not self.export_tables:  # a full export becomes a table export once a table is unchecked
            hint += " " + translate_text(self._partial_note())
        box.setText(
            translate_text("덤프에 그대로 담을 수 없는 테이블 정의가 있어 Export를 시작하지 않았습니다. "
                           "아직 파일은 만들지 않았습니다.")
            + "\n\n" + "\n".join(self._refusal_lines(lossy))
            + ("\n\n" + translate_text("그 밖에 빼고 내보낼 수 있는 객체:") + "\n" + "\n".join(self._refusal_lines(others))
               if others else "")
            + "\n\n" + hint
        )
        close_button = box.addButton(translate_text("닫기"), QMessageBox.ButtonRole.RejectRole)
        close_button.setObjectName("export_refusal_close")
        self._add_details_button(box, title, "\n".join(objects))
        box.open()
        return box

    def _retry_allow_incomplete(self) -> None:
        self._allow_incomplete = True
        self._add_log("사용자가 '이 객체 없이 내보내기 (불완전)'를 선택했습니다.")
        self.set_ui_enabled(False)
        self.collapse_config_section()
        self.label_status.setText("일부 객체를 제외한 Export (불완전) 진행 중...")
        worker = self._build_worker(self.export_schema, self.input_output_dir.text())
        self._start_export_worker(worker)

    def _add_log(self, msg: str):
        """로그 항목 추가 (수집용)"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = f"[{timestamp}] {sanitize_local_diagnostic(msg)}"
        self.log_entries.append(log_entry)
        if len(self.log_entries) > MAX_LOG_ENTRIES:
            del self.log_entries[:-MAX_LOG_ENTRIES]
        if hasattr(self, "btn_save_log"):
            self.btn_save_log.setEnabled(True)

    def on_progress(self, msg: str):
        msg = sanitize_local_diagnostic(msg)
        self.txt_log.addItem(msg)
        self.txt_log.scrollToBottom()
        self._add_log(msg)

    def on_table_progress(self, current: int, total: int, table_name: str):
        """테이블별 진행률 업데이트"""
        if table_name:
            self.export_completed_table_names.add(table_name)
        self.export_completed_tables = max(
            self.export_completed_tables,
            len(self.export_completed_table_names),
        )
        self.export_total_tables = max(self.export_total_tables, total)
        self.label_tables.setText(
            f"📋 테이블: {self.export_completed_tables} / {self.export_total_tables} 완료"
        )
        display_table = sanitize_local_diagnostic(table_name)
        self._add_log(
            f"테이블 완료: {display_table} ({self.export_completed_tables}/{self.export_total_tables})"
        )

    def _report_error_anonymously(self):
        """Submit a privacy-allowlisted report in the background."""
        report_operation_error(self, "export", "dump.run")

    def on_finished(self, success: bool, message: str):
        message = sanitize_local_diagnostic(message)
        rls_sentence = (describe_core_failure(message) if not success and "row_security_blocks_read:" in message else None)
        rls_detail = message if rls_sentence else ""
        if rls_sentence:
            logger.info("Export source read refused: %s", message)
            message = rls_sentence
        # The core refused before writing anything: objects the dump cannot keep. No privilege question
        # ever: a missing privilege only moved the core on to the next method (mysql_snapshot_mode "auto").
        refusal = getattr(self.worker, "export_refusal", None)
        refused = (not success and not self._cancel_requested and isinstance(refusal, dict)
                   and bool(refusal.get("objects")))
        if refused and refusal.get("bypassable") and not self._allow_incomplete:
            if self._prompt_incomplete_export(list(refusal["objects"])):
                self._retry_allow_incomplete()
                return
            self._add_log("담을 수 없는 객체가 있어 Export를 시작하지 않았습니다 (사용자가 취소).")
        if refused:
            # 작업 목록과 저장한 로그에는 읽을 수 있는 문장만 남긴다. Core 원문은 앱 로그에만 둔다.
            logger.info("Export refused by the core: %s", message)
            objects = list(refusal["objects"])
            message = " ".join([translate_text("담을 수 없는 객체가 있어 Export를 시작하지 않았습니다."),
                                "; ".join(describe_export_refusal(objects[:_INCOMPLETE_EXPORT_LIST_LIMIT], refusal.get("reasons"), refusal.get("object_tables")))])
            if len(objects) > _INCOMPLETE_EXPORT_LIST_LIMIT:
                message += " " + translate_text("외 {}개").format(len(objects) - _INCOMPLETE_EXPORT_LIST_LIMIT)
        run = getattr(self.worker, "export_run", None)
        if success and isinstance(run, dict):
            self._snapshot_mode = run.get("mode") or self._snapshot_mode
            self._snapshot_method = str(run.get("method") or "")
            self._omitted_objects = [str(item) for item in run.get("omitted") or []]

        # 로그 기록
        self.export_end_time = datetime.now()
        self.export_success = success
        finish_export_job(self, success, message)

        self._add_log(f"{'='*60}")
        self._add_log(f"Export {'성공' if success else '실패'}")
        self._add_log(f"종료 시간: {self.export_end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        if self.export_start_time:
            elapsed = self.export_end_time - self.export_start_time
            self._add_log(f"소요 시간: {elapsed}")
        self._add_log(f"결과 메시지: {message}")
        self._add_log(f"{'='*60}")

        # UI 상태 복구
        self.set_ui_enabled(True)
        self.btn_save_log.setEnabled(True)  # 로그 저장 버튼 활성화

        # 설정 섹션 펼치기
        self.expand_config_section()

        unsynchronized = self._snapshot_mode == "parallel_unsynchronized"
        if success:
            # message 는 Core 가 실제로 쓴 방법으로 끝난다 ("Export 완료: 테이블 3개, 행 100개 · 같은 시점으로 내보냄 · ...").
            self.txt_log.addItem(f"✅ {message}")
            if unsynchronized:
                self.txt_log.addItem(f"⚠️ {_UNSYNCHRONIZED_EXPORT_WARNING}")
                self._add_log(f"⚠️ {_UNSYNCHRONIZED_EXPORT_WARNING}")
            # 최종 진행률 100% 표시
            self.progress_bar.setValue(100)
            self.progress_bar.setMaximum(100)  # 퍼센트 기준으로 재설정
            self.label_percent.setText("📊 전체 진행률: 100%")
            data_label, estimate_label = format_export_row_labels(
                sum(self.export_table_done.values()),
                self.export_total_rows,
            )
            self.label_data.setText(data_label)
            self.label_estimated_rows.setText(estimate_label)
            self.label_speed.setText("⚡ 속도: -")
            self.label_status.setText("✅ Export 완료")
            # 테이블 완료 수 계산 (done 상태인 테이블 수)
            done_count = sum(1 for item in self.table_items.values()
                           if item.text().startswith("✅"))
            total_count = len(self.table_items)
            if total_count > 0:
                self.label_tables.setText(f"📋 테이블: {done_count} / {total_count} 완료")
            self._show_export_done(
                translate_text("✅ Export가 완료되었습니다.\n\n폴더: {}").format(self.input_output_dir.text())
                + ("\n" + translate_text("내보낸 방법: {}").format(self._snapshot_method) if self._snapshot_method else "")
                + (f"\n\n⚠️ {translate_text(_UNSYNCHRONIZED_EXPORT_WARNING)}" if unsynchronized else "")
                + (f"\n\n⚠️ {translate_text(_INCOMPLETE_EXPORT_WARNING)}" if self._omitted_objects else "")
            )
        else:
            self.txt_log.addItem(
                "❌ " + translate_text("담을 수 없는 객체가 있어 Export를 시작하지 않았습니다.") if refused
                else f"❌ 실패: {message}"
            )
            self.label_data.setText("📦 데이터: -" if refused else "📦 데이터: Export 실패")
            self.label_estimated_rows.setText("📐 예상 전체: -")
            self.label_speed.setText("⚡ 속도: -")
            self.label_status.setText(
                "⏹ Export 취소됨" if self._cancel_requested
                else "⏹ Export를 시작하지 않았습니다" if refused else "❌ Export 실패"
            )
            # 테이블 완료 수 계산
            done_count = sum(1 for item in self.table_items.values()
                           if item.text().startswith("✅"))
            total_count = len(self.table_items)
            if total_count > 0:
                self.label_tables.setText(f"📋 테이블: {done_count} / {total_count} 완료")

            if self._cancel_requested:
                self._add_log("Export가 취소되었습니다.")
            elif refused:
                # 덤프에 담을 수 없는 것은 앱 오류가 아니다: 오류 보고 없이 이유만 보여 준다.
                if not refusal.get("bypassable"):
                    self._show_lossy_refusal(list(refusal["objects"]))
            elif rls_sentence:
                box = QMessageBox(QMessageBox.Icon.Warning, translate_text("Export 실패"), message,
                                  QMessageBox.StandardButton.Ok, self)
                box.setDetailedText(VerbatimText(sanitize_local_diagnostic(rls_detail)))
                box.exec()
            else:
                QMessageBox.warning(self, "Export 실패", f"❌ {message}")

                self._report_error_anonymously()

        if self._close_after_cancel:
            QTimer.singleShot(0, self.close)

    def on_detail_progress(self, info: dict):
        """상세 진행 정보 업데이트"""
        if info.get("event") == "dump_plan":
            tables = info.get("tables") or []
            self.export_table_totals = {
                str(item.get("name")): int(item.get("rows") or 0)
                for item in tables
                if item.get("name")
            }
            self.export_table_done = {name: 0 for name in self.export_table_totals}
            self.export_total_rows = int(info.get("rows_total") or sum(self.export_table_totals.values()))
            self.export_total_tables = int(info.get("tables_total") or len(self.export_table_totals))
            self.label_tables.setText(f"📋 테이블: 0 / {self.export_total_tables} 완료")
            data_label, estimate_label = format_export_row_labels(0, self.export_total_rows)
            self.label_data.setText(data_label)
            self.label_estimated_rows.setText(estimate_label)
            self.label_status.setText("Export 계획 수립 완료")
            return

        table = str(info.get("table") or "")
        if table:
            rows_done = int(info.get("rows_done") or 0)
            table_total = int(info.get("rows_total") or 0)
            if table_total:
                previous_total = int(self.export_table_totals.get(table) or 0)
                self.export_table_totals[table] = table_total
                if previous_total:
                    self.export_total_rows = max(
                        0,
                        self.export_total_rows - previous_total + table_total,
                    )
                else:
                    self.export_total_rows = max(
                        self.export_total_rows,
                        sum(self.export_table_totals.values()),
                    )
                rows_done = min(rows_done, table_total)
            self.export_table_done[table] = max(self.export_table_done.get(table, 0), rows_done)

        overall_done = sum(self.export_table_done.values())
        percent = export_overall_percent(
            self.export_last_percent,
            overall_done,
            self.export_total_rows,
            int(info.get("percent") or 0),
            self.export_completed_tables,
            self.export_total_tables,
        )
        self.export_last_percent = percent

        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(percent)
        self.label_percent.setText(f"📊 전체 진행률: {percent}%")
        data_label, estimate_label = format_export_row_labels(overall_done, self.export_total_rows)
        self.label_data.setText(data_label)
        self.label_estimated_rows.setText(estimate_label)
        display_speed = sanitize_local_diagnostic(
            info.get('speed', 'Rust DB Core')
        )
        self.label_speed.setText(f"⚡ 속도: {display_speed}")

        if table:
            table_total = self.export_table_totals.get(table) or int(info.get("rows_total") or 0)
            self.label_status.setText(
                format_export_table_status(
                    sanitize_local_diagnostic(table),
                    self.export_table_done.get(table, 0),
                    table_total,
                )
            )

    def on_table_status(self, table_name: str, status: str, message: str):
        """테이블 상태 업데이트"""
        display_table = sanitize_local_diagnostic(table_name)
        display_message = sanitize_local_diagnostic(message)
        now = datetime.now()
        self.export_table_status[table_name] = status
        if status == "loading":
            self.export_table_started_at.setdefault(table_name, now)
            self.label_status.setText(f"🔄 {display_table}")
        elif status in ("done", "error"):
            self.export_table_finished_at[table_name] = now

        icon = TABLE_STATUS_ICONS.get(status, '❓')

        # 기존 아이템이 있으면 업데이트, 없으면 새로 생성
        if table_name in self.table_items:
            item = self.table_items[table_name]
            display_text = f"{icon} {display_table}"
            if status == 'error' and message:
                display_text += f" - {display_message[:50]}..."
            item.setText(display_text)
        else:
            # 새 아이템 생성
            display_text = f"{icon} {display_table}"
            if status == 'error' and message:
                display_text += f" - {display_message[:50]}..."
            item = QListWidgetItem(display_text)
            self.table_list.addItem(item)
            self.table_items[table_name] = item

        # 로그에 테이블 상태 변경 기록 (done/error만)
        if status in ('done', 'error'):
            status_text = '완료' if status == 'done' else f'오류: {display_message}'
            self._add_log(f"테이블 [{display_table}] {status_text}")

    def on_raw_output(self, line: str):
        """rust_dump 실시간 출력 처리 (로그에 추가)"""
        is_telemetry_event = False
        visible_summary = None
        try:
            event = json.loads(line)
        except Exception:
            event = None
        normalized_event = _normalized_export_telemetry(event)
        if normalized_event is not None:
            is_telemetry_event = True
            self.export_telemetry_events.append(normalized_event)
            if len(self.export_telemetry_events) > MAX_LOG_ENTRIES:
                del self.export_telemetry_events[:-MAX_LOG_ENTRIES]
            visible_summary = format_export_visible_telemetry(normalized_event)

        if visible_summary:
            visible_summary = sanitize_local_diagnostic(visible_summary)
            # 너무 많은 로그 방지
            if self.txt_log.count() > MAX_VISIBLE_LOG_LINES:
                self.txt_log.takeItem(0)
            self.txt_log.addItem(visible_summary)
            self.txt_log.scrollToBottom()
            self._add_log(visible_summary)
        elif not is_telemetry_event:
            visible_line = (
                sanitize_local_diagnostic_json(event)
                if type(event) in {dict, list}
                else sanitize_local_diagnostic(line)
            )
            # 너무 많은 로그 방지
            if self.txt_log.count() > MAX_VISIBLE_LOG_LINES:
                self.txt_log.takeItem(0)
            self.txt_log.addItem(visible_line)
            self.txt_log.scrollToBottom()

    def _show_export_done(self, text: str) -> QMessageBox:
        """완료 안내 + '폴더 열기'(결과 폴더를 탐색기로 연다). open() 이라 호출부를 막지 않는다."""
        box = QMessageBox(self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Export 완료")
        box.setText(text)
        open_button = box.addButton(translate_text("📂 폴더 열기"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.buttonClicked.connect(lambda button: self.open_output_folder() if button is open_button else None)
        box.open()
        return box

    def open_output_folder(self) -> bool:
        folder = self.input_output_dir.text()
        if folder and os.path.isdir(folder):
            return QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        return False

    def _export_table_duration_seconds(self, table_name: str) -> float:
        start = self.export_table_started_at.get(table_name)
        end = self.export_table_finished_at.get(table_name)
        if not start or not end:
            return 0.0
        return max(0.0, (end - start).total_seconds())

    def _export_slow_table_summaries(self) -> List[dict]:
        summaries = []
        for table, total_rows in self.export_table_totals.items():
            summaries.append({
                "table": table,
                "rows": total_rows,
                "done": self.export_table_done.get(table, 0),
                "duration_sec": self._export_table_duration_seconds(table),
            })
        return sorted(summaries, key=lambda item: item["duration_sec"], reverse=True)

    def _export_slow_chunk_summaries(self) -> List[dict]:
        summaries = []
        for event in self.export_telemetry_events:
            if event.get("event") != "row_progress":
                continue
            elapsed_ms = int(
                event.get("stream_ms")
                or event.get("read_ms")
                or event.get("write_ms")
                or event.get("load_ms")
                or 0
            )
            if elapsed_ms <= 0:
                continue
            summaries.append({
                "table": str(event.get("table") or ""),
                "chunk_index": event.get("chunk_index"),
                "chunk_rows": int(event.get("chunk_rows") or 0),
                "elapsed_ms": elapsed_ms,
                "strategy": str(event.get("strategy") or ""),
            })
        return sorted(summaries, key=lambda item: item["elapsed_ms"], reverse=True)

    def _export_schedule_summary(self) -> Optional[dict]:
        for event in self.export_telemetry_events:
            if event.get("event") == "dump_schedule":
                return event
        return None

    def save_log(self):
        """로그를 파일로 저장"""
        if not self.log_entries:
            QMessageBox.warning(self, "로그 없음", "저장할 로그가 없습니다.")
            return

        # 기본 파일명 생성
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if self.export_success is None:
            status = "running"
        else:
            status = "success" if self.export_success else "failed"
        schema_name = safe_filename_component(sanitize_local_diagnostic(self.export_schema or ""), "unknown")
        default_filename = f"export_log_{schema_name}_{status}_{timestamp}.txt"

        # 파일 저장 대화상자
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "로그 파일 저장",
            os.path.join(self._get_base_output_dir(), default_filename),
            "텍스트 파일 (*.txt);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            safe = sanitize_local_diagnostic
            with open(file_path, 'w', encoding='utf-8') as f:
                # 헤더 정보
                f.write("=" * 70 + "\n")
                f.write("Rust DB Core Export Log\n")
                f.write("=" * 70 + "\n\n")

                f.write(f"{self._namespace_noun()}: {safe(self.export_schema)}\n")
                f.write(f"Export 범위: {'고른 테이블' if self.export_tables else '전체'}\n")
                if self.export_tables:
                    f.write(
                        f"선택 테이블: "
                        f"{', '.join(safe(table) for table in self.export_tables)}\n"
                    )
                f.write(f"출력 폴더: {safe(self.input_output_dir.text())}\n")
                f.write(f"연결 정보: {safe(self.connection_info)}\n")
                if self.export_success is None:
                    result_label = "진행 중"
                else:
                    result_label = "성공 ✅" if self.export_success else "실패 ❌"
                f.write(f"결과: {result_label}\n")

                if self.export_start_time:
                    f.write(f"시작 시간: {self.export_start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                if self.export_end_time:
                    f.write(f"종료 시간: {self.export_end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                if self.export_start_time and self.export_end_time:
                    elapsed = self.export_end_time - self.export_start_time
                    f.write(f"소요 시간: {elapsed}\n")

                f.write("\n" + "=" * 70 + "\n")
                f.write("Export Telemetry Summary\n")
                f.write("=" * 70 + "\n")
                f.write(f"총 rows: {self.export_total_rows:,}\n")
                f.write(f"완료 rows: {sum(self.export_table_done.values()):,}\n")
                f.write(f"수집 이벤트: {len(self.export_telemetry_events):,}\n")
                schedule = self._export_schedule_summary()
                if schedule:
                    f.write("\nAdaptive Schedule\n")
                    f.write(
                        f"- format={safe(schedule.get('data_format'))}, "
                        f"compression={safe(schedule.get('compression'))}, "
                        f"threads={safe(schedule.get('threads'))}, "
                        f"table_workers={safe(schedule.get('table_workers'))}, "
                        f"range_workers/table={safe(schedule.get('range_workers_per_table'))}, "
                        f"chunk_size={safe(schedule.get('chunk_size'))}\n"
                    )
                    for item in (schedule.get("scheduled_tables") or [])[:8]:
                        f.write(
                            f"  - {safe(item.get('name'))}: {int(item.get('rows') or 0):,} rows, "
                            f"{int(item.get('estimated_chunks') or 0):,} chunks\n"
                        )
                f.write("\n느린 테이블 Top 10\n")
                for item in self._export_slow_table_summaries()[:10]:
                    f.write(
                        f"- {safe(item['table'])}: {item['duration_sec']:.1f}s, "
                        f"{item['done']:,}/{item['rows']:,} rows\n"
                    )
                f.write("\n느린 Chunk Top 10\n")
                for item in self._export_slow_chunk_summaries()[:10]:
                    chunk = item["chunk_index"] if item["chunk_index"] is not None else "-"
                    f.write(
                        f"- {safe(item['table'])} chunk {safe(chunk)}: "
                        f"{item['elapsed_ms']}ms, {item['chunk_rows']:,} rows, "
                        f"{safe(item['strategy'] or 'default')}\n"
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
            reply = QMessageBox.question(
                self,
                translate_text("Export 실행 중"),
                translate_text(
                    "Export가 실행 중입니다.\n"
                    "취소하면 전용 Rust DB Core 프로세스를 종료합니다. "
                    "생성 중인 dump 폴더는 불완전할 수 있습니다.\n\n"
                    "취소하시겠습니까?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._cancel_requested = True
                self._close_after_cancel = True
                self._add_log("Export 취소 요청: 전용 Rust DB Core 프로세스 종료를 요청했습니다.")
                self.worker.cancel()
                self.label_status.setText(translate_text("⏹ Export 취소 요청 중..."))
            event.ignore()
            return
        if self.connector:
            self.connector.disconnect()
        event.accept()


