"""MySQL <-> PostgreSQL migration dialog."""
import json
from typing import Any, Dict, List, Optional, cast

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.core.cross_engine_migration import (
    DatabaseEngine,
    MigrationDirection,
    load_resume_state,
    next_workflow_command,
    make_connection_payload,
    render_result_report,
    save_resume_state,
    schema_from_inspect_result,
    state_key_from_payload,
)
from src.ui.workers.cross_engine_migration_worker import CrossEngineMigrationWorker


class CrossEngineMigrationDialog(QDialog):
    """Wizard-style dialog for DB engine conversion."""

    def __init__(self, parent=None, tunnel_engine=None, config_manager=None):
        super().__init__(parent)
        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager
        self.worker: Optional[CrossEngineMigrationWorker] = None
        self.last_result: Optional[Dict] = None
        self.unsupported_objects = []
        self._last_checkpoint_path = None
        self._workflow_active = False
        self._current_command: Optional[str] = None
        self._pending_after_inspect: Optional[str] = None
        self._execution_unlocked = False
        self._verify_result_received = False
        self._safety_activity_base = ""
        self._safety_activity_dots = 0
        self.safety_activity_timer = QTimer(self)
        self.safety_activity_timer.setInterval(450)
        self.safety_activity_timer.timeout.connect(self._tick_safety_activity)
        self.step_ids: List[str] = [
            "connections",
            "inspect",
            "safety",
            "plan",
            "execute",
            "verify",
        ]
        self.step_titles: List[str] = [
            "1. 연결 선택",
            "2. Source 구조 분석",
            "3. 전환 가능 여부 점검",
            "4. 실행 계획 확인",
            "5. 승인 및 전환 실행",
            "6. 검증 및 결과 저장",
        ]
        self.current_step_id = self.step_ids[0]
        self.step_pages: Dict[str, QWidget] = {}
        self.step_page_layouts: Dict[str, QVBoxLayout] = {}
        self._step_completed: Dict[str, bool] = {
            "inspect": False,
            "safety": False,
            "plan": False,
            "execute": False,
            "verify": False,
        }
        self.setWindowTitle("DB 전환 마법사")
        self.resize(900, 700)
        self._setup_ui()

    def _apply_wizard_style(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #f8fafc;
            }
            QGroupBox {
                background-color: #ffffff;
                border: 1px solid #d0d5dd;
                border-radius: 6px;
                margin-top: 10px;
                padding: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
                color: #101828;
                font-weight: 600;
            }
            QLabel#StepHelp {
                color: #344054;
                font-weight: 600;
                padding: 8px 0;
            }
            QLabel#MutedHelp {
                color: #667085;
            }
            QPushButton {
                min-height: 26px;
                padding: 4px 12px;
                border: 1px solid #98a2b3;
                border-radius: 3px;
                background-color: #ffffff;
                color: #182230;
            }
            QPushButton:hover:enabled {
                background-color: #f2f4f7;
            }
            QPushButton:disabled {
                background-color: #e4e7ec;
                color: #98a2b3;
                border: 1px solid #d0d5dd;
            }
            QPushButton#WizardNextButton {
                background-color: #2563eb;
                border: 1px solid #1d4ed8;
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton#WizardNextButton:hover:enabled {
                background-color: #1d4ed8;
            }
            QPushButton#WizardNextButton:disabled {
                background-color: #e4e7ec;
                color: #98a2b3;
                border: 1px solid #d0d5dd;
                font-weight: 600;
            }
            QPushButton#WizardBackButton:disabled {
                background-color: #f2f4f7;
                color: #98a2b3;
                border: 1px solid #e4e7ec;
            }
            QPushButton#PrimaryActionButton {
                background-color: #16a34a;
                border: 1px solid #15803d;
                color: #ffffff;
                font-weight: 600;
                min-height: 32px;
            }
            QPushButton#PrimaryActionButton:hover:enabled {
                background-color: #15803d;
            }
            QPushButton#PrimaryActionButton:disabled {
                background-color: #dcfce7;
                color: #86efac;
                border: 1px solid #bbf7d0;
            }
            QLabel#NextHint {
                color: #475467;
                font-weight: 600;
            }
        """)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        self._apply_wizard_style()

        endpoint_layout = QHBoxLayout()
        self.source_form = EndpointForm(
            "Source",
            DatabaseEngine.MYSQL,
            self.tunnel_engine,
            self.config_manager,
            require_tunnel=True,
        )
        self.target_form = EndpointForm(
            "Target",
            DatabaseEngine.POSTGRESQL,
            self.tunnel_engine,
            self.config_manager,
            require_tunnel=True,
        )
        endpoint_layout.addWidget(self.source_form)
        endpoint_layout.addWidget(self.target_form)
        self.lbl_direction_summary = QLabel()
        self.lbl_direction_summary.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.lbl_direction_summary)

        self.page_container = QWidget()
        self.page_layout = QVBoxLayout(self.page_container)
        layout.addWidget(self.page_container, 1)
        for step_id in self.step_ids:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            self.step_pages[step_id] = page
            self.step_page_layouts[step_id] = page_layout
            self.page_layout.addWidget(page)
        self.step_page_layouts["connections"].addLayout(endpoint_layout)

        option_group = QGroupBox("실행 옵션")
        option_layout = QHBoxLayout(option_group)
        self.chk_create_only = QCheckBox("create_only")
        self.chk_create_only.setChecked(True)
        self.spin_chunk_size = QSpinBox()
        self.spin_chunk_size.setRange(100, 100000)
        self.spin_chunk_size.setSingleStep(1000)
        self.spin_chunk_size.setValue(10000)
        self.spin_guide_row_limit = QSpinBox()
        self.spin_guide_row_limit.setRange(1, 1000)
        self.spin_guide_row_limit.setValue(20)
        option_layout.addWidget(self.chk_create_only)
        option_layout.addWidget(QLabel("Chunk size:"))
        option_layout.addWidget(self.spin_chunk_size)
        option_layout.addWidget(QLabel("Guide rows:"))
        option_layout.addWidget(self.spin_guide_row_limit)
        option_layout.addStretch()

        schema_group = QGroupBox("스키마 검사 결과")
        schema_layout = QVBoxLayout(schema_group)
        schema_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        load_layout = QHBoxLayout()
        self.lbl_inspect_step_help = QLabel(
            "Source 자동 검사를 실행해 Rust Core가 구조를 분석해야 합니다. 완료되면 다음 단계로 이동할 수 있습니다."
        )
        self.lbl_inspect_step_help.setObjectName("StepHelp")
        self.lbl_inspect_step_help.setWordWrap(True)
        schema_layout.addWidget(self.lbl_inspect_step_help)
        self.lbl_schema_status = QLabel("Source DB를 검사하면 Rust Core가 정규화한 스키마가 자동으로 채워집니다.")
        self.lbl_schema_status.setObjectName("MutedHelp")
        schema_layout.addWidget(self.lbl_schema_status)
        self.lbl_source_summary = QLabel("아직 Source 구조를 분석하지 않았습니다.")
        self.lbl_source_summary.setObjectName("MutedHelp")
        self.lbl_source_summary.setWordWrap(True)
        schema_layout.addWidget(self.lbl_source_summary)
        self.chk_show_schema_json = QCheckBox("고급 설정: schema JSON 및 수동 검사 도구 보기")
        self.chk_show_schema_json.setChecked(False)
        schema_layout.addWidget(self.chk_show_schema_json)
        self.btn_load_schema = QPushButton("JSON 불러오기")
        self.btn_auto_inspect = QPushButton("Source 구조 분석 시작")
        self.btn_auto_inspect.setObjectName("PrimaryActionButton")
        self.btn_load_schema.clicked.connect(self._load_schema_json)
        self.btn_auto_inspect.clicked.connect(lambda: self._start_command("inspect"))
        self.btn_auto_inspect.setToolTip("선택한 Source DB를 Rust Core로 분석합니다.")
        load_layout.addWidget(self.btn_auto_inspect)
        load_layout.addStretch()
        schema_layout.addLayout(load_layout)
        self.schema_advanced_layout = QHBoxLayout()
        self.schema_advanced_layout.addWidget(self.btn_load_schema)
        self.schema_advanced_layout.addStretch()
        schema_layout.addLayout(self.schema_advanced_layout)
        self.txt_schema = QPlainTextEdit()
        self.txt_schema.setPlaceholderText('{"tables":[{"name":"users","columns":[{"name":"id","type":"int(11)","nullable":false,"primary_key":true}]}]}')
        self.txt_schema.setPlainText('{"tables":[]}')
        self.txt_schema.setVisible(False)
        self.chk_show_schema_json.toggled.connect(self._set_schema_advanced_visible)
        schema_layout.addWidget(self.txt_schema)
        self.step_page_layouts["inspect"].addWidget(schema_group, 1)

        self.execution_group = QGroupBox("승인 및 전환 실행")
        execution_layout = QVBoxLayout(self.execution_group)
        self.lbl_execution_warning = QLabel("대상 schema 이름을 정확히 입력해야 DB 변경 실행이 활성화됩니다.")
        self.lbl_execution_warning.setWordWrap(True)
        self.input_approval_schema = QLineEdit()
        self.input_approval_schema.setPlaceholderText("Target schema 이름 입력")
        self.lbl_execution_phase = QLabel("실행 전")
        self.lbl_current_table = QLabel("현재 테이블: -")
        self.lbl_current_rows = QLabel("현재 rows: -")
        self.lbl_migration_result = QLabel("")
        self.lbl_migration_result.setWordWrap(True)
        self.lbl_migration_result.hide()
        execution_layout.addWidget(self.lbl_execution_warning)
        execution_layout.addWidget(self.input_approval_schema)
        execution_layout.addWidget(self.lbl_execution_phase)
        execution_layout.addWidget(self.lbl_current_table)
        execution_layout.addWidget(self.lbl_current_rows)
        execution_layout.addWidget(self.lbl_migration_result)
        self.step_page_layouts["execute"].addWidget(self.execution_group)

        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(1000)
        self.step_page_layouts["execute"].addWidget(self.txt_log, 1)

        self.safety_group = QGroupBox("전환 가능 여부 점검")
        safety_layout = QVBoxLayout(self.safety_group)
        safety_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.lbl_safety_summary = QLabel("아직 전환 가능 여부를 점검하지 않았습니다.")
        self.lbl_safety_summary.setWordWrap(True)
        self.lbl_target_safety = QLabel("Target 상태를 아직 확인하지 않았습니다.")
        self.lbl_target_safety.setWordWrap(True)
        self.lbl_safety_activity = QLabel("대기 중")
        self.lbl_safety_activity.setObjectName("MutedHelp")
        self.lbl_safety_activity.setWordWrap(True)
        self.safety_activity_bar = QProgressBar()
        self.safety_activity_bar.setRange(0, 0)
        self.safety_activity_bar.hide()
        self.txt_safety_log = QPlainTextEdit()
        self.txt_safety_log.setReadOnly(True)
        self.txt_safety_log.setMaximumBlockCount(80)
        self.txt_safety_log.setFixedHeight(110)
        self.txt_safety_log.setPlaceholderText("전환 가능 여부 점검의 최근 진행 상황이 표시됩니다.")
        self.target_advanced_panel = QWidget()
        target_advanced_layout = QVBoxLayout(self.target_advanced_panel)
        target_advanced_layout.setContentsMargins(0, 0, 0, 0)
        self.lbl_target_advanced_help = QLabel(
            "DB 변경 실행 직전에 기존 Target 테이블을 정리하도록 계획할 수 있습니다. "
            "실행 버튼을 누르기 전까지 DB는 변경되지 않습니다."
        )
        self.lbl_target_advanced_help.setObjectName("MutedHelp")
        self.lbl_target_advanced_help.setWordWrap(True)
        self.chk_cleanup_before_migrate = QCheckBox("DB 변경 실행 직전에 기존 Target 테이블 정리")
        self.chk_cleanup_before_migrate.toggled.connect(self._on_cleanup_before_migrate_toggled)
        target_advanced_layout.addWidget(self.lbl_target_advanced_help)
        target_advanced_layout.addWidget(self.chk_cleanup_before_migrate)
        self.target_advanced_panel.hide()
        self.btn_target_advanced = QPushButton("고급 설정 열기")
        self.btn_target_advanced.hide()
        self.btn_target_advanced.clicked.connect(self._open_target_advanced_options)
        self.btn_run_safety = QPushButton("전환 가능 여부 점검")
        self.btn_run_safety.clicked.connect(lambda: self._start_command("preflight"))
        safety_layout.addWidget(self.lbl_safety_summary)
        safety_layout.addWidget(self.lbl_target_safety)
        safety_layout.addWidget(self.lbl_safety_activity)
        safety_layout.addWidget(self.safety_activity_bar)
        safety_layout.addWidget(self.txt_safety_log)
        safety_layout.addWidget(self.target_advanced_panel)
        safety_layout.addWidget(self.btn_target_advanced)
        safety_layout.addWidget(self.btn_run_safety)
        self.step_page_layouts["safety"].addWidget(self.safety_group)

        action_group = QGroupBox("작업 순서")
        action_layout = QVBoxLayout(action_group)
        self.lbl_execution_lock = QLabel("DB 변경 실행은 사전 점검 또는 계획 생성 성공 후 활성화됩니다.")
        action_layout.addWidget(self.lbl_execution_lock)
        self.lbl_next_hint = QLabel("")
        self.lbl_next_hint.setObjectName("NextHint")
        self.lbl_next_hint.setWordWrap(True)
        action_layout.addWidget(self.lbl_next_hint)

        control_layout = QHBoxLayout()

        self.btn_full_run = QPushButton("전체 실행")
        self.btn_inspect = QPushButton("스키마 검사")
        self.btn_preflight = QPushButton("사전 점검")
        self.btn_readiness = QPushButton("양방향 점검")
        self.btn_guide = QPushButton("상세 가이드")
        self.btn_plan = QPushButton("계획 생성")
        self.btn_run_plan = self.btn_plan
        self.btn_migrate = QPushButton("DB 변경 실행")
        self.btn_resume = QPushButton("중단 지점부터 재개")
        self.btn_cleanup_failed = QPushButton("실패한 전환 정리")
        self.btn_verify = QPushButton("검증")
        self.btn_save_report = QPushButton("결과 저장")
        self.btn_cancel = QPushButton("취소")
        self.btn_close = QPushButton("닫기")
        self.btn_previous = QPushButton("이전")
        self.btn_next = QPushButton("다음")
        self.btn_previous.setObjectName("WizardBackButton")
        self.btn_next.setObjectName("WizardNextButton")
        self.btn_full_run.hide()
        self.btn_migrate.hide()
        self.btn_migrate.setToolTip("대상 DB에 스키마 생성과 데이터 적재를 실행합니다.")
        self.btn_resume.setToolTip("저장된 상태부터 대상 DB 변경 작업을 재개합니다.")
        self.btn_cleanup_failed.setToolTip("실패한 전환에서 생성된 Target 테이블을 정리합니다.")
        self.btn_cleanup_failed.hide()
        self.btn_migrate.setStyleSheet(
            "QPushButton { background-color: #b42318; color: white; font-weight: 600; }"
            "QPushButton:disabled { background-color: #d0d5dd; color: #667085; }"
        )

        self.btn_full_run.clicked.connect(self._start_full_workflow)
        self.btn_inspect.clicked.connect(lambda: self._start_command("inspect"))
        self.btn_preflight.clicked.connect(lambda: self._start_command("preflight"))
        self.btn_readiness.clicked.connect(lambda: self._start_command("readiness"))
        self.btn_guide.clicked.connect(lambda: self._start_command("guide"))
        self.btn_plan.clicked.connect(lambda: self._start_command("plan"))
        self.btn_migrate.clicked.connect(lambda: self._start_command("migrate"))
        self.btn_resume.clicked.connect(self._resume_migration)
        self.btn_cleanup_failed.clicked.connect(self._cleanup_failed_migration)
        self.btn_verify.clicked.connect(lambda: self._start_command("verify"))
        self.btn_save_report.clicked.connect(self._save_report)
        self.btn_cancel.clicked.connect(self._cancel_worker)
        self.btn_close.clicked.connect(self.close)
        self.btn_previous.clicked.connect(self._go_previous_step)
        self.btn_next.clicked.connect(self._go_next_step)
        self.btn_save_report.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.input_approval_schema.textChanged.connect(self._update_execution_state_from_approval)
        self._update_execution_state(False)

        self.plan_group = QGroupBox("실행 계획 확인")
        plan_layout = QVBoxLayout(self.plan_group)
        self.lbl_plan_summary = QLabel("아직 실행 계획을 생성하지 않았습니다.")
        self.lbl_plan_summary.setWordWrap(True)
        plan_layout.addWidget(self.lbl_plan_summary)
        plan_layout.addWidget(self.btn_run_plan)
        self.step_page_layouts["plan"].addWidget(self.plan_group)
        self.step_page_layouts["plan"].addWidget(option_group)

        self.schema_advanced_layout.insertWidget(0, self.btn_inspect)
        self._set_schema_advanced_visible(False)

        plan_action_layout = QHBoxLayout()
        plan_action_layout.addWidget(self.btn_guide)
        plan_action_layout.addStretch()
        self.step_page_layouts["plan"].addLayout(plan_action_layout)

        execute_secondary_layout = QHBoxLayout()
        execute_secondary_layout.addWidget(self.btn_resume)
        execute_secondary_layout.addWidget(self.btn_cleanup_failed)
        execute_secondary_layout.addStretch()
        execution_layout.addLayout(execute_secondary_layout)

        self.verify_group = QGroupBox("검증 및 결과 저장")
        verify_layout = QVBoxLayout(self.verify_group)
        self.lbl_verify_mode = QLabel("기본 검증: strict row/key/value 비교")
        self.lbl_verify_status = QLabel("검증 대기 중")
        self.lbl_verify_table = QLabel("현재 테이블: -")
        self.lbl_verify_rows = QLabel("검증 rows: -")
        self.lbl_verify_mismatch = QLabel("Mismatch: -")
        self.verify_activity_bar = QProgressBar()
        self.verify_activity_bar.setRange(0, 0)
        self.verify_activity_bar.hide()
        self.txt_verify_log = QPlainTextEdit()
        self.txt_verify_log.setReadOnly(True)
        self.txt_verify_log.setMaximumBlockCount(120)
        self.txt_verify_log.setFixedHeight(110)
        self.txt_verify_log.setPlaceholderText("검증 진행 상황이 표시됩니다.")
        self.txt_verify_result = QPlainTextEdit()
        self.txt_verify_result.setReadOnly(True)
        self.txt_verify_result.setPlaceholderText("검증 실행 후 mismatch 예시와 요약이 표시됩니다.")
        self.btn_run_verify = self.btn_verify
        verify_action_layout = QHBoxLayout()
        verify_action_layout.addWidget(self.btn_run_verify)
        verify_action_layout.addWidget(self.btn_save_report)
        verify_action_layout.addStretch()
        verify_layout.addWidget(self.lbl_verify_mode)
        verify_layout.addWidget(self.lbl_verify_status)
        verify_layout.addWidget(self.verify_activity_bar)
        verify_layout.addWidget(self.lbl_verify_table)
        verify_layout.addWidget(self.lbl_verify_rows)
        verify_layout.addWidget(self.lbl_verify_mismatch)
        verify_layout.addWidget(self.txt_verify_log)
        verify_layout.addLayout(verify_action_layout)
        verify_layout.addWidget(self.txt_verify_result, 1)
        self.step_page_layouts["verify"].addWidget(self.verify_group, 1)

        control_layout.addWidget(self.btn_full_run)
        control_layout.addStretch()
        control_layout.addWidget(self.btn_previous)
        control_layout.addWidget(self.btn_next)
        control_layout.addWidget(self.btn_cancel)
        control_layout.addWidget(self.btn_close)
        action_layout.addLayout(control_layout)
        layout.addWidget(action_group)

        self.txt_schema.textChanged.connect(self._lock_execution_due_to_input_change)
        self._connect_endpoint_lock_signals(self.source_form)
        self._connect_endpoint_lock_signals(self.target_form)
        self.source_form.combo_tunnel.currentIndexChanged.connect(self._sync_target_engine_filter)
        self._sync_target_engine_filter()
        self.source_form.combo_tunnel.currentIndexChanged.connect(self._refresh_direction_summary)
        self.source_form.combo_engine.currentIndexChanged.connect(self._refresh_direction_summary)
        self.target_form.combo_tunnel.currentIndexChanged.connect(self._refresh_direction_summary)
        self.target_form.combo_engine.currentIndexChanged.connect(self._refresh_direction_summary)
        self.source_form.input_schema.textChanged.connect(self._refresh_direction_summary)
        self.source_form.input_database.textChanged.connect(self._refresh_direction_summary)
        self.target_form.input_schema.textChanged.connect(self._refresh_direction_summary)
        self.target_form.input_database.textChanged.connect(self._refresh_direction_summary)
        self._show_step("connections")

    def _current_step_index(self) -> int:
        return self.step_ids.index(self.current_step_id)

    def _direction_label(self) -> str:
        source_engine = "MySQL" if self.source_form.engine() == DatabaseEngine.MYSQL else "PostgreSQL"
        target_engine = "MySQL" if self.target_form.engine() == DatabaseEngine.MYSQL else "PostgreSQL"
        source_schema = self.source_form.input_schema.text().strip() or self.source_form.input_database.text().strip()
        target_schema = self.target_form.input_schema.text().strip() or self.target_form.input_database.text().strip()
        return f"{source_engine} {source_schema} -> {target_engine} {target_schema}"

    def _refresh_direction_summary(self):
        if hasattr(self, "lbl_direction_summary"):
            self.lbl_direction_summary.setText(self._direction_label())

    def _schema_summary_text(self, schema: Dict, unsupported_objects: List[str]) -> str:
        schema_data: Dict[str, Any] = schema if isinstance(schema, dict) else {}
        raw_tables = schema_data.get("tables")
        tables: List[Any] = cast(List[Any], raw_tables) if isinstance(raw_tables, list) else []
        valid_tables: List[Dict[str, Any]] = [table for table in tables if isinstance(table, dict)]
        table_count = len(valid_tables)
        column_count = 0
        index_count = 0
        foreign_key_count = 0
        for table in valid_tables:
            raw_columns = table.get("columns")
            columns: List[Any] = cast(List[Any], raw_columns) if isinstance(raw_columns, list) else []
            raw_indexes = table.get("indexes")
            indexes: List[Any] = cast(List[Any], raw_indexes) if isinstance(raw_indexes, list) else []
            raw_foreign_keys = table.get("foreign_keys")
            foreign_keys: List[Any] = (
                cast(List[Any], raw_foreign_keys) if isinstance(raw_foreign_keys, list) else []
            )
            column_count += len(columns)
            index_count += len(indexes)
            foreign_key_count += len(foreign_keys)
        return (
            f"테이블 {table_count}개, 컬럼 {column_count}개, "
            f"인덱스 {index_count}개, FK {foreign_key_count}개, "
            f"지원 제외 {len(unsupported_objects)}개"
        )

    def _update_source_summary(self, schema: Dict):
        unsupported = [str(item) for item in self.unsupported_objects]
        self.lbl_source_summary.setText(self._schema_summary_text(schema, unsupported))

    def _set_schema_advanced_visible(self, visible: bool):
        if hasattr(self, "txt_schema"):
            self.txt_schema.setVisible(visible)
        if hasattr(self, "btn_load_schema"):
            self.btn_load_schema.setVisible(visible)
        if hasattr(self, "btn_inspect"):
            self.btn_inspect.setVisible(visible)

    def _plan_tables(self, payload: Dict) -> List[Dict]:
        raw_plan = payload.get("plan")
        plan: Dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
        raw_tables = plan.get("tables")
        tables: List[Any] = cast(List[Any], raw_tables) if isinstance(raw_tables, list) else []
        return [table for table in tables if isinstance(table, dict)]

    def _plan_type_mappings(self, payload: Dict) -> List[Dict]:
        raw_plan = payload.get("plan")
        plan: Dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
        raw_mappings = plan.get("type_mappings")
        mappings: List[Any] = cast(List[Any], raw_mappings) if isinstance(raw_mappings, list) else []
        return [mapping for mapping in mappings if isinstance(mapping, dict)]

    def _plan_summary_text(self, payload: Dict) -> str:
        tables = self._plan_tables(payload)
        mappings = self._plan_type_mappings(payload)
        estimated_rows = 0
        for table in tables:
            raw_rows = table.get("estimated_rows")
            if not isinstance(raw_rows, int) or isinstance(raw_rows, bool):
                raw_rows = table.get("rows")
            if isinstance(raw_rows, int) and not isinstance(raw_rows, bool):
                estimated_rows += raw_rows

        lines = [
            f"전환 대상 테이블 {len(tables)}개",
            f"예상 rows {estimated_rows:,}",
        ]
        mapping_summaries: List[str] = []
        for mapping in mappings:
            source_type = str(mapping.get("source_type", "")).strip()
            target_type = str(mapping.get("target_type", "")).strip()
            if source_type and target_type:
                mapping_summaries.append(f"{source_type} -> {target_type}")
        if mapping_summaries:
            lines.append("타입 변환: " + ", ".join(mapping_summaries[:8]))

        raw_plan = payload.get("plan")
        plan: Dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
        raw_ddl_order = plan.get("ddl_order")
        ddl_order: List[Any] = cast(List[Any], raw_ddl_order) if isinstance(raw_ddl_order, list) else []
        ddl_order_text = " ".join(str(item).lower() for item in ddl_order)
        if "foreign" in ddl_order_text or "fk" in ddl_order_text:
            lines.append("FK/index는 데이터 적재 후 생성")
        return "\n".join(lines)

    def _update_plan_summary(self, payload: Dict):
        self.lbl_plan_summary.setText(self._plan_summary_text(payload))

    def _reset_plan_summary_after_failure(self):
        self.lbl_plan_summary.setText("실행 계획 생성에 실패했습니다. 다시 계획 생성을 실행해 주세요.")

    def _reset_plan_summary_after_input_change(self):
        if hasattr(self, "lbl_plan_summary"):
            self.lbl_plan_summary.setText("아직 실행 계획을 생성하지 않았습니다.")

    def _verification_result_text(self, payload: Dict) -> str:
        mismatch_lines: List[str] = []
        raw_mismatches = payload.get("mismatches")
        if isinstance(raw_mismatches, list):
            for mismatch in raw_mismatches[:20]:
                if not isinstance(mismatch, dict):
                    continue
                table = mismatch.get("table", "")
                key = mismatch.get("key", "")
                column = mismatch.get("column", "")
                source_value = mismatch.get("source_value", "")
                target_value = mismatch.get("target_value", "")
                difference = mismatch.get("difference", "")
                mismatch_lines.append(
                    f"- {table} / {key} / {column}: "
                    f"source={source_value}, target={target_value}, type={difference}"
                )

        row_diff_lines: List[str] = []
        raw_row_diffs = payload.get("row_count_differences")
        if isinstance(raw_row_diffs, list):
            for diff in raw_row_diffs:
                if not isinstance(diff, dict):
                    continue
                row_diff_lines.append(
                    f"- {diff.get('table', '')}: "
                    f"source {diff.get('source_rows', 0)}, target {diff.get('target_rows', 0)}"
                )

        lines: List[str] = []
        if mismatch_lines:
            lines.append("Mismatch 예시")
            lines.extend(mismatch_lines)
        if row_diff_lines:
            if lines:
                lines.append("")
            lines.append("Row count 차이")
            lines.extend(row_diff_lines)
        if not lines and payload.get("success") is True:
            lines.append("검증 통과: Source와 Target 데이터가 일치합니다.")
        elif not lines:
            lines.append("검증 실패: Rust Core가 비교 차이 상세를 반환하지 않았습니다.")
        return "\n".join(lines)

    def _update_verification_result(self, payload: Dict):
        mismatches = payload.get("mismatches")
        mismatch_count = len(mismatches) if isinstance(mismatches, list) else 0
        self.verify_activity_bar.hide()
        self.lbl_verify_status.setText("검증 완료" if bool(payload.get("success")) else "검증 완료: 불일치 확인 필요")
        self.lbl_verify_mismatch.setText(f"Mismatch: {mismatch_count:,}개")
        self._append_verify_log("검증 완료")
        self.txt_verify_result.setPlainText(self._verification_result_text(payload))

    def _mark_verify_result_stale(self):
        self.verify_activity_bar.hide()
        self.lbl_verify_status.setText("검증 실패")
        self.txt_verify_result.setPlainText("검증 실패: 새 검증 결과를 받지 못했습니다.")
        if isinstance(self.last_result, dict) and self.last_result.get("command") == "verify":
            self.last_result = None
        self.btn_save_report.setEnabled(False)

    def _mark_verify_result_stale_after_input_change(self):
        if not hasattr(self, "txt_verify_result"):
            return
        if self.txt_verify_result.toPlainText().strip():
            self.txt_verify_result.setPlainText("입력 정보가 변경되어 새 검증이 필요합니다.")
        else:
            self.txt_verify_result.clear()

    def _invalidate_stale_reports_after_input_change(self):
        self._reset_plan_summary_after_input_change()
        self._mark_verify_result_stale_after_input_change()
        if isinstance(self.last_result, dict) and self.last_result.get("command") in ("plan", "verify"):
            self.last_result = None
        if not self.last_result and hasattr(self, "btn_save_report"):
            self.btn_save_report.setEnabled(False)

    def _selected_direction(self) -> MigrationDirection:
        return MigrationDirection.from_engines(self.source_form.engine(), self.target_form.engine())

    def _direction_display(self, direction: MigrationDirection) -> str:
        if direction == MigrationDirection.MYSQL_TO_POSTGRESQL:
            return "MySQL -> PostgreSQL"
        return "PostgreSQL -> MySQL"

    def _selected_direction_result(self, payload: Dict) -> Optional[Dict]:
        directions = payload.get("directions")
        if not isinstance(directions, list):
            return None
        selected = self._selected_direction().value
        for direction in directions:
            if isinstance(direction, dict) and direction.get("direction") == selected:
                return direction
        return None

    def _issue_counts(self, issues) -> Dict[str, int]:
        if not isinstance(issues, list):
            return {"blocking": 0, "warnings": 0}
        blocking_count = sum(1 for issue in issues if isinstance(issue, dict) and issue.get("blocking"))
        warning_count = sum(1 for issue in issues if isinstance(issue, dict) and not issue.get("blocking"))
        return {"blocking": blocking_count, "warnings": warning_count}

    def _is_target_non_empty_issue(self, issue) -> bool:
        if not isinstance(issue, dict):
            return False
        if issue.get("blocking") is not True:
            return False
        location = str(issue.get("location", "")).lower()
        message = str(issue.get("message", "")).lower()
        text = f"{location} {message}"
        has_target = "target" in text
        has_non_empty = any(marker in text for marker in ("not empty", "non-empty", "existing"))
        return has_target and has_non_empty

    def _update_target_safety_from_issues(self, issues) -> bool:
        issue_list = issues if isinstance(issues, list) else []
        target_blocked = any(self._is_target_non_empty_issue(issue) for issue in issue_list)
        if target_blocked:
            self.lbl_target_safety.setText(
                "Target에 기존 테이블 또는 데이터가 있습니다. 기본 설정에서는 빈 Target만 전환을 실행할 수 있습니다."
            )
            self.btn_target_advanced.setVisible(True)
            self._show_step("safety")
            if not self.isVisible():
                self.show()
            return True
        self.lbl_target_safety.setText("Target 상태 확인 완료: 기존 테이블 또는 데이터 차단 이슈가 없습니다.")
        self.btn_target_advanced.setVisible(False)
        return False

    def _update_preflight_summary(self, payload: Dict, target_blocked: bool):
        counts = self._issue_counts(payload.get("issues"))
        if bool(payload.get("success")) and not target_blocked:
            summary = f"점검 통과: 차단 이슈 0개, 경고 {counts['warnings']}개"
        else:
            summary = f"점검 실패: 차단 이슈 {counts['blocking']}개, 경고 {counts['warnings']}개"
        self.lbl_safety_summary.setText(summary)

    def _blocking_preflight_issues(self):
        if not isinstance(self.last_result, dict) or self.last_result.get("command") != "preflight":
            return []
        issues = self.last_result.get("issues")
        if not isinstance(issues, list):
            return []
        return [issue for issue in issues if isinstance(issue, dict) and issue.get("blocking") is True]

    def _cleanup_plan_resolves_safety_blockers(self) -> bool:
        if not self.chk_cleanup_before_migrate.isChecked():
            return False
        blocking_issues = self._blocking_preflight_issues()
        return bool(blocking_issues) and all(
            self._is_target_non_empty_issue(issue) for issue in blocking_issues
        )

    def _safety_step_complete(self) -> bool:
        return self._step_completed.get("safety", False) or self._cleanup_plan_resolves_safety_blockers()

    def _on_cleanup_before_migrate_toggled(self, checked: bool):
        if checked and self._cleanup_plan_resolves_safety_blockers():
            self.lbl_target_safety.setText(
                "Target 정리를 실행 직전에 수행하도록 계획했습니다. 실행 버튼을 누르기 전까지 DB는 변경되지 않습니다."
            )
        elif self.last_result and self.last_result.get("command") == "preflight":
            target_blocked = self._update_target_safety_from_issues(self.last_result.get("issues"))
            self._update_preflight_summary(self.last_result, target_blocked)
        self._refresh_navigation_state()

    def _open_target_advanced_options(self):
        visible = not self.target_advanced_panel.isVisible()
        self.target_advanced_panel.setVisible(visible)
        self.btn_target_advanced.setText("고급 설정 닫기" if visible else "고급 설정 열기")

    def _next_enabled_for_current_step(self) -> bool:
        if self.worker and self.worker.isRunning():
            return False
        if self.current_step_id == "connections":
            return self._connection_step_ready()
        if self.current_step_id == "inspect":
            return self._step_completed.get("inspect", False)
        if self.current_step_id == "safety":
            return self._safety_step_complete()
        if self.current_step_id == "plan":
            return self._step_completed.get("plan", False)
        if self.current_step_id == "execute":
            return self._step_completed.get("execute", False) or self._can_start_migration_from_execute_step()
        if self.current_step_id == "verify":
            return self._step_completed.get("verify", False)
        return False

    def _can_start_migration_from_execute_step(self) -> bool:
        running = bool(self.worker and self.worker.isRunning())
        return (not running) and self._execution_unlocked and self._approval_matches_target_schema()

    def _next_button_text(self) -> str:
        if self.current_step_id == "verify":
            return "완료"
        if self.current_step_id == "execute":
            if self._step_completed.get("execute", False):
                return "검증 단계로 이동"
            return "DB 변경 실행"
        return "다음"

    def _connection_step_ready(self) -> bool:
        source_selected = bool(self.source_form.combo_tunnel.currentData())
        target_selected = bool(self.target_form.combo_tunnel.currentData())
        return source_selected and target_selected and self.source_form.engine() != self.target_form.engine()

    def _refresh_navigation_state(self):
        if hasattr(self, "btn_previous"):
            running = bool(self.worker and self.worker.isRunning())
            self.btn_previous.setEnabled(self._current_step_index() > 0 and not running)
        if hasattr(self, "btn_next"):
            self.btn_next.setText(self._next_button_text())
            self.btn_next.setEnabled(self._next_enabled_for_current_step())
        if hasattr(self, "lbl_next_hint"):
            self.lbl_next_hint.setText(self._next_hint_text())

    def _next_hint_text(self) -> str:
        if self.worker and self.worker.isRunning():
            return "현재 작업이 실행 중입니다. 완료될 때까지 기다려 주세요."
        if self.current_step_id == "connections":
            return "Source와 Target 연결을 모두 선택하면 다음 단계로 이동할 수 있습니다."
        if self.current_step_id == "inspect":
            return "Source 구조 분석이 완료되면 다음 단계로 이동할 수 있습니다."
        if self.current_step_id == "safety":
            if self._cleanup_plan_resolves_safety_blockers():
                return "Target 정리를 실행 직전에 수행하도록 계획했습니다. 다음 단계로 이동할 수 있습니다."
            return "전환 가능 여부 점검이 통과되면 다음 단계로 이동할 수 있습니다."
        if self._next_enabled_for_current_step():
            return "현재 단계가 완료되었습니다. 다음 단계로 이동할 수 있습니다."
        if self.current_step_id == "plan":
            return "실행 계획 생성이 완료되면 다음 단계로 이동할 수 있습니다."
        if self.current_step_id == "execute":
            if not self._approval_matches_target_schema():
                return "Target schema 이름을 정확히 입력한 뒤 DB 변경 실행을 눌러 주세요."
            return "DB 변경 실행이 완료되면 다음 단계로 이동할 수 있습니다."
        if self.current_step_id == "verify":
            return "검증 결과를 받은 뒤 완료할 수 있습니다."
        return ""

    def _show_step(self, step_id: str):
        self.current_step_id = step_id
        for page_id, page in self.step_pages.items():
            page.setVisible(page_id == step_id)
        if hasattr(self, "btn_previous"):
            running = bool(self.worker and self.worker.isRunning())
            self.btn_previous.setEnabled(self._current_step_index() > 0 and not running)
        if hasattr(self, "btn_next"):
            self.btn_next.setText(self._next_button_text())
            self.btn_next.setEnabled(self._next_enabled_for_current_step())
        self._refresh_navigation_state()
        self._refresh_direction_summary()

    def _go_previous_step(self):
        index = self._current_step_index()
        if index > 0:
            self._show_step(self.step_ids[index - 1])

    def _go_next_step(self):
        if hasattr(self, "btn_next") and not self.btn_next.isEnabled():
            return
        if self.current_step_id == "execute" and not self._step_completed.get("execute", False):
            self._start_command("migrate")
            return
        index = self._current_step_index()
        if index >= len(self.step_ids) - 1:
            self.close()
            return
        self._show_step(self.step_ids[index + 1])

    def _load_schema_json(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Normalized schema JSON 불러오기",
            "",
            "JSON Files (*.json);;All Files (*.*)",
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.txt_schema.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
            self._step_completed["inspect"] = not self._schema_is_empty()
            self._refresh_navigation_state()
        except Exception as exc:
            QMessageBox.critical(self, "불러오기 실패", str(exc))

    def _payload(self, prepare_tunnels: bool = False) -> Dict:
        source_engine = self.source_form.engine()
        target_engine = self.target_form.engine()
        try:
            schema = json.loads(self.txt_schema.toPlainText() or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Normalized schema JSON 오류: {exc}") from exc

        payload = {
            "source_engine": source_engine.value,
            "target_engine": target_engine.value,
            "source": self.source_form.payload(prepare_tunnel=prepare_tunnels),
            "target": self.target_form.payload(prepare_tunnel=prepare_tunnels),
            "schema": schema,
            "execution_options": {
                "mode": "create_only" if self.chk_create_only.isChecked() else "append",
                "chunk_size": self.spin_chunk_size.value(),
                "cleanup_before_migrate": self.chk_cleanup_before_migrate.isChecked(),
            },
            "guide_options": {
                "row_limit": self.spin_guide_row_limit.value(),
            },
            "verify_options": {
                "mode": "strict",
                "mismatch_limit": 20,
            },
        }
        if self.unsupported_objects:
            payload["unsupported_objects"] = list(self.unsupported_objects)
        return payload

    def _start_full_workflow(self):
        self._workflow_active = True
        self._start_command("inspect", workflow=True)

    def _start_command(self, command: str, workflow: bool = False):
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "작업 진행 중", "이미 실행 중인 작업이 있습니다.")
            return

        if command not in ("inspect", "migrate") and self._schema_is_empty():
            self._pending_after_inspect = command
            self._workflow_active = workflow
            self._append_log(f"[inspect] 스키마가 비어 있어 Source 자동 검사를 먼저 실행합니다.")
            self._start_command("inspect", workflow=False)
            return

        if command == "migrate":
            if not self._execution_unlocked:
                QMessageBox.warning(
                    self,
                    "DB 변경 잠김",
                    "사전 점검 또는 계획 생성이 성공한 뒤에 DB 변경 실행을 사용할 수 있습니다.",
                )
                self._workflow_active = False
                self._current_command = None
                return
            if not self._confirm_migration_execution():
                self._workflow_active = False
                self._current_command = None
                return

        try:
            payload = self._payload(prepare_tunnels=True)
        except ValueError as exc:
            QMessageBox.warning(self, "입력 오류", str(exc))
            return

        self._start_command_with_payload(command, payload, workflow=workflow)

    def _start_command_with_payload(self, command: str, payload: Dict, workflow: bool = False):
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "작업 진행 중", "이미 실행 중인 작업이 있습니다.")
            return

        self._workflow_active = workflow or self._workflow_active
        self._current_command = command
        if command == "verify":
            self._verify_result_received = False
        self._reset_command_ui(command)
        self._append_log(f"[{command}] 시작")
        self._set_running(True)
        self.worker = CrossEngineMigrationWorker(command, payload)
        self.worker.phase_changed.connect(self._on_phase_changed)
        self.worker.table_progress.connect(self._on_table_progress)
        self.worker.row_progress.connect(self._on_row_progress)
        self.worker.checkpoint.connect(self._save_checkpoint)
        self.worker.issue.connect(self._on_issue)
        self.worker.log_message.connect(self._append_log)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.result.connect(self._on_result)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _save_checkpoint(self, state: Dict):
        key = state_key_from_payload(self._payload())
        self._last_checkpoint_path = save_resume_state(key, state)

    def _on_result(self, payload: Dict):
        self.last_result = payload
        self.btn_save_report.setEnabled(True)
        if payload.get("command") == "migrate" and isinstance(payload.get("state"), dict):
            key = state_key_from_payload(self._payload())
            path = save_resume_state(key, payload["state"])
            self._append_log(f"재개 상태 저장: {path}")
        schema = schema_from_inspect_result(payload)
        if schema is not None:
            self.txt_schema.setPlainText(json.dumps(schema, ensure_ascii=False, indent=2))
            self._step_completed["inspect"] = bool(payload.get("success")) and not self._schema_is_empty()
            self._set_execution_unlocked(False)
            table_count = len(schema.get("tables", [])) if isinstance(schema.get("tables"), list) else 0
            self.lbl_schema_status.setText(f"Rust Core 검사 완료: {table_count}개 테이블")
            if payload.get("success"):
                self.btn_auto_inspect.hide()
            self._append_log("스키마 검사 결과를 입력에 반영했습니다.")
        unsupported_objects = payload.get("unsupported_objects")
        if isinstance(unsupported_objects, list):
            self.unsupported_objects = [str(item) for item in unsupported_objects]
            if self.unsupported_objects:
                self._append_log(
                    f"지원 제외 객체 {len(self.unsupported_objects)}개를 preflight warning 대상으로 저장했습니다."
                )
        elif payload.get("command") == "inspect":
            self.unsupported_objects = []
        if schema is not None:
            self._update_source_summary(schema)
        if payload.get("command") == "readiness":
            self._append_readiness_summary(payload)
        if payload.get("command") == "guide":
            self._append_guide_summary(payload)
        if payload.get("command") == "plan":
            self._step_completed["plan"] = bool(payload.get("success"))
            self._update_plan_summary(payload)
        if payload.get("command") == "verify":
            self._verify_result_received = True
            self._step_completed["verify"] = True
            self._update_verification_result(payload)
        if payload.get("command") in ("preflight", "plan"):
            target_blocked = self._update_target_safety_from_issues(payload.get("issues"))
            if payload.get("command") == "preflight":
                self._step_completed["safety"] = bool(payload.get("success")) and not target_blocked
                self._update_preflight_summary(payload, target_blocked)
            self._set_execution_unlocked(bool(payload.get("success")) and not target_blocked)
            if self._execution_unlocked:
                self._append_log("사전 확인이 완료되어 DB 변경 실행이 활성화되었습니다.")
            else:
                self._append_log("차단 이슈가 있어 DB 변경 실행은 계속 잠겨 있습니다.")
        if payload.get("command") == "migrate":
            self._step_completed["execute"] = bool(payload.get("success"))
            self._update_migration_result_summary(payload)
        if payload.get("command") not in ("readiness", "migrate"):
            self._append_log(json.dumps(payload, ensure_ascii=False, indent=2))
        self._refresh_navigation_state()

        if payload.get("command") == "inspect" and payload.get("success") and self._pending_after_inspect:
            next_command = self._pending_after_inspect
            self._pending_after_inspect = None
            QTimer.singleShot(0, lambda: self._start_command(next_command, workflow=self._workflow_active))

    def _append_readiness_summary(self, payload: Dict):
        selected = self._selected_direction_result(payload)
        if not selected:
            return
        direction = self._selected_direction()
        status = "가능" if selected.get("success") else "불가"
        counts = self._issue_counts(selected.get("issues"))
        summary = (
            f"{self._direction_display(direction)} {status} "
            f"(tables={selected.get('table_count', 0)}, "
            f"blocking={counts['blocking']}, warnings={counts['warnings']})"
        )
        self.lbl_safety_summary.setText(summary)
        self._append_log("[전환 가능 여부 점검]")
        self._append_log(summary)

    def _append_guide_summary(self, payload: Dict):
        directions = payload.get("directions")
        if not isinstance(directions, list):
            return
        self._append_log("[상세 가이드]")
        for direction in directions:
            if not isinstance(direction, dict):
                continue
            guide = direction.get("guide")
            tables = guide.get("tables") if isinstance(guide, dict) else []
            table_count = len(tables) if isinstance(tables, list) else 0
            ddl_count = len(guide.get("create_table_sql", [])) if isinstance(guide, dict) else 0
            self._append_log(
                f"- {direction.get('direction', '')}: "
                f"DDL {ddl_count}개, table guide {table_count}개"
            )

    def _on_finished(self, success: bool, payload):
        finished_command = self._current_command
        self.worker = None
        self._set_running(False)
        if finished_command == "preflight":
            self._finish_safety_activity(success)
        if finished_command == "plan" and not success:
            self._step_completed["plan"] = False
            self._reset_plan_summary_after_failure()
        if finished_command == "verify" and not success and not self._verify_result_received:
            self._step_completed["verify"] = False
            self._mark_verify_result_stale()
        if finished_command == "migrate" and not success:
            self._step_completed["execute"] = False
            if not self.lbl_migration_result.isVisible():
                self.lbl_migration_result.setText("DB 변경 실패: Rust Core가 상세 실패 원인을 반환하지 않았습니다.")
                self.lbl_migration_result.show()
                self.btn_cleanup_failed.show()
        if finished_command == "cleanup" and success:
            self.btn_cleanup_failed.hide()
            self.lbl_migration_result.setText("실패한 전환 정리가 완료되었습니다. 전환 가능 여부 점검을 다시 실행하세요.")
            self.lbl_migration_result.show()
            self.lbl_safety_summary.setText("Target 정리 완료: 전환 가능 여부 점검을 다시 실행하세요.")
            self.lbl_target_safety.setText("Target 정리가 완료되었습니다. 다시 점검해 빈 Target 상태를 확인하세요.")
            self._append_safety_log("Target 정리 완료")
        self._append_log("완료" if success else "실패")
        if self._workflow_active and finished_command:
            next_command = next_workflow_command(finished_command, success)
            if next_command:
                QTimer.singleShot(0, lambda: self._start_command(next_command, workflow=True))
            else:
                self._workflow_active = False
                self._current_command = None
        elif finished_command == "cleanup":
            self._current_command = None
        self._refresh_navigation_state()

    def _confirm_migration_execution(self) -> bool:
        if self._approval_matches_target_schema():
            return True
        QMessageBox.warning(
            self,
            "승인 필요",
            "Target schema 이름을 정확히 입력해야 DB 변경을 실행할 수 있습니다.",
        )
        return False

    def _cancel_worker(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self._append_log("취소 요청")

    def _resume_migration(self):
        try:
            payload = self._payload(prepare_tunnels=True)
        except ValueError as exc:
            QMessageBox.warning(self, "입력 오류", str(exc))
            return
        state = load_resume_state(state_key_from_payload(payload))
        if not state:
            QMessageBox.information(self, "재개 상태 없음", "저장된 재개 상태가 없습니다.")
            return
        if not self._confirm_migration_execution():
            return
        payload["state"] = state
        self._start_command_with_payload("migrate", payload)

    def _cleanup_failed_migration(self):
        if not self._approval_matches_target_schema():
            QMessageBox.warning(
                self,
                "승인 필요",
                "Target schema 이름을 정확히 입력해야 실패한 전환 정리를 실행할 수 있습니다.",
            )
            return
        self.chk_cleanup_before_migrate.setChecked(True)
        self.lbl_migration_result.setText(
            "다음 DB 변경 실행 전에 Target 정리를 수행합니다. Target schema 이름을 확인한 뒤 DB 변경 실행을 다시 눌러 주세요."
        )
        self.lbl_migration_result.show()

    def _save_report(self):
        if not self.last_result:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "DB 전환 결과 저장",
            "cross_engine_migration_report.txt",
            "Text Files (*.txt);;JSON Files (*.json);;All Files (*.*)",
        )
        if not path:
            return
        try:
            if path.lower().endswith(".json"):
                content = json.dumps(self.last_result, ensure_ascii=False, indent=2)
            else:
                content = render_result_report(self.last_result)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._append_log(f"결과 저장 완료: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))

    def _set_running(self, running: bool):
        if self._current_command == "preflight" and running:
            self._start_safety_activity("전환 가능 여부 점검 중")
        for button in (
            self.btn_full_run,
            self.btn_auto_inspect,
            self.btn_inspect,
            self.btn_preflight,
            self.btn_readiness,
            self.btn_run_safety,
            self.btn_guide,
            self.btn_plan,
            self.btn_migrate,
            self.btn_resume,
            self.btn_cleanup_failed,
            self.btn_verify,
        ):
            button.setEnabled(not running)
        self._update_execution_state(running)
        self.btn_cancel.setEnabled(running)
        if hasattr(self, "btn_previous"):
            self.btn_previous.setEnabled((not running) and self._current_step_index() > 0)
        if hasattr(self, "btn_next"):
            self.btn_next.setEnabled((not running) and self._next_enabled_for_current_step())

    def _set_execution_unlocked(self, unlocked: bool):
        self._execution_unlocked = unlocked
        running = bool(self.worker and self.worker.isRunning())
        self._update_execution_state(running)

    def _update_execution_state(self, running: bool):
        approved = self._approval_matches_target_schema() if hasattr(self, "input_approval_schema") else True
        can_execute = (not running) and self._execution_unlocked and approved
        if hasattr(self, "btn_migrate"):
            self.btn_migrate.setEnabled(can_execute)
        if hasattr(self, "lbl_execution_lock"):
            if self._execution_unlocked:
                self.lbl_execution_lock.setText(
                    "점검이 통과되었습니다. Target schema 이름 입력 후 DB 변경 실행을 사용할 수 있습니다."
                )
            else:
                self.lbl_execution_lock.setText("DB 변경 실행은 사전 점검 또는 계획 생성 성공 후 활성화됩니다.")
        if hasattr(self, "btn_next"):
            self.btn_next.setEnabled(self._next_enabled_for_current_step())

    def _target_approval_schema(self) -> str:
        schema = self.target_form.input_schema.text().strip()
        database = self.target_form.input_database.text().strip()
        if self.target_form.engine() == DatabaseEngine.POSTGRESQL:
            return schema or "public"
        return schema or database

    def _approval_matches_target_schema(self) -> bool:
        if not hasattr(self, "input_approval_schema"):
            return False
        return self.input_approval_schema.text().strip() == self._target_approval_schema()

    def _update_execution_state_from_approval(self):
        self._update_execution_state(bool(self.worker and self.worker.isRunning()))
        if hasattr(self, "btn_next"):
            self.btn_next.setEnabled(self._next_enabled_for_current_step())

    def _on_phase_changed(self, phase: str, message: str):
        self.lbl_execution_phase.setText(message or phase)
        self._append_log(f"[phase:{phase}] {message}")
        if phase == "preflight" or self._current_command == "preflight":
            self._update_safety_activity(message or phase)
        if phase == "verify" or self._current_command == "verify":
            self.lbl_verify_status.setText(message or phase)
            self.verify_activity_bar.show()
            self._append_verify_log(f"[phase:{phase}] {message}")

    def _on_issue(self, issue):
        line = f"[{issue.severity}] {issue.location}: {issue.message}"
        self._append_log(line)
        if self._current_command == "preflight":
            self._append_safety_log(line)

    def _on_worker_failed(self, message: str):
        line = f"[error] {message}"
        self._append_log(line)
        if self._current_command == "preflight":
            self._append_safety_log(line)
        if self._current_command == "verify":
            self._append_verify_log(line)

    def _on_table_progress(self, table: str, status: str):
        if self._current_command == "verify":
            self.lbl_verify_table.setText(f"현재 테이블: {table} ({status})")
            self._append_verify_log(f"[table:{table}] {status}")
            return
        self.lbl_current_table.setText(f"현재 테이블: {table} ({status})")
        self._append_log(f"[table:{table}] {status}")

    def _on_row_progress(self, table: str, rows: int, total):
        total_text = f"{int(total):,}" if total is not None else "?"
        if self._current_command == "verify":
            self.lbl_verify_rows.setText(f"검증 rows: {int(rows):,} / {total_text} rows")
            self._append_verify_log(f"[rows:{table}] {rows}/{total if total is not None else '?'}")
            return
        self.lbl_current_rows.setText(f"현재 rows: {int(rows):,} / {total_text} rows")
        self._append_log(f"[rows:{table}] {rows}/{total if total is not None else '?'}")

    def _reset_command_ui(self, command: str):
        if command in ("migrate", "cleanup"):
            self.lbl_execution_phase.setText("DB 변경 준비 중" if command == "migrate" else "정리 준비 중")
            self.lbl_current_table.setText("현재 테이블: -")
            self.lbl_current_rows.setText("현재 rows: -")
            self.lbl_migration_result.clear()
            self.lbl_migration_result.hide()
            self.txt_log.clear()
            self.btn_cleanup_failed.hide()
        if command == "inspect":
            self.btn_auto_inspect.show()
        if command == "preflight":
            self.txt_safety_log.clear()
            self.target_advanced_panel.hide()
            self.btn_target_advanced.setText("고급 설정 열기")
        if command == "verify":
            self.lbl_verify_status.setText("검증 준비 중")
            self.lbl_verify_table.setText("현재 테이블: -")
            self.lbl_verify_rows.setText("검증 rows: -")
            self.lbl_verify_mismatch.setText("Mismatch: -")
            self.verify_activity_bar.show()
            self.txt_verify_log.clear()
            self.txt_verify_result.clear()

    def _append_verify_log(self, text: str):
        if hasattr(self, "txt_verify_log"):
            self.txt_verify_log.appendPlainText(text)

    def _update_migration_result_summary(self, payload: Dict):
        issues = payload.get("issues")
        issue_list: List[Dict[str, Any]] = (
            [cast(Dict[str, Any], issue) for issue in issues if isinstance(issue, dict)]
            if isinstance(issues, list)
            else []
        )
        success = bool(payload.get("success"))
        if success:
            self.lbl_execution_phase.setText("DB 변경 완료")
            self.lbl_migration_result.setText("DB 변경이 완료되었습니다. 다음 단계에서 검증을 실행하세요.")
            self.lbl_migration_result.show()
            self.btn_cleanup_failed.hide()
            return

        first_issue: Dict[str, Any] = issue_list[0] if issue_list else {}
        location = str(first_issue.get("location", "")).strip()
        message = str(first_issue.get("message", "")).strip()
        suggestion = str(first_issue.get("suggestion", "")).strip()
        raw_state = payload.get("state")
        state: Dict[str, Any] = cast(Dict[str, Any], raw_state) if isinstance(raw_state, dict) else {}
        raw_tables = state.get("tables")
        tables: List[Any] = cast(List[Any], raw_tables) if isinstance(raw_tables, list) else []
        failed_table = location or next(
            (
                str(table.get("table", ""))
                for table in tables
                if isinstance(table, dict) and not table.get("completed")
            ),
            "",
        )
        self.lbl_execution_phase.setText("DB 변경 실패")
        if failed_table:
            self.lbl_current_table.setText(f"실패 테이블: {failed_table}")
        lines = ["DB 변경 실패"]
        if failed_table:
            lines.append(f"실패 위치: {failed_table}")
        if message:
            lines.append(f"원인: {message}")
        if suggestion:
            lines.append(f"다음 행동: {suggestion}")
        else:
            lines.append("다음 행동: Target 정리 후 전환 가능 여부 점검부터 다시 실행하세요.")
        self.lbl_migration_result.setText("\n".join(lines))
        self.lbl_migration_result.show()
        self.btn_cleanup_failed.show()
        self._append_log("\n".join(lines))

    def _lock_execution_due_to_input_change(self):
        for step_id in self._step_completed:
            self._step_completed[step_id] = False
        if self._execution_unlocked:
            self._set_execution_unlocked(False)
        self._invalidate_stale_reports_after_input_change()
        self._refresh_navigation_state()

    def _schema_is_empty(self) -> bool:
        try:
            schema = json.loads(self.txt_schema.toPlainText() or "{}")
        except json.JSONDecodeError:
            return False
        tables = schema.get("tables") if isinstance(schema, dict) else None
        return not isinstance(tables, list) or len(tables) == 0

    def _connect_endpoint_lock_signals(self, form: "EndpointForm"):
        form.combo_tunnel.currentIndexChanged.connect(self._lock_execution_due_to_input_change)
        form.combo_engine.currentIndexChanged.connect(self._lock_execution_due_to_input_change)
        form.input_host.textChanged.connect(self._lock_execution_due_to_input_change)
        form.input_port.valueChanged.connect(self._lock_execution_due_to_input_change)
        form.input_user.textChanged.connect(self._lock_execution_due_to_input_change)
        form.input_password.textChanged.connect(self._lock_execution_due_to_input_change)
        form.input_database.textChanged.connect(self._lock_execution_due_to_input_change)
        form.input_schema.textChanged.connect(self._lock_execution_due_to_input_change)

    def _sync_target_engine_filter(self):
        source_data = self.source_form.combo_tunnel.currentData()
        if not source_data:
            self.target_form.set_engine_filter(None)
            self._refresh_direction_summary()
            return
        source_engine = self.source_form.engine().value
        target_engine = "postgresql" if source_engine == "mysql" else "mysql"
        self.target_form.set_engine_filter({target_engine})
        self._refresh_direction_summary()

    def _append_log(self, message: str):
        self.txt_log.appendPlainText(message)

    def _append_safety_log(self, message: str):
        if hasattr(self, "txt_safety_log"):
            self.txt_safety_log.appendPlainText(message)

    def _safety_activity_text(self, message: str) -> str:
        text = (message or "").strip()
        lowered = text.lower()
        if "target state" in lowered and "completed" not in lowered:
            return "Target 상태 확인 중"
        if "target state" in lowered:
            return "Target 상태 확인 완료"
        if "schema compatibility" in lowered:
            return "Source schema 호환성 확인 완료"
        if "result ready" in lowered:
            return "점검 결과 정리 중"
        if "preflight checks started" in lowered or lowered == "preflight":
            return "전환 가능 여부 점검 중"
        return text or "전환 가능 여부 점검 중"

    def _start_safety_activity(self, message: str):
        self._safety_activity_base = self._safety_activity_text(message)
        self._safety_activity_dots = 0
        self.lbl_safety_activity.setText(self._safety_activity_base)
        self.safety_activity_bar.show()
        if not self.safety_activity_timer.isActive():
            self.safety_activity_timer.start()
        if "전환 가능 여부 점검을 시작했습니다" not in self.txt_safety_log.toPlainText():
            self._append_safety_log("전환 가능 여부 점검을 시작했습니다.")

    def _update_safety_activity(self, message: str):
        self._safety_activity_base = self._safety_activity_text(message)
        self._safety_activity_dots = 0
        self.lbl_safety_activity.setText(self._safety_activity_base)
        self._append_safety_log(self._safety_activity_base)

    def _tick_safety_activity(self):
        if not self._safety_activity_base:
            return
        self._safety_activity_dots = (self._safety_activity_dots % 3) + 1
        self.lbl_safety_activity.setText(
            f"{self._safety_activity_base}{'.' * self._safety_activity_dots}"
        )

    def _finish_safety_activity(self, success: bool):
        self.safety_activity_timer.stop()
        self.safety_activity_bar.hide()
        self._safety_activity_base = ""
        text = "점검 완료" if success else "점검 실패"
        self.lbl_safety_activity.setText(text)
        self._append_safety_log(text)

    def closeEvent(self, a0: Optional[QCloseEvent]):
        assert a0 is not None
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "작업 진행 중",
                "DB 전환 작업이 진행 중입니다. 종료하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                a0.ignore()
                return
            self.worker.cancel()
            if not self.worker.wait(3000):
                self.worker.terminate()
                self.worker.wait(1000)
        a0.accept()


class EndpointForm(QGroupBox):
    def __init__(
        self,
        title: str,
        default_engine: DatabaseEngine,
        tunnel_engine=None,
        config_manager=None,
        require_tunnel: bool = False,
    ):
        super().__init__(title)
        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager
        self.require_tunnel = require_tunnel
        self.engine_filter = None
        self._setup_ui(default_engine)

    def _setup_ui(self, default_engine: DatabaseEngine):
        layout = QFormLayout(self)

        self.combo_tunnel = QComboBox()
        self._load_tunnels()

        self.combo_engine = QComboBox()
        self.combo_engine.addItem("MySQL", DatabaseEngine.MYSQL.value)
        self.combo_engine.addItem("PostgreSQL", DatabaseEngine.POSTGRESQL.value)
        index = self.combo_engine.findData(default_engine.value)
        self.combo_engine.setCurrentIndex(index if index >= 0 else 0)
        self.combo_engine.setEnabled(False)
        self.combo_engine.setToolTip("터널 연결 정보에서 자동 인식됩니다.")

        self.input_host = QLineEdit("127.0.0.1")
        self.input_port = QSpinBox()
        self.input_port.setRange(1, 65535)
        self.input_port.setValue(3306 if default_engine == DatabaseEngine.MYSQL else 5432)
        self.input_user = QLineEdit()
        self.input_password = QLineEdit()
        self.input_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_database = QLineEdit()
        self.input_schema = QLineEdit()
        self.input_schema.setPlaceholderText("MySQL은 database, PostgreSQL은 schema")

        self.combo_engine.currentIndexChanged.connect(self._on_engine_changed)
        self.combo_tunnel.currentIndexChanged.connect(self._on_tunnel_selected)

        layout.addRow("기존 연결:", self.combo_tunnel)
        layout.addRow("Engine:", self.combo_engine)
        layout.addRow("Host:", self.input_host)
        layout.addRow("Port:", self.input_port)
        layout.addRow("User:", self.input_user)
        layout.addRow("Password:", self.input_password)
        layout.addRow("Database:", self.input_database)
        layout.addRow("Schema scope:", self.input_schema)
        self._apply_tunnel_only_state()

    def _load_tunnels(self):
        selected_data = self.combo_tunnel.currentData() if hasattr(self, "combo_tunnel") else None
        selected_id = selected_data.get("tunnel_id") if isinstance(selected_data, dict) else None
        self.combo_tunnel.clear()
        self.combo_tunnel.addItem("터널 목록에서 선택", None)

        seen = set()
        for config in self._configured_tunnels():
            tid = config.get("id")
            if not tid:
                continue
            if not self._passes_engine_filter(config):
                continue
            self.combo_tunnel.addItem(self._tunnel_display(config), self._tunnel_data(config))
            seen.add(tid)

        if self.tunnel_engine:
            for tunnel in self.tunnel_engine.get_active_tunnels():
                tid = tunnel.get("tunnel_id") or tunnel.get("id")
                if not tid or tid in seen:
                    continue
                config = getattr(self.tunnel_engine, "tunnel_configs", {}).get(tid, {})
                if not self._passes_engine_filter(config, tunnel):
                    continue
                self.combo_tunnel.addItem(self._tunnel_display(config, tunnel), self._tunnel_data(config, tunnel))

        if self.combo_tunnel.count() == 1:
            self.combo_tunnel.setItemText(0, "사용 가능한 터널 항목 없음")
        elif selected_id:
            for index in range(1, self.combo_tunnel.count()):
                data = self.combo_tunnel.itemData(index)
                if isinstance(data, dict) and data.get("tunnel_id") == selected_id:
                    self.combo_tunnel.setCurrentIndex(index)
                    break

    def set_engine_filter(self, allowed_engines):
        self.engine_filter = set(allowed_engines) if allowed_engines else None
        self._load_tunnels()

    def _passes_engine_filter(self, config: Dict, active_info: Optional[Dict] = None) -> bool:
        if not self.engine_filter:
            return True
        engine = self._known_engine(config, active_info)
        return engine in self.engine_filter

    def _configured_tunnels(self):
        if not self.config_manager:
            return []
        try:
            config = self.config_manager.load_config()
        except Exception:
            return []
        tunnels = config.get("tunnels", [])
        return tunnels if isinstance(tunnels, list) else []

    def _tunnel_display(self, config: Dict, active_info: Optional[Dict] = None) -> str:
        name = config.get("name") or (active_info or {}).get("name") or config.get("id", "Unknown")
        engine = self._known_engine(config, active_info)
        engine_key = engine or ""
        engine_label = {
            "mysql": "MySQL",
            "postgresql": "PostgreSQL",
        }.get(engine_key, "엔진 미확인")
        if active_info:
            host = active_info.get("host", "")
            port = active_info.get("port", "")
            return f"{name} ({engine_label}, {host}:{port}, 연결됨)"
        host = config.get("remote_host", "")
        port = config.get("remote_port", "")
        mode = "직접" if config.get("connection_mode") == "direct" else "터널"
        return f"{name} ({engine_label}, {host}:{port}, {mode})"

    def _tunnel_data(self, config: Dict, active_info: Optional[Dict] = None) -> Dict:
        tid = config.get("id") or (active_info or {}).get("tunnel_id") or (active_info or {}).get("id")
        host, port = self._connection_host_port(config, active_info)
        return {
            "tunnel_id": tid,
            "host": host,
            "port": port,
            "config": config,
        }

    def _known_engine(self, config: Dict, active_info: Optional[Dict] = None):
        engine = config.get("db_engine")
        if engine in ("mysql", "postgresql"):
            return engine
        return None

    def _connection_host_port(self, config: Dict, active_info: Optional[Dict] = None):
        if active_info and active_info.get("host") and active_info.get("port"):
            return active_info["host"], int(active_info["port"])

        tid = config.get("id")
        if self.tunnel_engine and tid and self.tunnel_engine.is_running(tid):
            host, port = self.tunnel_engine.get_connection_info(tid)
            if host and port:
                return host, int(port)

        if config.get("connection_mode") == "direct":
            return config.get("remote_host") or "127.0.0.1", int(config.get("remote_port", 0) or 0)
        return "127.0.0.1", int(config.get("local_port", 0) or config.get("remote_port", 0) or 0)

    def _on_engine_changed(self):
        if self.engine() == DatabaseEngine.MYSQL and self.input_port.value() == 5432:
            self.input_port.setValue(3306)
        elif self.engine() == DatabaseEngine.POSTGRESQL and self.input_port.value() == 3306:
            self.input_port.setValue(5432)
        if self.engine() == DatabaseEngine.MYSQL and not self.input_schema.text().strip():
            self.input_schema.setText(self.input_database.text().strip())
        elif self.engine() == DatabaseEngine.POSTGRESQL and not self.input_schema.text().strip():
            self.input_schema.setText("public")

    def _on_tunnel_selected(self):
        data = self.combo_tunnel.currentData()
        if not data:
            return
        self._apply_tunnel_data(data)

    def _apply_tunnel_data(self, data: Dict):
        host = data.get("host")
        port = data.get("port")
        config = data.get("config") or {}
        if host:
            self.input_host.setText(str(host))
        if port:
            self.input_port.setValue(int(port))

        engine = self._detect_engine(host, port, config)
        engine_index = self.combo_engine.findData(engine.value)
        if engine_index >= 0:
            self.combo_engine.setCurrentIndex(engine_index)

        default_schema = config.get("default_schema")
        default_database = config.get("default_database")
        if self.engine() == DatabaseEngine.POSTGRESQL and default_database:
            self.input_database.setText(str(default_database))
        elif self.engine() == DatabaseEngine.POSTGRESQL:
            self.input_database.setText("postgres")
        elif default_schema:
            self.input_database.setText(str(default_schema))
        if default_schema:
            if self.engine() == DatabaseEngine.MYSQL:
                self.input_schema.setText(str(default_schema))
            elif self.engine() == DatabaseEngine.POSTGRESQL:
                self.input_schema.setText(str(default_schema))
        elif self.engine() == DatabaseEngine.POSTGRESQL and not self.input_schema.text().strip():
            self.input_schema.setText("public")

        if self.config_manager:
            db_user, db_password = self.config_manager.get_tunnel_credentials(data["tunnel_id"])
            if db_user:
                self.input_user.setText(db_user)
            if db_password:
                self.input_password.setText(db_password)

    def _detect_engine(self, host, port, config: Dict) -> DatabaseEngine:
        configured = config.get("db_engine")
        if configured in ("mysql", "postgresql"):
            return DatabaseEngine(configured)
        return self.engine()

    def _apply_tunnel_only_state(self):
        if not self.require_tunnel:
            return
        self.input_host.setReadOnly(True)
        self.input_port.setEnabled(False)
        self.input_user.setReadOnly(True)
        self.input_password.setReadOnly(True)
        self.input_database.setReadOnly(True)

    def _prepare_selected_tunnel(self):
        data = self.combo_tunnel.currentData()
        if not data:
            raise ValueError(f"{self.title()}는 터널 목록에서 항목을 선택해야 합니다.")

        config = data.get("config") or {}
        tid = data.get("tunnel_id")
        if self.tunnel_engine and config and tid and not self.tunnel_engine.is_running(tid):
            success, message = self.tunnel_engine.start_tunnel(config)
            if not success:
                raise ValueError(f"{self.title()} 터널 시작 실패: {message}")
            host, port = self.tunnel_engine.get_connection_info(tid)
            if host and port:
                data["host"] = host
                data["port"] = int(port)

        self._apply_tunnel_data(data)

    def engine(self) -> DatabaseEngine:
        return DatabaseEngine(self.combo_engine.currentData())

    def payload(self, prepare_tunnel: bool = False) -> Dict:
        if self.require_tunnel:
            if prepare_tunnel:
                self._prepare_selected_tunnel()
            elif not self.combo_tunnel.currentData():
                raise ValueError(f"{self.title()}는 터널 목록에서 항목을 선택해야 합니다.")
        schema = self.input_schema.text().strip()
        database = self.input_database.text().strip()
        if self.engine() == DatabaseEngine.MYSQL:
            database = schema or database
            schema = database
        elif not schema:
            schema = "public"
            if not database:
                database = "postgres"
        return make_connection_payload(
            self.engine(),
            self.input_host.text().strip(),
            self.input_port.value(),
            self.input_user.text().strip(),
            self.input_password.text(),
            database,
            schema,
        )


class CrossEngineMigrationWizard:
    @staticmethod
    def start(parent=None, tunnel_engine=None, config_manager=None) -> bool:
        dialog = CrossEngineMigrationDialog(parent, tunnel_engine=tunnel_engine, config_manager=config_manager)
        return dialog.exec() == QDialog.DialogCode.Accepted
