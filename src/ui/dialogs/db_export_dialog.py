"""
Rust DB Core 기반 Export 다이얼로그
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

from src.core.constants import MAX_VISIBLE_LOG_LINES, TABLE_STATUS_ICONS
from src.core.db_connector import MySQLConnector
from src.core.i18n import translate_text
from src.core.logger import get_logger
from src.core.path_safety import safe_output_dir
from src.exporters.rust_dump_exporter import (
    RustDumpChecker, build_rust_dump_config, check_rust_dump,
    DEFAULT_DUMP_COMPRESSION
)
from src.ui.dialogs.collapsible_config_dialog import CollapsibleConfigDialog
from src.ui.workers.github_worker import GithubReportingMixin
from src.ui.workers.rust_dump_worker import RustDumpWorker

logger = get_logger("db_dialogs")


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

class RustDumpExportDialog(CollapsibleConfigDialog, GithubReportingMixin, QDialog):
    """Rust DB Core Export 다이얼로그"""

    def __init__(self, parent=None, connector: MySQLConnector = None,
                 config_manager=None, connection_info: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Rust DB Core Export (병렬 처리)")
        self.resize(600, 650)

        self.connector = connector
        self.config_manager = config_manager
        self.connection_info = connection_info  # 터널명 또는 host_port
        self.worker: Optional[RustDumpWorker] = None

        # GitHub 이슈 보고 워커 목록 (완료 전까지 참조 유지)
        self._github_workers: List[object] = []
        self._cancel_requested = False
        self._close_after_cancel = False

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
        container_layout.addWidget(self._build_export_type_group())
        container_layout.addWidget(self._build_schema_section())
        container_layout.addWidget(self._build_output_folder_group())

        self._load_naming_settings()
        self._update_output_dir_preview()

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

    def _build_export_type_group(self):
        type_group = QGroupBox("Export 유형")
        type_layout = QVBoxLayout(type_group)

        self.btn_type_group = QButtonGroup(self)

        self.radio_full = QRadioButton("전체 스키마 Export")
        self.radio_full.setChecked(True)
        self.radio_partial = QRadioButton("선택 테이블 Export")

        self.btn_type_group.addButton(self.radio_full)
        self.btn_type_group.addButton(self.radio_partial)

        self.radio_full.toggled.connect(self.on_type_changed)

        type_layout.addWidget(self.radio_full)
        type_layout.addWidget(self.radio_partial)
        return type_group

    def _build_schema_section(self):
        section = QWidget()
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)

        schema_layout = QHBoxLayout()
        schema_layout.addWidget(QLabel("Schema:"))
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

        self.list_tables = QListWidget()
        self.list_tables.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.list_tables.setMaximumHeight(150)
        table_layout.addWidget(self.list_tables)

        self.chk_include_fk = QCheckBox("FK 의존성 테이블 자동 포함")
        self.chk_include_fk.setChecked(True)
        table_layout.addWidget(self.chk_include_fk)

        self.table_group.setVisible(False)
        section_layout.addWidget(self.table_group)

        option_group = QGroupBox("Export 옵션")
        option_layout = QFormLayout(option_group)

        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(8)
        option_layout.addRow("병렬 스레드:", self.spin_threads)

        self.combo_compression = QComboBox()
        self.combo_compression.addItems(["none", "zstd"])
        self.combo_compression.setCurrentText(DEFAULT_DUMP_COMPRESSION)
        self.combo_compression.setToolTip("Rust DB Core dump 압축 방식입니다. zstd는 디스크 사용량을 줄이고 import 시 스트리밍 해제됩니다.")
        option_layout.addRow("압축 방식:", self.combo_compression)

        section_layout.addWidget(option_group)
        return section

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

        naming_layout = QHBoxLayout()
        naming_layout.addWidget(QLabel("폴더 이름:"))

        self.radio_auto_naming = QRadioButton("자동 지정")
        self.radio_auto_naming.setChecked(True)
        self.radio_auto_naming.toggled.connect(self._on_naming_mode_changed)
        naming_layout.addWidget(self.radio_auto_naming)

        self.chk_name = QCheckBox("name")
        self.chk_name.setChecked(True)
        self.chk_name.setToolTip("연결 정보 (터널명 또는 host_port)")
        self.chk_name.toggled.connect(self._on_naming_option_changed)

        self.chk_schema = QCheckBox("schema")
        self.chk_schema.setChecked(True)
        self.chk_schema.toggled.connect(self._on_naming_option_changed)

        self.chk_timestamp = QCheckBox("timestamp")
        self.chk_timestamp.setChecked(True)
        self.chk_timestamp.toggled.connect(self._on_naming_option_changed)

        naming_layout.addWidget(self.chk_name)
        naming_layout.addWidget(self.chk_schema)
        naming_layout.addWidget(self.chk_timestamp)

        # 수동 지정 라디오
        self.radio_manual_naming = QRadioButton("수동 지정:")
        self.radio_manual_naming.toggled.connect(self._on_naming_mode_changed)
        naming_layout.addWidget(self.radio_manual_naming)

        self.input_manual_folder = QLineEdit()
        self.input_manual_folder.setPlaceholderText("폴더명 입력...")
        self.input_manual_folder.setEnabled(False)
        self.input_manual_folder.setMaximumWidth(150)
        self.input_manual_folder.textChanged.connect(self._update_output_dir_preview)
        naming_layout.addWidget(self.input_manual_folder)

        naming_layout.addStretch()
        folder_main_layout.addLayout(naming_layout)

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
        """
        동적 출력 폴더명 생성
        설정에 따라 name, schema, timestamp 조합
        """
        base_dir = self._get_base_output_dir()

        # 수동 모드일 경우
        if hasattr(self, 'radio_manual_naming') and self.radio_manual_naming.isChecked():
            manual_name = self.input_manual_folder.text().strip()
            if manual_name:
                return safe_output_dir(base_dir, manual_name)
            return safe_output_dir(base_dir, f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

        # 자동 모드
        parts = []

        # name (연결 정보)
        if hasattr(self, 'chk_name') and self.chk_name.isChecked() and self.connection_info:
            parts.append(self.connection_info)

        # schema
        if hasattr(self, 'chk_schema') and self.chk_schema.isChecked() and schema:
            parts.append(schema)

        # timestamp
        if hasattr(self, 'chk_timestamp') and self.chk_timestamp.isChecked():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            parts.append(timestamp)

        # 모두 비활성화된 경우 기본값
        if not parts:
            parts.append(f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

        folder_name = "_".join(parts)
        return safe_output_dir(base_dir, folder_name)

    def _get_default_output_dir(self) -> str:
        """기본 출력 디렉토리 (초기값)"""
        return self._generate_output_dir("")

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

    def _load_naming_settings(self):
        """폴더 네이밍 설정 로드"""
        if not self.config_manager:
            return

        mode = self.config_manager.get_app_setting('rust_dump_export_folder_mode', 'auto')
        use_name = self.config_manager.get_app_setting('rust_dump_export_folder_use_name', True)
        use_schema = self.config_manager.get_app_setting('rust_dump_export_folder_use_schema', True)
        use_timestamp = self.config_manager.get_app_setting('rust_dump_export_folder_use_timestamp', True)
        manual_name = self.config_manager.get_app_setting('rust_dump_export_folder_manual_name', '')

        if mode == 'manual':
            self.radio_manual_naming.setChecked(True)
            self.input_manual_folder.setText(manual_name)
        else:
            self.radio_auto_naming.setChecked(True)

        self.chk_name.setChecked(use_name)
        self.chk_schema.setChecked(use_schema)
        self.chk_timestamp.setChecked(use_timestamp)

    def _save_naming_settings(self):
        """폴더 네이밍 설정 저장"""
        if not self.config_manager:
            return

        mode = 'manual' if self.radio_manual_naming.isChecked() else 'auto'
        self.config_manager.set_app_setting('rust_dump_export_folder_mode', mode)
        self.config_manager.set_app_setting('rust_dump_export_folder_use_name', self.chk_name.isChecked())
        self.config_manager.set_app_setting('rust_dump_export_folder_use_schema', self.chk_schema.isChecked())
        self.config_manager.set_app_setting('rust_dump_export_folder_use_timestamp', self.chk_timestamp.isChecked())
        self.config_manager.set_app_setting('rust_dump_export_folder_manual_name', self.input_manual_folder.text())

    def _on_naming_mode_changed(self):
        """폴더 네이밍 모드 변경 시"""
        is_auto = self.radio_auto_naming.isChecked()

        # 자동 옵션 활성화/비활성화
        self.chk_name.setEnabled(is_auto)
        self.chk_schema.setEnabled(is_auto)
        self.chk_timestamp.setEnabled(is_auto)

        # 수동 입력 활성화/비활성화
        self.input_manual_folder.setEnabled(not is_auto)

        self._save_naming_settings()
        self._update_output_dir_preview()

    def _on_naming_option_changed(self):
        """자동 네이밍 옵션 변경 시"""
        # 최소 하나는 선택되어야 함
        if not self.chk_name.isChecked() and not self.chk_schema.isChecked() and not self.chk_timestamp.isChecked():
            sender = self.sender()
            if sender:
                sender.setChecked(True)
                QMessageBox.warning(self, "경고", "최소 하나의 옵션은 선택되어야 합니다.")
            return

        self._save_naming_settings()
        self._update_output_dir_preview()

    def _update_output_dir_preview(self):
        """출력 경로 미리보기 업데이트"""
        schema = self.combo_schema.currentText() if hasattr(self, 'combo_schema') else ""
        self.input_output_dir.setText(self._generate_output_dir(schema))

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

    def on_type_changed(self):
        is_partial = self.radio_partial.isChecked()
        self.table_group.setVisible(is_partial)

    def load_schemas(self):
        self.combo_schema.clear()
        if not self.connector:
            return
        schemas = self.connector.get_schemas()
        for schema in schemas:
            self.combo_schema.addItem(schema)

    def on_schema_changed(self, schema: str):
        self.list_tables.clear()
        if not schema or not self.connector:
            return
        tables = self.connector.get_tables(schema)
        for table in tables:
            item = QListWidgetItem(table)
            item.setCheckState(Qt.CheckState.Checked)
            self.list_tables.addItem(item)

        # 출력 폴더명 업데이트 (스키마 반영)
        self._update_output_dir_preview()

    def select_all_tables(self):
        for i in range(self.list_tables.count()):
            self.list_tables.item(i).setCheckState(Qt.CheckState.Checked)

    def deselect_all_tables(self):
        for i in range(self.list_tables.count()):
            self.list_tables.item(i).setCheckState(Qt.CheckState.Unchecked)

    def get_selected_tables(self) -> List[str]:
        tables = []
        for i in range(self.list_tables.count()):
            item = self.list_tables.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                tables.append(item.text())
        return tables

    def set_ui_enabled(self, enabled: bool):
        """Export 진행 중 UI 요소 활성화/비활성화"""
        self.radio_full.setEnabled(enabled)
        self.radio_partial.setEnabled(enabled)
        self.combo_schema.setEnabled(enabled)
        self.table_group.setEnabled(enabled)
        self.spin_threads.setEnabled(enabled)
        self.combo_compression.setEnabled(enabled)
        self.btn_export.setEnabled(enabled)

        # 폴더 설정 UI
        self.input_base_dir.setEnabled(enabled)
        self.radio_auto_naming.setEnabled(enabled)
        self.radio_manual_naming.setEnabled(enabled)
        if enabled:
            # 모드에 따라 옵션 활성화
            is_auto = self.radio_auto_naming.isChecked()
            self.chk_name.setEnabled(is_auto)
            self.chk_schema.setEnabled(is_auto)
            self.chk_timestamp.setEnabled(is_auto)
            self.input_manual_folder.setEnabled(not is_auto)
        else:
            self.chk_name.setEnabled(False)
            self.chk_schema.setEnabled(False)
            self.chk_timestamp.setEnabled(False)
            self.input_manual_folder.setEnabled(False)

    def do_export(self):
        schema = self.combo_schema.currentText()

        if not schema:
            QMessageBox.warning(self, "오류", "스키마를 선택하세요.")
            return

        # 일부 테이블 Export 시 테이블 확인
        if self.radio_partial.isChecked():
            tables = self.get_selected_tables()
            if not tables:
                QMessageBox.warning(self, "오류", "최소 하나의 테이블을 선택하세요.")
                return

        output_dir = self._resolve_output_dir(schema)

        if not output_dir:
            QMessageBox.warning(self, "오류", "출력 폴더를 선택하세요.")
            return

        self.input_output_dir.setText(output_dir)

        # 설정 저장
        if self.config_manager:
            self.config_manager.set_app_setting('rust_dump_export_dir', output_dir)
            self.config_manager.set_app_setting('rust_dump_last_dump_dir', output_dir)

        self._reset_export_state(schema)

        # 로그 헤더 추가
        self._add_log(f"{'='*60}")
        self._add_log("Rust DB Core Export 시작")
        self._add_log(f"시작 시간: {self.export_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._add_log(f"스키마: {schema}")
        self._add_log(f"Export 유형: {'전체 스키마' if self.radio_full.isChecked() else '선택 테이블'}")
        if self.radio_partial.isChecked():
            self._add_log(f"선택 테이블: {', '.join(self.export_tables)}")
        self._add_log(f"출력 폴더: {output_dir}")
        self._add_log(f"병렬 스레드: {self.spin_threads.value()}")
        self._add_log(f"압축 방식: {self.combo_compression.currentText()}")
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

        # 시그널 연결
        self.worker.progress.connect(self.on_progress)
        self.worker.table_progress.connect(self.on_table_progress)
        self.worker.detail_progress.connect(self.on_detail_progress)
        self.worker.table_status.connect(self.on_table_status)
        self.worker.raw_output.connect(self.on_raw_output)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def _resolve_output_dir(self, schema: str) -> str:
        # 실행 시점 기준으로 출력 폴더를 재생성한다.
        # 미리보기(_update_output_dir_preview)는 스키마 변경 시 등 이전 시점에
        # 생성된 값이라 타임스탬프가 오래됐을 수 있어, 같은 세션에서 연속 Export
        # 시 동일 폴더를 재사용하는 문제가 있었다.
        output_dir = self.input_output_dir.text()
        is_auto_mode = not self.radio_manual_naming.isChecked()
        if is_auto_mode and self.chk_timestamp.isChecked():
            output_dir = self._generate_output_dir(schema)
        return self._unique_output_dir(output_dir)

    def _reset_export_state(self, schema: str):
        self.log_entries.clear()
        self.export_start_time = datetime.now()
        self.export_end_time = None
        self.export_success = None
        self.export_schema = schema
        self.export_tables = self.get_selected_tables() if self.radio_partial.isChecked() else []
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

    def _build_worker(self, schema: str, output_dir: str):
        config = build_rust_dump_config(self.connector)
        if self.radio_full.isChecked():
            return RustDumpWorker(
                "export_schema", config,
                schema=schema,
                output_dir=output_dir,
                threads=self.spin_threads.value(),
                compression=self.combo_compression.currentText()
            )
        return RustDumpWorker(
            "export_tables", config,
            schema=schema,
            tables=self.get_selected_tables(),
            output_dir=output_dir,
            threads=self.spin_threads.value(),
            compression=self.combo_compression.currentText(),
            include_fk_parents=self.chk_include_fk.isChecked()
        )

    def _add_log(self, msg: str):
        """로그 항목 추가 (수집용)"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = f"[{timestamp}] {msg}"
        self.log_entries.append(log_entry)
        if hasattr(self, "btn_save_log"):
            self.btn_save_log.setEnabled(True)

    def on_progress(self, msg: str):
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
        self._add_log(
            f"테이블 완료: {table_name} ({self.export_completed_tables}/{self.export_total_tables})"
        )

    def on_finished(self, success: bool, message: str):
        # 로그 기록
        self.export_end_time = datetime.now()
        self.export_success = success

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

        if success:
            self.txt_log.addItem(f"✅ 완료: {message}")
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
            QMessageBox.information(
                self, "Export 완료",
                f"✅ Export가 완료되었습니다.\n\n폴더: {self.input_output_dir.text()}"
            )
        else:
            self.txt_log.addItem(f"❌ 실패: {message}")
            self.label_data.setText("📦 데이터: Export 실패")
            self.label_estimated_rows.setText("📐 예상 전체: -")
            self.label_speed.setText("⚡ 속도: -")
            self.label_status.setText("⏹ Export 취소됨" if self._cancel_requested else "❌ Export 실패")
            # 테이블 완료 수 계산
            done_count = sum(1 for item in self.table_items.values()
                           if item.text().startswith("✅"))
            total_count = len(self.table_items)
            if total_count > 0:
                self.label_tables.setText(f"📋 테이블: {done_count} / {total_count} 완료")

            if self._cancel_requested:
                self._add_log("Export가 취소되었습니다.")
            else:
                QMessageBox.warning(self, "Export 실패", f"❌ {message}")

                # GitHub 이슈 자동 보고
                self._report_error_to_github("export", message)

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
        self.label_speed.setText(f"⚡ 속도: {info.get('speed', 'Rust DB Core')}")

        if table:
            table_total = self.export_table_totals.get(table) or int(info.get("rows_total") or 0)
            self.label_status.setText(
                format_export_table_status(
                    table,
                    self.export_table_done.get(table, 0),
                    table_total,
                )
            )

    def on_table_status(self, table_name: str, status: str, message: str):
        """테이블 상태 업데이트"""
        now = datetime.now()
        self.export_table_status[table_name] = status
        if status == "loading":
            self.export_table_started_at.setdefault(table_name, now)
            self.label_status.setText(f"🔄 {table_name}")
        elif status in ("done", "error"):
            self.export_table_finished_at[table_name] = now

        icon = TABLE_STATUS_ICONS.get(status, '❓')

        # 기존 아이템이 있으면 업데이트, 없으면 새로 생성
        if table_name in self.table_items:
            item = self.table_items[table_name]
            display_text = f"{icon} {table_name}"
            if status == 'error' and message:
                display_text += f" - {message[:50]}..."
            item.setText(display_text)
        else:
            # 새 아이템 생성
            display_text = f"{icon} {table_name}"
            if status == 'error' and message:
                display_text += f" - {message[:50]}..."
            item = QListWidgetItem(display_text)
            self.table_list.addItem(item)
            self.table_items[table_name] = item

        # 로그에 테이블 상태 변경 기록 (done/error만)
        if status in ('done', 'error'):
            status_text = '완료' if status == 'done' else f'오류: {message}'
            self._add_log(f"테이블 [{table_name}] {status_text}")

    def on_raw_output(self, line: str):
        """rust_dump 실시간 출력 처리 (로그에 추가)"""
        is_telemetry_event = False
        visible_summary = None
        try:
            event = json.loads(line)
        except Exception:
            event = None
        if isinstance(event, dict) and event.get("event") in {
            "dump_plan",
            "dump_schedule",
            "row_progress",
            "table_progress",
        }:
            is_telemetry_event = True
            for key in ("password", "credentials"):
                event.pop(key, None)
            self.export_telemetry_events.append(event)
            visible_summary = format_export_visible_telemetry(event)

        if visible_summary:
            # 너무 많은 로그 방지
            if self.txt_log.count() > MAX_VISIBLE_LOG_LINES:
                self.txt_log.takeItem(0)
            self.txt_log.addItem(visible_summary)
            self.txt_log.scrollToBottom()
            self._add_log(visible_summary)
        elif not is_telemetry_event:
            # 너무 많은 로그 방지
            if self.txt_log.count() > MAX_VISIBLE_LOG_LINES:
                self.txt_log.takeItem(0)
            self.txt_log.addItem(line)
            self.txt_log.scrollToBottom()
        logger.debug("[rust_dump] %s", line)

    def _report_error_to_github(self, error_type: str, error_message: str):
        """GitHub 이슈 자동 보고 (백그라운드)"""
        if not self.config_manager:
            return

        context = {
            'schema': self.export_schema,
            'tables': self.export_tables,
            'mode': '전체 스키마' if self.radio_full.isChecked() else '선택 테이블'
        }

        self._start_github_report_worker(error_type, error_message, context)

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
        default_filename = f"export_log_{self.export_schema}_{status}_{timestamp}.txt"

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
            with open(file_path, 'w', encoding='utf-8') as f:
                # 헤더 정보
                f.write("=" * 70 + "\n")
                f.write("Rust DB Core Export Log\n")
                f.write("=" * 70 + "\n\n")

                f.write(f"스키마: {self.export_schema}\n")
                f.write(f"Export 유형: {'전체 스키마' if self.radio_full.isChecked() else '선택 테이블'}\n")
                if self.export_tables:
                    f.write(f"선택 테이블: {', '.join(self.export_tables)}\n")
                f.write(f"출력 폴더: {self.input_output_dir.text()}\n")
                f.write(f"연결 정보: {self.connection_info}\n")
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
                        f"- format={schedule.get('data_format')}, "
                        f"compression={schedule.get('compression')}, "
                        f"threads={schedule.get('threads')}, "
                        f"table_workers={schedule.get('table_workers')}, "
                        f"range_workers/table={schedule.get('range_workers_per_table')}, "
                        f"chunk_size={schedule.get('chunk_size')}\n"
                    )
                    for item in (schedule.get("scheduled_tables") or [])[:8]:
                        f.write(
                            f"  - {item.get('name')}: {int(item.get('rows') or 0):,} rows, "
                            f"{int(item.get('estimated_chunks') or 0):,} chunks\n"
                        )
                f.write("\n느린 테이블 Top 10\n")
                for item in self._export_slow_table_summaries()[:10]:
                    f.write(
                        f"- {item['table']}: {item['duration_sec']:.1f}s, "
                        f"{item['done']:,}/{item['rows']:,} rows\n"
                    )
                f.write("\n느린 Chunk Top 10\n")
                for item in self._export_slow_chunk_summaries()[:10]:
                    chunk = item["chunk_index"] if item["chunk_index"] is not None else "-"
                    f.write(
                        f"- {item['table']} chunk {chunk}: "
                        f"{item['elapsed_ms']}ms, {item['chunk_rows']:,} rows, "
                        f"{item['strategy'] or 'default'}\n"
                    )

                f.write("\n" + "=" * 70 + "\n")
                f.write("상세 로그\n")
                f.write("=" * 70 + "\n\n")

                for entry in self.log_entries:
                    f.write(entry + "\n")

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


