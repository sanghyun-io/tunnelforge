"""
마이그레이션 분석 다이얼로그
- 스키마 분석 (고아 레코드, 호환성 이슈)
- FK 관계 시각화
- dry-run 정리 영향 분석
"""
import threading
from pathlib import Path
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox,
    QCheckBox, QGroupBox,
    QMessageBox, QProgressBar, QApplication,
    QRadioButton, QButtonGroup, QWidget, QTabWidget,
    QTextEdit, QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFileDialog
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from typing import Iterator, List, Optional, Dict
from datetime import datetime

from src.core.db_connector import MySQLConnector
from src.core.migration_analyzer import (
    MigrationAnalyzer, AnalysisResult, OrphanRecord,
    CompatibilityIssue, ActionType
)
from src.core.migration_constants import ISSUE_TYPE_DISPLAY_NAMES, AUTO_FIXABLE_ISSUE_TYPES
from src.ui.workers.migration_worker import (
    MigrationAnalyzerWorker,
    MigrationCheckOptions,
    CleanupWorker,
)
from src.core.logger import get_logger
from src.ui.dialogs.migration_manual_guide_dialog import ManualGuideDialog
from src.ui.dialogs.migration_result_store import MigrationResultStore

logger = get_logger('migration_dialogs')
ONE_CLICK_MIGRATION_FEATURE_ENABLED = True
ORPHAN_COUNT_CRITICAL_THRESHOLD = 1000
ORPHAN_COUNT_WARNING_THRESHOLD = 100
LEGACY_CLEANUP_EXECUTION_DISABLED_TOOLTIP = (
    "실제 정리 실행은 Rust Core 구현 전까지 비활성화되어 있습니다. "
    "현재는 Dry-Run과 SQL 미리보기만 사용할 수 있습니다."
)

# 다이얼로그가 닫힌 뒤에도 백그라운드에서 계속 실행 중인 Worker (강제 종료 대신 완료까지 추적)
_DETACHED_MIGRATION_WORKERS = set()


def _worker_is_running(worker) -> bool:
    try:
        is_running = getattr(worker, "isRunning")
        return bool(is_running()) if callable(is_running) else False
    except (AttributeError, RuntimeError, TypeError):
        return False


def has_active_detached_migration_workers() -> bool:
    """Return whether a detached migration DB worker is still running."""
    active = False
    for worker in list(_DETACHED_MIGRATION_WORKERS):
        if _worker_is_running(worker):
            active = True
        else:
            _DETACHED_MIGRATION_WORKERS.discard(worker)
    return active


def _disconnect_connector_in_background(connector) -> None:
    """DB 커넥터 연결 해제를 백그라운드 스레드에서 수행 (UI 스레드 블로킹 방지)"""
    if not connector:
        return

    def _run():
        try:
            connector.disconnect()
            logger.info("✅ 백그라운드에서 DB 커넥터 연결 해제 완료")
        except Exception as e:
            logger.error(f"백그라운드 커넥터 연결 해제 오류: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True).start()


def _detach_workers_until_finished(workers, connector) -> None:
    """다이얼로그가 닫힌 뒤 실행 중인 Worker를 강제 종료하지 않고 백그라운드에서 계속 실행시킨다.

    모든 Worker가 완료되면 커넥터를 백그라운드에서 정리한다. UI 스레드를 블로킹하지 않는다.
    """
    remaining = {id(worker): worker for worker in workers}
    if not remaining:
        _disconnect_connector_in_background(connector)
        return

    remaining_lock = threading.Lock()
    disconnect_started = False

    def _make_on_finished(worker):
        def _on_finished(*_args):
            nonlocal disconnect_started
            should_disconnect = False
            with remaining_lock:
                _DETACHED_MIGRATION_WORKERS.discard(worker)
                was_pending = remaining.pop(id(worker), None) is not None
                if was_pending and not remaining and not disconnect_started:
                    disconnect_started = True
                    should_disconnect = True
            if should_disconnect:
                _disconnect_connector_in_background(connector)
        return _on_finished

    for worker in list(remaining.values()):
        on_finished = _make_on_finished(worker)
        try:
            worker.finished.connect(on_finished)
        except Exception as e:
            logger.error(f"디태치 Worker 완료 신호 연결 오류: {e}", exc_info=True)
            on_finished()
            continue

        if not _worker_is_running(worker):
            on_finished()
            continue

        _DETACHED_MIGRATION_WORKERS.add(worker)
        if not _worker_is_running(worker):
            on_finished()


def _safe_disconnect_all(signal) -> None:
    """시그널에 연결된 모든 슬롯을 best-effort로 해제 (연결이 없어도 예외를 삼킨다)"""
    if signal is None:
        return
    try:
        signal.disconnect()
    except (TypeError, RuntimeError):
        pass


def _make_action_button(text: str, bg: str, hover: str, disabled: str = '#bdc3c7') -> QPushButton:
    """굵은 액션 버튼 생성"""
    button = QPushButton(text)
    padding = "8px 20px"
    font_size = "font-size: 14px;"
    if text in {"🔍 Dry-Run (미리보기)", "⚡ 실행"}:
        padding = "8px 16px"
        font_size = ""
    elif text in {"🔧 자동 수정 위저드", "📖 수동 처리 가이드"}:
        padding = "6px 16px"
        font_size = ""

    button.setStyleSheet(f"""
        QPushButton {{
            background-color: {bg}; color: white; font-weight: bold;
            padding: {padding}; border-radius: 4px; border: none;
            {font_size}
        }}
        QPushButton:hover {{ background-color: {hover}; }}
        QPushButton:disabled {{ background-color: {disabled}; }}
    """)
    return button


def _make_secondary_button(text: str) -> QPushButton:
    """보조 버튼 생성"""
    button = QPushButton(text)
    button.setStyleSheet("""
        QPushButton {
            background-color: #ecf0f1; color: #2c3e50;
            padding: 8px 20px; border-radius: 4px; border: 1px solid #bdc3c7;
        }
        QPushButton:hover { background-color: #d5dbdb; }
    """)
    return button


def iter_fk_tree(fk_tree: Dict[str, List[str]]) -> Iterator[tuple[str, int, bool, bool]]:
    """FK 트리를 루트/사이클 처리 규칙에 맞게 순회한다."""
    if not fk_tree:
        return

    all_children = set()
    for children in fk_tree.values():
        all_children.update(children)

    root_tables = sorted(set(fk_tree.keys()) - all_children)
    rendered: set = set()

    def _walk(table: str, depth: int, visited: set) -> Iterator[tuple[str, int, bool, bool]]:
        rendered.add(table)
        children = fk_tree.get(table, [])
        for index, child in enumerate(children):
            is_last = index == len(children) - 1
            if child in visited:
                yield child, depth + 1, True, is_last
                continue
            yield child, depth + 1, False, is_last
            yield from _walk(child, depth + 1, visited | {child})

    for root in root_tables:
        yield root, 0, False, True
        yield from _walk(root, 0, {root})

    for table in sorted(fk_tree.keys()):
        if table in rendered:
            continue
        yield table, 0, False, True
        yield from _walk(table, 0, {table})


def build_orphan_select_sql(orphan: OrphanRecord, schema: str) -> str:
    """고아 레코드 조회 쿼리 생성"""
    return f"""-- {orphan.child_table}.{orphan.child_column} → {orphan.parent_table}.{orphan.parent_column}
-- 고아 레코드 수: {orphan.orphan_count:,}개
SELECT c.*
FROM `{schema}`.`{orphan.child_table}` c
LEFT JOIN `{schema}`.`{orphan.parent_table}` p
    ON c.`{orphan.child_column}` = p.`{orphan.parent_column}`
WHERE c.`{orphan.child_column}` IS NOT NULL
  AND p.`{orphan.parent_column}` IS NULL;"""


def _format_fk_tree_text(fk_tree: Dict[str, List[str]]) -> str:
    """FK 트리를 ASCII 텍스트로 변환하는 순수 포맷터 (DB 접근 없이 fk_tree 데이터만 사용)"""
    if not fk_tree:
        return "FK 관계가 없습니다."

    lines = ["FK 관계 트리:"]
    last_at_depth: List[bool] = []

    for table, depth, is_cycle, is_last in iter_fk_tree(fk_tree):
        if depth == 0:
            lines.append(f"📁 {table}")
            last_at_depth = [True]
            continue

        while len(last_at_depth) <= depth:
            last_at_depth.append(True)
        prefix = "".join("    " if last_at_depth[level] else "│   " for level in range(1, depth))
        branch = "└── " if is_last else "├── "
        label = f"🔄 {table} (순환 참조)" if is_cycle else table
        lines.append(f"{prefix}{branch}{label}")
        last_at_depth[depth] = is_last

    return "\n".join(lines)


class MigrationAnalyzerDialog(QDialog):
    """마이그레이션 분석 다이얼로그"""

    def __init__(self, parent=None, connector: MySQLConnector = None, config_manager=None):
        super().__init__(parent)
        self.setWindowTitle("🔄 마이그레이션 분석기")
        self.resize(1000, 700)

        self.connector = connector
        self.config_manager = config_manager
        self.analysis_result: Optional[AnalysisResult] = None
        self.worker: Optional[MigrationAnalyzerWorker] = None
        self.cleanup_worker: Optional[CleanupWorker] = None
        self.result_store = MigrationResultStore()
        self._is_closing = False  # 닫기 진행 중 플래그
        self._auto_saved_path: Optional[str] = None  # 자동 저장 경로
        self._disconnect_deferred_to_worker_completion = False  # 커넥터 해제를 Worker 완료로 위임했는지 여부

        self.init_ui()
        self.load_schemas()

    @property
    def disconnect_deferred_to_worker_completion(self) -> bool:
        """닫기 시 커넥터 연결 해제가 백그라운드 Worker 완료 시점으로 지연되었는지 여부"""
        return self._disconnect_deferred_to_worker_completion

    def closeEvent(self, event):
        """다이얼로그 닫기 이벤트 - 실행 중인 Worker를 강제 종료하지 않고 백그라운드로 분리"""
        self._is_closing = True

        # 실행 중인 Worker가 있는지 확인
        workers_running = []
        if self.worker and self.worker.isRunning():
            workers_running.append(("분석", self.worker))
        if self.cleanup_worker and self.cleanup_worker.isRunning():
            workers_running.append(("정리", self.cleanup_worker))

        if workers_running:
            # 사용자에게 확인
            # 주의: 이 문구는 src/core/i18n.py의 정규식 번역 항목과 정확히 일치해야 한다
            # (i18n.py는 WP-3.6 허용 파일 범위 밖이라 문구를 변경하면 런타임 영어 번역이 깨진다).
            reply = QMessageBox.question(
                self,
                "작업 진행 중",
                f"현재 {len(workers_running)}개의 작업이 진행 중입니다.\n"
                "창을 닫으면 작업이 중단됩니다. 닫으시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply != QMessageBox.StandardButton.Yes:
                self._is_closing = False
                event.ignore()
                return

            # 닫힌 다이얼로그가 이후 완료 신호로 UI를 건드리지 않도록 결과 슬롯 연결을 먼저 해제
            if self.worker:
                _safe_disconnect_all(self.worker.progress)
                _safe_disconnect_all(self.worker.analysis_complete)
                _safe_disconnect_all(self.worker.finished)
            if self.cleanup_worker:
                _safe_disconnect_all(self.cleanup_worker.progress)
                _safe_disconnect_all(self.cleanup_worker.action_complete)
                _safe_disconnect_all(self.cleanup_worker.finished)

            for name, worker in workers_running:
                logger.info(f"🔀 {name} Worker를 백그라운드로 분리하여 계속 실행합니다 (강제 종료하지 않음)")
                request_interruption = getattr(worker, "requestInterruption", None)
                if callable(request_interruption):
                    request_interruption()

            # quit()/wait()/terminate()로 블로킹하거나 강제 종료하지 않고,
            # Worker가 스스로 끝날 때까지 백그라운드에서 추적한 뒤 커넥터를 정리한다.
            self._disconnect_deferred_to_worker_completion = True
            _detach_workers_until_finished([w for _, w in workers_running], self.connector)

        logger.info("✅ MigrationAnalyzerDialog 정상 종료")
        event.accept()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # --- 상단: 스키마 선택 및 분석 옵션 ---
        top_group = QGroupBox("분석 설정")
        top_layout = QVBoxLayout(top_group)
        top_layout.addLayout(self._build_schema_row())
        top_layout.addLayout(self._build_basic_check_options())
        top_layout.addLayout(self._build_upgrade_checker_options())
        top_layout.addLayout(self._build_action_buttons())

        layout.addWidget(top_group)

        # --- 진행 상황 ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # --- 탭 위젯: 결과 표시 ---
        self.tab_widget = QTabWidget()

        # 탭 1: 개요
        self.tab_overview = QWidget()
        self.init_overview_tab()
        self.tab_widget.addTab(self.tab_overview, "📊 개요")

        # 탭 2: 고아 레코드
        self.tab_orphans = QWidget()
        self.init_orphans_tab()
        self.tab_widget.addTab(self.tab_orphans, "🔗 고아 레코드")

        # 탭 3: 호환성 이슈
        self.tab_compatibility = QWidget()
        self.init_compatibility_tab()
        self.tab_widget.addTab(self.tab_compatibility, "⚠️ 호환성")

        # 탭 4: FK 트리
        self.tab_fk_tree = QWidget()
        self.init_fk_tree_tab()
        self.tab_widget.addTab(self.tab_fk_tree, "🌳 FK 관계")

        # 탭 5: 로그
        self.tab_log = QWidget()
        self.init_log_tab()
        self.tab_widget.addTab(self.tab_log, "📝 로그")

        layout.addWidget(self.tab_widget)

        # --- 하단 버튼 ---
        bottom_layout = QHBoxLayout()

        self.btn_close = _make_secondary_button("닫기")
        self.btn_close.clicked.connect(self.close)

        bottom_layout.addStretch()
        bottom_layout.addWidget(self.btn_close)

        layout.addLayout(bottom_layout)

    def _build_schema_row(self) -> QHBoxLayout:
        schema_layout = QHBoxLayout()
        schema_layout.addWidget(QLabel("스키마:"))
        self.combo_schema = QComboBox()
        self.combo_schema.setMinimumWidth(200)
        schema_layout.addWidget(self.combo_schema)
        schema_layout.addStretch()
        return schema_layout

    def _build_basic_check_options(self) -> QHBoxLayout:
        options_layout = QHBoxLayout()

        self.chk_orphans = QCheckBox("고아 레코드 검사")
        self.chk_orphans.setChecked(True)
        self.chk_orphans.setToolTip("FK 관계에서 부모가 없는 자식 레코드 탐지")

        self.chk_charset = QCheckBox("문자셋 이슈")
        self.chk_charset.setChecked(True)
        self.chk_charset.setToolTip("utf8mb3 사용 테이블/컬럼 확인")

        self.chk_keywords = QCheckBox("예약어 충돌")
        self.chk_keywords.setChecked(True)
        self.chk_keywords.setToolTip("MySQL 8.4 새 예약어와 충돌하는 이름 확인")

        self.chk_routines = QCheckBox("저장 프로시저/함수")
        self.chk_routines.setChecked(True)
        self.chk_routines.setToolTip("deprecated 함수 사용 여부 확인")

        self.chk_sql_mode = QCheckBox("SQL 모드")
        self.chk_sql_mode.setChecked(True)
        self.chk_sql_mode.setToolTip("deprecated SQL 모드 사용 여부 확인")

        options_layout.addWidget(self.chk_orphans)
        options_layout.addWidget(self.chk_charset)
        options_layout.addWidget(self.chk_keywords)
        options_layout.addWidget(self.chk_routines)
        options_layout.addWidget(self.chk_sql_mode)
        options_layout.addStretch()
        return options_layout

    def _build_upgrade_checker_options(self) -> QHBoxLayout:
        options_layout = QHBoxLayout()

        self.chk_auth_plugins = QCheckBox("인증 플러그인")
        self.chk_auth_plugins.setChecked(True)
        self.chk_auth_plugins.setToolTip("mysql_native_password, sha256_password 사용자 확인")

        self.chk_zerofill = QCheckBox("ZEROFILL")
        self.chk_zerofill.setChecked(True)
        self.chk_zerofill.setToolTip("ZEROFILL 속성 사용 컬럼 확인")

        self.chk_float_precision = QCheckBox("FLOAT(M,D)")
        self.chk_float_precision.setChecked(True)
        self.chk_float_precision.setToolTip("FLOAT(M,D), DOUBLE(M,D) deprecated 구문 확인")

        self.chk_fk_name_length = QCheckBox("FK 이름 길이")
        self.chk_fk_name_length.setChecked(True)
        self.chk_fk_name_length.setToolTip("FK 이름 64자 초과 확인")

        options_layout.addWidget(QLabel("🔧 8.4 검사:"))
        options_layout.addWidget(self.chk_auth_plugins)
        options_layout.addWidget(self.chk_zerofill)
        options_layout.addWidget(self.chk_float_precision)
        options_layout.addWidget(self.chk_fk_name_length)
        options_layout.addStretch()
        return options_layout

    def _build_action_buttons(self) -> QHBoxLayout:
        btn_layout = QHBoxLayout()

        self.btn_analyze = _make_action_button("🔍 분석 시작", "#3498db", "#2980b9")
        self.btn_analyze.clicked.connect(self.start_analysis)
        btn_layout.addWidget(self.btn_analyze)

        self.btn_oneclick = _make_action_button("🚀 One-Click Migration", "#27ae60", "#219a52")
        self.btn_oneclick.setToolTip(
            "Rust Core 기반 One-Click 사전 검사/분석/권장/검증을 기본 dry-run으로 실행합니다.\n"
            "백업 확인 후 검증된 MyISAM/deprecated engine 테이블만 InnoDB로 자동 변경할 수 있습니다."
        )
        self.btn_oneclick.clicked.connect(self.start_oneclick_migration)
        self.btn_oneclick.setVisible(ONE_CLICK_MIGRATION_FEATURE_ENABLED)
        btn_layout.addWidget(self.btn_oneclick)

        self.btn_save = QPushButton("💾 결과 저장")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.save_analysis_result)
        btn_layout.addWidget(self.btn_save)

        self.btn_load = QPushButton("📂 결과 불러오기")
        self.btn_load.clicked.connect(self.load_analysis_result)
        btn_layout.addWidget(self.btn_load)

        btn_layout.addStretch()
        return btn_layout

    def init_overview_tab(self):
        """개요 탭 초기화"""
        layout = QVBoxLayout(self.tab_overview)

        # 요약 정보
        self.lbl_summary = QLabel("분석을 시작하세요.")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setStyleSheet("""
            QLabel {
                background-color: #f8f9fa;
                padding: 15px;
                border-radius: 8px;
                font-size: 13px;
            }
        """)
        layout.addWidget(self.lbl_summary)

        # 통계 테이블
        self.table_stats = QTableWidget()
        self.table_stats.setColumnCount(2)
        self.table_stats.setHorizontalHeaderLabels(["항목", "값"])
        self.table_stats.horizontalHeader().setStretchLastSection(True)
        self.table_stats.verticalHeader().setVisible(False)
        layout.addWidget(self.table_stats)

    def init_orphans_tab(self):
        """고아 레코드 탭 초기화"""
        layout = QVBoxLayout(self.tab_orphans)

        # 고아 레코드 테이블
        self.table_orphans = QTableWidget()
        self.table_orphans.setColumnCount(6)
        self.table_orphans.setHorizontalHeaderLabels([
            "자식 테이블", "자식 컬럼", "부모 테이블", "부모 컬럼", "고아 수", "샘플 값"
        ])
        self.table_orphans.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table_orphans.horizontalHeader().setStretchLastSection(True)
        self.table_orphans.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table_orphans.itemSelectionChanged.connect(self.on_orphan_selected)
        layout.addWidget(self.table_orphans)

        # 정리 옵션
        cleanup_group = QGroupBox("정리 작업")
        cleanup_layout = QVBoxLayout(cleanup_group)

        # 조치 선택
        action_layout = QHBoxLayout()
        action_layout.addWidget(QLabel("조치:"))

        self.btn_group_action = QButtonGroup(self)
        self.radio_delete = QRadioButton("삭제 (DELETE)")
        self.radio_delete.setChecked(True)
        self.radio_set_null = QRadioButton("NULL로 설정 (SET NULL)")

        self.btn_group_action.addButton(self.radio_delete)
        self.btn_group_action.addButton(self.radio_set_null)

        action_layout.addWidget(self.radio_delete)
        action_layout.addWidget(self.radio_set_null)
        action_layout.addStretch()
        cleanup_layout.addLayout(action_layout)

        # SQL 미리보기
        self.txt_cleanup_sql = QTextEdit()
        self.txt_cleanup_sql.setReadOnly(True)
        self.txt_cleanup_sql.setMaximumHeight(100)
        self.txt_cleanup_sql.setPlaceholderText("정리할 레코드를 선택하면 SQL이 표시됩니다...")
        self.txt_cleanup_sql.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                background-color: #2d2d2d;
                color: #f8f8f2;
                border-radius: 4px;
            }
        """)
        cleanup_layout.addWidget(self.txt_cleanup_sql)

        # 버튼들
        btn_layout = QHBoxLayout()

        self.btn_dry_run = _make_action_button("🔍 Dry-Run (미리보기)", "#f39c12", "#e67e22")
        self.btn_dry_run.clicked.connect(lambda: self.execute_cleanup(dry_run=True))
        self.btn_dry_run.setEnabled(False)

        self.btn_execute = _make_action_button("⚡ 실행", "#e74c3c", "#c0392b")
        self.btn_execute.setToolTip(LEGACY_CLEANUP_EXECUTION_DISABLED_TOOLTIP)
        self.btn_execute.clicked.connect(lambda: self.execute_cleanup(dry_run=False))
        self.btn_execute.setEnabled(False)

        self.btn_select_all = QPushButton("전체 선택")
        self.btn_select_all.clicked.connect(self.select_all_orphans)

        # 쿼리 복사/내보내기 버튼 추가
        self.btn_copy_orphan_query = QPushButton("📋 조회쿼리 복사")
        self.btn_copy_orphan_query.setToolTip("선택된 고아 레코드의 조회 쿼리를 클립보드에 복사")
        self.btn_copy_orphan_query.clicked.connect(self.copy_orphan_query)
        self.btn_copy_orphan_query.setEnabled(False)

        self.btn_export_orphan_query = QPushButton("📄 조회쿼리 저장")
        self.btn_export_orphan_query.setToolTip("모든 고아 레코드 조회 쿼리를 .sql 파일로 저장")
        self.btn_export_orphan_query.clicked.connect(self.export_orphan_queries)
        self.btn_export_orphan_query.setEnabled(False)

        btn_layout.addWidget(self.btn_select_all)
        btn_layout.addWidget(self.btn_copy_orphan_query)
        btn_layout.addWidget(self.btn_export_orphan_query)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_dry_run)
        btn_layout.addWidget(self.btn_execute)

        cleanup_layout.addLayout(btn_layout)
        layout.addWidget(cleanup_group)

    def init_compatibility_tab(self):
        """호환성 이슈 탭 초기화"""
        layout = QVBoxLayout(self.tab_compatibility)

        # 필터
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("필터:"))

        self.chk_filter_error = QCheckBox("Error")
        self.chk_filter_error.setChecked(True)
        self.chk_filter_error.stateChanged.connect(self.filter_compatibility_issues)

        self.chk_filter_warning = QCheckBox("Warning")
        self.chk_filter_warning.setChecked(True)
        self.chk_filter_warning.stateChanged.connect(self.filter_compatibility_issues)

        self.chk_filter_info = QCheckBox("Info")
        self.chk_filter_info.setChecked(True)
        self.chk_filter_info.stateChanged.connect(self.filter_compatibility_issues)

        filter_layout.addWidget(self.chk_filter_error)
        filter_layout.addWidget(self.chk_filter_warning)
        filter_layout.addWidget(self.chk_filter_info)
        filter_layout.addStretch()

        # 자동 수정 위저드 버튼
        self.btn_auto_fix = _make_action_button("🔧 자동 수정 위저드", "#9b59b6", "#8e44ad")
        self.btn_auto_fix.setToolTip("자동 수정 가능한 이슈를 대화형 위저드로 수정합니다.")
        self.btn_auto_fix.setEnabled(False)  # 분석 완료 후 활성화
        self.btn_auto_fix.clicked.connect(self.open_fix_wizard)
        filter_layout.addWidget(self.btn_auto_fix)

        # 수동 처리 가이드 버튼
        self.btn_manual_guide = _make_action_button("📖 수동 처리 가이드", "#e67e22", "#d35400")
        self.btn_manual_guide.setToolTip("자동 수정이 불가능한 이슈에 대한 수동 처리 가이드를 제공합니다.")
        self.btn_manual_guide.setEnabled(False)
        self.btn_manual_guide.clicked.connect(self.show_manual_guide)
        filter_layout.addWidget(self.btn_manual_guide)

        layout.addLayout(filter_layout)

        # 이슈 테이블
        self.table_issues = QTableWidget()
        self.table_issues.setColumnCount(5)
        self.table_issues.setHorizontalHeaderLabels([
            "심각도", "유형", "위치", "설명", "권장 조치"
        ])
        self.table_issues.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table_issues.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table_issues)

    def init_fk_tree_tab(self):
        """FK 관계 트리 탭 초기화"""
        layout = QVBoxLayout(self.tab_fk_tree)

        # 트리 위젯
        self.tree_fk = QTreeWidget()
        self.tree_fk.setHeaderLabels(["테이블 (부모 → 자식)"])
        self.tree_fk.setAlternatingRowColors(True)
        layout.addWidget(self.tree_fk)

        # 텍스트 뷰 (ASCII 트리)
        self.txt_fk_tree = QTextEdit()
        self.txt_fk_tree.setReadOnly(True)
        self.txt_fk_tree.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 12px;
            }
        """)
        layout.addWidget(self.txt_fk_tree)

    def init_log_tab(self):
        """로그 탭 초기화"""
        layout = QVBoxLayout(self.tab_log)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 11px;
            }
        """)
        layout.addWidget(self.txt_log)

        # 로그 저장 버튼
        btn_layout = QHBoxLayout()
        btn_save_log = QPushButton("로그 저장")
        btn_save_log.clicked.connect(self.save_log)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save_log)
        layout.addLayout(btn_layout)

    def load_schemas(self):
        """스키마 목록 로드"""
        if not self.connector:
            return

        schemas = self.connector.get_schemas()
        self.combo_schema.clear()
        for schema in schemas:
            self.combo_schema.addItem(schema)

    def add_log(self, message: str):
        """로그 추가"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.txt_log.append(f"[{timestamp}] {message}")

    def save_log(self):
        """로그 저장"""
        filename, _ = QFileDialog.getSaveFileName(
            self, "로그 저장", f"migration_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt)"
        )
        if filename:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(self.txt_log.toPlainText())
            QMessageBox.information(self, "저장 완료", f"로그가 저장되었습니다:\n{filename}")

    def set_ui_enabled(self, enabled: bool):
        """UI 활성화/비활성화"""
        self.btn_analyze.setEnabled(enabled)
        self.combo_schema.setEnabled(enabled)
        self.chk_orphans.setEnabled(enabled)
        self.chk_charset.setEnabled(enabled)
        self.chk_keywords.setEnabled(enabled)
        self.chk_routines.setEnabled(enabled)
        self.chk_sql_mode.setEnabled(enabled)
        # MySQL 8.4 Upgrade Checker 옵션
        self.chk_auth_plugins.setEnabled(enabled)
        self.chk_zerofill.setEnabled(enabled)
        self.chk_float_precision.setEnabled(enabled)
        self.chk_fk_name_length.setEnabled(enabled)

    def start_oneclick_migration(self):
        """One-Click 마이그레이션 시작"""
        if not ONE_CLICK_MIGRATION_FEATURE_ENABLED:
            return

        schema = self.combo_schema.currentText()
        if not schema:
            QMessageBox.warning(self, "경고", "스키마를 선택하세요.")
            return

        from src.ui.dialogs.oneclick_migration_dialog import OneClickMigrationDialog

        # One-Click 마이그레이션 다이얼로그 실행
        dialog = OneClickMigrationDialog(self, self.connector, schema)
        dialog.exec()

    def start_analysis(self):
        """분석 시작"""
        schema = self.combo_schema.currentText()
        if not schema:
            QMessageBox.warning(self, "오류", "스키마를 선택하세요.")
            return

        self.set_ui_enabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # 무한 프로그레스

        self.add_log(f"📊 스키마 '{schema}' 분석 시작...")

        # 워커 생성 및 시작
        self.worker = MigrationAnalyzerWorker(
            connector=self.connector,
            schema=schema,
            options=MigrationCheckOptions(
                check_orphans=self.chk_orphans.isChecked(),
                check_charset=self.chk_charset.isChecked(),
                check_keywords=self.chk_keywords.isChecked(),
                check_routines=self.chk_routines.isChecked(),
                check_sql_mode=self.chk_sql_mode.isChecked(),
                check_auth_plugins=self.chk_auth_plugins.isChecked(),
                check_zerofill=self.chk_zerofill.isChecked(),
                check_float_precision=self.chk_float_precision.isChecked(),
                check_fk_name_length=self.chk_fk_name_length.isChecked(),
            )
        )

        self.worker.progress.connect(self.add_log)
        self.worker.analysis_complete.connect(self.on_analysis_complete)
        self.worker.finished.connect(self.on_analysis_finished)
        self.worker.start()

    def on_analysis_complete(self, result: AnalysisResult):
        """분석 완료 시"""
        if self._is_closing:
            return
        try:
            self.analysis_result = result
            self.update_overview(result)
            self.update_orphans_table(result.orphan_records)
            self.update_compatibility_table(result.compatibility_issues)
            self.update_fk_tree(result.fk_tree, result.schema)

            # 백그라운드 자동 저장 (기록 보관용)
            self._auto_save_result(result)

            # 저장 버튼 활성화
            self.btn_save.setEnabled(True)
            # 자동/수동 버튼 활성화
            self._update_fix_buttons(result.compatibility_issues)
        except Exception as e:
            logger.error(f"분석 결과 UI 업데이트 오류: {e}", exc_info=True)
            QMessageBox.critical(self, "오류", f"분석 결과 표시 중 오류 발생:\n{e}")

    def on_analysis_finished(self, success: bool, message: str):
        """분석 종료 시"""
        if self._is_closing:
            return
        self.set_ui_enabled(True)
        self.progress_bar.setVisible(False)

        if success:
            self.add_log(f"✅ {message}")
        else:
            self.add_log(f"❌ {message}")
            QMessageBox.critical(self, "분석 오류", message)

    def update_overview(self, result: AnalysisResult):
        """개요 탭 업데이트"""
        # 요약 텍스트
        orphan_count = sum(o.orphan_count for o in result.orphan_records)
        error_count = sum(1 for i in result.compatibility_issues if i.severity == "error")
        warning_count = sum(1 for i in result.compatibility_issues if i.severity == "warning")

        summary = f"""
<h3>📊 분석 결과: {result.schema}</h3>
<p><b>분석 시각:</b> {result.analyzed_at}</p>
<p><b>테이블 수:</b> {result.total_tables}개</p>
<p><b>FK 관계:</b> {result.total_fk_relations}개</p>
<hr>
<p><b>🔗 고아 레코드:</b> {len(result.orphan_records)}개 FK 관계에서 총 {orphan_count:,}개 발견</p>
<p><b>❌ 오류:</b> {error_count}개</p>
<p><b>⚠️ 경고:</b> {warning_count}개</p>
"""
        self.lbl_summary.setText(summary)

        # 통계 테이블
        stats = [
            ("스키마", result.schema),
            ("분석 시각", result.analyzed_at),
            ("테이블 수", str(result.total_tables)),
            ("FK 관계 수", str(result.total_fk_relations)),
            ("고아 레코드 FK 관계", str(len(result.orphan_records))),
            ("총 고아 레코드 수", f"{orphan_count:,}"),
            ("호환성 오류", str(error_count)),
            ("호환성 경고", str(warning_count)),
        ]

        self.table_stats.setRowCount(len(stats))
        for i, (key, value) in enumerate(stats):
            self.table_stats.setItem(i, 0, QTableWidgetItem(key))
            self.table_stats.setItem(i, 1, QTableWidgetItem(value))

    def update_orphans_table(self, orphans: List[OrphanRecord]):
        """고아 레코드 테이블 업데이트"""
        self.table_orphans.setRowCount(len(orphans))

        for i, orphan in enumerate(orphans):
            self.table_orphans.setItem(i, 0, QTableWidgetItem(orphan.child_table))
            self.table_orphans.setItem(i, 1, QTableWidgetItem(orphan.child_column))
            self.table_orphans.setItem(i, 2, QTableWidgetItem(orphan.parent_table))
            self.table_orphans.setItem(i, 3, QTableWidgetItem(orphan.parent_column))

            count_item = QTableWidgetItem(f"{orphan.orphan_count:,}")
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if orphan.orphan_count > ORPHAN_COUNT_CRITICAL_THRESHOLD:
                count_item.setForeground(QColor("#e74c3c"))
            elif orphan.orphan_count > ORPHAN_COUNT_WARNING_THRESHOLD:
                count_item.setForeground(QColor("#f39c12"))
            self.table_orphans.setItem(i, 4, count_item)

            samples = ", ".join(str(v) for v in orphan.sample_values[:3])
            if len(orphan.sample_values) > 3:
                samples += "..."
            self.table_orphans.setItem(i, 5, QTableWidgetItem(samples))

        self.btn_dry_run.setEnabled(len(orphans) > 0)
        self.btn_execute.setEnabled(False)
        self.btn_export_orphan_query.setEnabled(len(orphans) > 0)

    def update_compatibility_table(self, issues: List[CompatibilityIssue]):
        """호환성 이슈 테이블 업데이트"""
        self._all_issues = issues  # 필터링용 저장
        self.filter_compatibility_issues()

    def filter_compatibility_issues(self):
        """호환성 이슈 필터링"""
        if not hasattr(self, '_all_issues'):
            return

        show_error = self.chk_filter_error.isChecked()
        show_warning = self.chk_filter_warning.isChecked()
        show_info = self.chk_filter_info.isChecked()

        filtered = []
        for issue in self._all_issues:
            if issue.severity == "error" and show_error:
                filtered.append(issue)
            elif issue.severity == "warning" and show_warning:
                filtered.append(issue)
            elif issue.severity == "info" and show_info:
                filtered.append(issue)

        # UI 업데이트 최적화 - 일괄 업데이트
        self.table_issues.setUpdatesEnabled(False)
        self.table_issues.setRowCount(len(filtered))

        severity_icons = {
            "error": "❌",
            "warning": "⚠️",
            "info": "ℹ️"
        }

        for i, issue in enumerate(filtered):
            severity_item = QTableWidgetItem(f"{severity_icons.get(issue.severity, '')} {issue.severity.upper()}")
            if issue.severity == "error":
                severity_item.setForeground(QColor("#e74c3c"))
            elif issue.severity == "warning":
                severity_item.setForeground(QColor("#f39c12"))

            self.table_issues.setItem(i, 0, severity_item)
            self.table_issues.setItem(i, 1, QTableWidgetItem(ISSUE_TYPE_DISPLAY_NAMES.get(issue.issue_type, str(issue.issue_type))))
            self.table_issues.setItem(i, 2, QTableWidgetItem(issue.location))
            self.table_issues.setItem(i, 3, QTableWidgetItem(issue.description))
            self.table_issues.setItem(i, 4, QTableWidgetItem(issue.suggestion))

        # UI 업데이트 재활성화
        self.table_issues.setUpdatesEnabled(True)

    def update_fk_tree(self, fk_tree: Dict[str, List[str]], schema: str):
        """FK 트리 업데이트 (분석 결과 fk_tree 데이터만 사용, 동기 DB 재조회 없음)"""
        # schema는 호출부 호환을 위해 유지되며 여기서는 사용하지 않는다 (DB 접근 금지)
        self.tree_fk.clear()

        if not fk_tree:
            self.txt_fk_tree.setText("FK 관계가 없습니다.")
            return

        item_stack: List[QTreeWidgetItem] = []
        for table, depth, is_cycle, _is_last in iter_fk_tree(fk_tree):
            if depth == 0:
                root_item = QTreeWidgetItem(self.tree_fk, [f"📁 {table}"])
                item_stack = [root_item]
                continue

            parent_item = item_stack[depth - 1]
            label = f"🔄 {table} (순환 참조)" if is_cycle else f"└── {table}"
            child_item = QTreeWidgetItem(parent_item, [label])
            if not is_cycle:
                if len(item_stack) <= depth:
                    item_stack.append(child_item)
                else:
                    item_stack[depth] = child_item
                    del item_stack[depth + 1:]

        self.tree_fk.expandAll()

        # ASCII 트리 텍스트 - 워커 분석 결과(fk_tree)만으로 렌더링, 동기 DB 재조회 없음
        self.txt_fk_tree.setText(_format_fk_tree_text(fk_tree))

    def on_orphan_selected(self):
        """고아 레코드 선택 시"""
        selected_rows = self.table_orphans.selectionModel().selectedRows()

        if not selected_rows or not self.analysis_result:
            self.txt_cleanup_sql.clear()
            self.btn_copy_orphan_query.setEnabled(False)
            return

        self.btn_copy_orphan_query.setEnabled(True)

        # 선택된 고아 레코드들에 대한 SQL 생성
        sql_parts = []
        schema = self.analysis_result.schema
        action = ActionType.DELETE if self.radio_delete.isChecked() else ActionType.SET_NULL

        analyzer = MigrationAnalyzer(self.connector)

        for row_index in selected_rows:
            row = row_index.row()
            if row < len(self.analysis_result.orphan_records):
                orphan = self.analysis_result.orphan_records[row]
                cleanup = analyzer.generate_cleanup_sql(orphan, action, schema, dry_run=True)
                sql_parts.append(f"-- {cleanup.description}\n{cleanup.sql};")

        self.txt_cleanup_sql.setText("\n\n".join(sql_parts))

    def select_all_orphans(self):
        """모든 고아 레코드 선택"""
        self.table_orphans.selectAll()

    def _generate_orphan_select_query(self, orphan: OrphanRecord, schema: str) -> str:
        """고아 레코드 조회 쿼리 생성"""
        return build_orphan_select_sql(orphan, schema)

    def copy_orphan_query(self):
        """선택된 고아 레코드 조회 쿼리 복사"""
        if not self.analysis_result:
            return

        selected_rows = self.table_orphans.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "선택 필요", "복사할 고아 레코드를 선택하세요.")
            return

        schema = self.analysis_result.schema
        queries = []

        for row_index in selected_rows:
            row = row_index.row()
            if row < len(self.analysis_result.orphan_records):
                orphan = self.analysis_result.orphan_records[row]
                queries.append(build_orphan_select_sql(orphan, schema))

        clipboard = QApplication.clipboard()
        clipboard.setText("\n\n".join(queries))

        QMessageBox.information(
            self, "복사 완료",
            f"✅ {len(queries)}개 조회 쿼리가 클립보드에 복사되었습니다."
        )

    def export_orphan_queries(self):
        """모든 고아 레코드 조회 쿼리를 파일로 저장"""
        if not self.analysis_result or not self.analysis_result.orphan_records:
            QMessageBox.warning(self, "데이터 없음", "내보낼 고아 레코드가 없습니다.")
            return

        schema = self.analysis_result.schema
        orphans = self.analysis_result.orphan_records
        total_count = sum(o.orphan_count for o in orphans)

        # 파일 저장 다이얼로그
        default_name = f"orphan_queries_{schema}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "고아 레코드 조회 쿼리 저장",
            default_name,
            "SQL 파일 (*.sql);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            self.result_store.export_orphan_queries(
                schema=schema,
                orphans=orphans,
                path=file_path,
                query_builder=build_orphan_select_sql,
            )

            QMessageBox.information(
                self, "저장 완료",
                f"✅ 조회 쿼리가 저장되었습니다.\n\n"
                f"파일: {file_path}\n"
                f"FK 관계: {len(orphans)}개\n"
                f"총 고아 레코드: {total_count:,}개"
            )
        except Exception as e:
            QMessageBox.critical(
                self, "저장 실패",
                f"❌ 파일 저장 중 오류가 발생했습니다.\n\n{str(e)}"
            )

    def execute_cleanup(self, dry_run: bool = True):
        """정리 작업 실행"""
        if not self.analysis_result:
            return

        if not dry_run:
            QMessageBox.warning(
                self,
                "실행 비활성화",
                LEGACY_CLEANUP_EXECUTION_DISABLED_TOOLTIP
            )
            return

        selected_rows = self.table_orphans.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "선택 필요", "정리할 고아 레코드를 선택하세요.")
            return

        # 정리 작업 목록 생성
        schema = self.analysis_result.schema
        action = ActionType.DELETE if self.radio_delete.isChecked() else ActionType.SET_NULL
        analyzer = MigrationAnalyzer(self.connector)

        actions = []
        for row_index in selected_rows:
            row = row_index.row()
            if row < len(self.analysis_result.orphan_records):
                orphan = self.analysis_result.orphan_records[row]
                cleanup = analyzer.generate_cleanup_sql(orphan, action, schema, dry_run=dry_run)
                actions.append(cleanup)

        # UI 비활성화
        self.btn_dry_run.setEnabled(False)
        self.btn_execute.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)

        mode = "DRY-RUN" if dry_run else "실행"
        self.add_log(f"🔧 [{mode}] 정리 작업 시작 ({len(actions)}개)")

        # 워커 실행
        self.cleanup_worker = CleanupWorker(
            connector=self.connector,
            schema=schema,
            actions=actions,
        )

        self.cleanup_worker.progress.connect(self.add_log)
        self.cleanup_worker.action_complete.connect(self.on_action_complete)
        self.cleanup_worker.finished.connect(self.on_cleanup_finished)
        self.cleanup_worker.start()

    def on_action_complete(self, table: str, success: bool, message: str, affected: int):
        """개별 정리 작업 완료 시"""
        if self._is_closing:
            return
        status = "✅" if success else "❌"
        self.add_log(f"  {status} {table}: {message}")

    def on_cleanup_finished(self, success: bool, message: str, results: dict):
        """정리 작업 완료 시"""
        if self._is_closing:
            return
        self.btn_dry_run.setEnabled(True)
        self.btn_execute.setEnabled(False)
        self.progress_bar.setVisible(False)

        self.add_log(message)

        # 결과 요약
        total_affected = sum(r.get('affected_rows', 0) for r in results.values())
        success_count = sum(1 for r in results.values() if r.get('success'))
        fail_count = len(results) - success_count

        QMessageBox.information(
            self,
            "작업 완료",
            f"정리 작업이 완료되었습니다.\n\n"
            f"성공: {success_count}개\n"
            f"실패: {fail_count}개\n"
            f"영향받은 행: {total_affected:,}개"
        )

    # =========================================================================
    # 자동 수정 위저드 / 수동 처리 가이드
    # =========================================================================

    # 자동 수정 가능한 이슈 타입 (공유 단일 소스 참조)
    AUTO_FIXABLE_TYPES = AUTO_FIXABLE_ISSUE_TYPES

    def _update_fix_buttons(self, issues: list):
        """자동 수정 / 수동 가이드 버튼 활성화 상태 업데이트"""
        auto_fixable = [i for i in issues if i.issue_type in self.AUTO_FIXABLE_TYPES]
        manual_only = [i for i in issues if i.issue_type not in self.AUTO_FIXABLE_TYPES]

        self.btn_auto_fix.setEnabled(len(auto_fixable) > 0)
        self.btn_manual_guide.setEnabled(len(manual_only) > 0)

        # 버튼 텍스트에 개수 표시
        if auto_fixable:
            self.btn_auto_fix.setText(f"🔧 자동 수정 위저드 ({len(auto_fixable)})")
        else:
            self.btn_auto_fix.setText("🔧 자동 수정 위저드")

        if manual_only:
            self.btn_manual_guide.setText(f"📖 수동 처리 가이드 ({len(manual_only)})")
        else:
            self.btn_manual_guide.setText("📖 수동 처리 가이드")

    def open_fix_wizard(self):
        """자동 수정 위저드 열기 (자동 수정 가능 이슈만)"""
        if not self.analysis_result:
            QMessageBox.warning(self, "분석 필요", "먼저 스키마 분석을 실행하세요.")
            return

        # 자동 수정 가능 이슈만 필터링
        auto_fixable_issues = [
            i for i in self.analysis_result.compatibility_issues
            if i.issue_type in self.AUTO_FIXABLE_TYPES
        ]

        if not auto_fixable_issues:
            QMessageBox.information(self, "이슈 없음", "자동 수정 가능한 이슈가 없습니다.")
            return

        try:
            from src.ui.dialogs.fix_wizard_dialog import FixWizardDialog

            wizard = FixWizardDialog(
                parent=self,
                connector=self.connector,
                issues=auto_fixable_issues,  # 자동 수정 가능 이슈만 전달
                schema=self.analysis_result.schema
            )
            result = wizard.exec()

            if result:
                # 위저드 완료 후 재분석 권장
                reply = QMessageBox.question(
                    self,
                    "재분석",
                    "수정이 완료되었습니다. 변경사항을 확인하기 위해 재분석하시겠습니까?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self.start_analysis()

        except ImportError as e:
            logger.error(f"자동 수정 위저드 모듈 로드 실패: {e}", exc_info=True)
            QMessageBox.critical(self, "오류", f"자동 수정 위저드를 불러올 수 없습니다:\n{e}")
        except Exception as e:
            logger.error(f"자동 수정 위저드 오류: {e}", exc_info=True)
            QMessageBox.critical(self, "오류", f"자동 수정 위저드 실행 중 오류:\n{e}")

    def show_manual_guide(self):
        """수동 처리 가이드 다이얼로그 열기"""
        if not self.analysis_result:
            QMessageBox.warning(self, "분석 필요", "먼저 스키마 분석을 실행하세요.")
            return

        # 수동 처리 필요 이슈만 필터링
        manual_issues = [
            i for i in self.analysis_result.compatibility_issues
            if i.issue_type not in self.AUTO_FIXABLE_TYPES
        ]

        if not manual_issues:
            QMessageBox.information(self, "이슈 없음", "수동 처리가 필요한 이슈가 없습니다.")
            return

        try:
            dialog = ManualGuideDialog(manual_issues, self)
            dialog.exec()
        except Exception as e:
            logger.error(f"수동 처리 가이드 오류: {e}", exc_info=True)
            QMessageBox.critical(self, "오류", f"수동 처리 가이드 표시 중 오류:\n{e}")

    # =========================================================================
    # 분석 결과 저장/로드
    # =========================================================================

    def _get_analysis_dir(self) -> str:
        """분석 결과 저장 디렉토리"""
        return str(self.result_store.analysis_dir())

    def _auto_save_result(self, result: AnalysisResult):
        """분석 결과 자동 저장 (백그라운드, 기록 보관용)"""
        try:
            auto_save_path = self.result_store.auto_save(result)

            self._auto_saved_path = str(auto_save_path)
            self.add_log(f"💾 분석 결과 자동 저장: {auto_save_path}")
            logger.info(f"분석 결과 자동 저장 완료: {auto_save_path}")

        except Exception as e:
            logger.error(f"분석 결과 자동 저장 오류: {e}", exc_info=True)
            self._auto_saved_path = None

    def save_analysis_result(self):
        """분석 결과 저장 (자동 저장 파일을 복사)"""
        if not self.analysis_result:
            QMessageBox.warning(self, "저장 오류", "저장할 분석 결과가 없습니다.")
            return

        # 자동 저장된 파일이 없으면 직접 저장
        if not self._auto_saved_path or not Path(self._auto_saved_path).exists():
            self._save_result_directly()
            return

        # 기본 파일명 생성
        default_name = Path(self._auto_saved_path).name

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "분석 결과 저장",
            default_name,
            "JSON 파일 (*.json);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            self.result_store.write(self.analysis_result, file_path)

            self.add_log(f"💾 분석 결과 복사 완료: {file_path}")
            QMessageBox.information(self, "저장 완료", f"분석 결과가 저장되었습니다.\n\n{file_path}")

        except Exception as e:
            logger.error(f"분석 결과 복사 오류: {e}", exc_info=True)
            QMessageBox.critical(self, "저장 오류", f"파일 저장 실패:\n{e}")

    def _save_result_directly(self):
        """분석 결과 직접 저장 (자동 저장 실패 시 fallback)"""
        default_name = self.result_store.default_name(self.analysis_result.schema)

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "분석 결과 저장",
            default_name,
            "JSON 파일 (*.json);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            self.result_store.write(self.analysis_result, file_path)

            self.add_log(f"💾 분석 결과 저장 완료: {file_path}")
            QMessageBox.information(self, "저장 완료", f"분석 결과가 저장되었습니다.\n\n{file_path}")

        except Exception as e:
            logger.error(f"분석 결과 저장 오류: {e}", exc_info=True)
            QMessageBox.critical(self, "저장 오류", f"파일 저장 실패:\n{e}")

    def load_analysis_result(self):
        """분석 결과 불러오기"""
        default_dir = self._get_analysis_dir()

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "분석 결과 불러오기",
            default_dir,
            "JSON 파일 (*.json);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            result = self.result_store.read(file_path)

            # UI 업데이트
            self.analysis_result = result
            self.combo_schema.setCurrentText(result.schema)
            self.update_overview(result)
            self.update_orphans_table(result.orphan_records)
            self.update_compatibility_table(result.compatibility_issues)
            self.update_fk_tree(result.fk_tree, result.schema)
            self.btn_save.setEnabled(True)
            # 자동/수동 버튼 활성화
            self._update_fix_buttons(result.compatibility_issues)

            self.add_log(f"📂 분석 결과 불러오기 완료: {file_path}")
            self.add_log(f"   스키마: {result.schema}, 분석일시: {result.analyzed_at}")
            QMessageBox.information(
                self,
                "불러오기 완료",
                f"분석 결과를 불러왔습니다.\n\n"
                f"스키마: {result.schema}\n"
                f"분석일시: {result.analyzed_at}\n"
                f"테이블: {result.total_tables}개\n"
                f"FK 관계: {result.total_fk_relations}개"
            )

        except Exception as e:
            logger.error(f"분석 결과 불러오기 오류: {e}", exc_info=True)
            QMessageBox.critical(self, "불러오기 오류", f"파일 불러오기 실패:\n{e}")




class MigrationWizard:
    """마이그레이션 분석 위저드"""

    @staticmethod
    def start(parent=None, tunnel_engine=None, config_manager=None) -> bool:
        """
        마이그레이션 분석 시작

        Args:
            parent: 부모 위젯
            tunnel_engine: TunnelEngine 인스턴스
            config_manager: ConfigManager 인스턴스

        Returns:
            성공 여부
        """
        from src.ui.dialogs.db_dialogs import DBConnectionDialog

        # 1단계: DB 연결
        conn_dialog = DBConnectionDialog(parent, tunnel_engine, config_manager)
        if conn_dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        connector = conn_dialog.connector
        if not connector:
            return False

        # 2단계: 마이그레이션 분석 다이얼로그
        analyzer_dialog = None
        try:
            analyzer_dialog = MigrationAnalyzerDialog(parent, connector, config_manager)
            analyzer_dialog.exec()
            return True
        finally:
            # 다이얼로그가 닫히면서 백그라운드 Worker 완료 시점으로 연결 해제를 위임한 경우,
            # 여기서 다시 동기적으로 disconnect()하지 않는다 (이중 해제/경합 방지).
            deferred = getattr(analyzer_dialog, "disconnect_deferred_to_worker_completion", False)
            if connector and not deferred:
                connector.disconnect()
