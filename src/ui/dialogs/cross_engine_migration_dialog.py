"""MySQL <-> PostgreSQL migration dialog."""
import json
from typing import Dict, List, Optional

from PyQt6.QtCore import QTimer, Qt
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
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.core.cross_engine_migration import (
    DatabaseEngine,
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
        self.setWindowTitle("DB 전환 마법사")
        self.resize(900, 700)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

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
        layout.addLayout(endpoint_layout)
        self.lbl_direction_summary = QLabel()
        self.lbl_direction_summary.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.lbl_direction_summary)

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
        layout.addWidget(option_group)

        schema_group = QGroupBox("스키마 검사 결과")
        schema_layout = QVBoxLayout(schema_group)
        load_layout = QHBoxLayout()
        self.lbl_schema_status = QLabel("Source DB를 검사하면 Rust Core가 정규화한 스키마가 자동으로 채워집니다.")
        schema_layout.addWidget(self.lbl_schema_status)
        self.btn_load_schema = QPushButton("JSON 불러오기")
        self.btn_auto_inspect = QPushButton("Source 자동 검사")
        self.btn_load_schema.clicked.connect(self._load_schema_json)
        self.btn_auto_inspect.clicked.connect(lambda: self._start_command("inspect"))
        self.btn_auto_inspect.setToolTip("선택한 Source DB를 Rust Core schema.inspect로 검사합니다.")
        load_layout.addWidget(self.btn_auto_inspect)
        load_layout.addWidget(self.btn_load_schema)
        load_layout.addStretch()
        schema_layout.addLayout(load_layout)
        self.txt_schema = QPlainTextEdit()
        self.txt_schema.setPlaceholderText('{"tables":[{"name":"users","columns":[{"name":"id","type":"int(11)","nullable":false,"primary_key":true}]}]}')
        self.txt_schema.setPlainText('{"tables":[]}')
        schema_layout.addWidget(self.txt_schema)
        layout.addWidget(schema_group, 1)

        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(1000)
        layout.addWidget(self.txt_log, 1)

        action_group = QGroupBox("작업 순서")
        action_layout = QVBoxLayout(action_group)
        self.lbl_execution_lock = QLabel("DB 변경 실행은 사전 점검 또는 계획 생성 성공 후 활성화됩니다.")
        action_layout.addWidget(self.lbl_execution_lock)

        step_layout = QHBoxLayout()
        step_check = QGroupBox("1. 점검")
        step_check_layout = QHBoxLayout(step_check)
        step_prepare = QGroupBox("2. 계획/가이드")
        step_prepare_layout = QHBoxLayout(step_prepare)
        step_execute = QGroupBox("3. DB 변경")
        step_execute_layout = QHBoxLayout(step_execute)
        step_verify = QGroupBox("4. 검증/저장")
        step_verify_layout = QHBoxLayout(step_verify)
        control_layout = QHBoxLayout()

        self.btn_full_run = QPushButton("전체 실행")
        self.btn_inspect = QPushButton("스키마 검사")
        self.btn_preflight = QPushButton("사전 점검")
        self.btn_readiness = QPushButton("양방향 점검")
        self.btn_guide = QPushButton("상세 가이드")
        self.btn_plan = QPushButton("계획 생성")
        self.btn_migrate = QPushButton("DB 변경 실행")
        self.btn_resume = QPushButton("재개(DB 변경)")
        self.btn_verify = QPushButton("검증")
        self.btn_save_report = QPushButton("결과 저장")
        self.btn_cancel = QPushButton("취소")
        self.btn_close = QPushButton("닫기")
        self.btn_previous = QPushButton("이전")
        self.btn_next = QPushButton("다음")
        self.btn_full_run.hide()
        self.btn_migrate.setToolTip("대상 DB에 스키마 생성과 데이터 적재를 실행합니다.")
        self.btn_resume.setToolTip("저장된 상태부터 대상 DB 변경 작업을 재개합니다.")
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
        self.btn_verify.clicked.connect(lambda: self._start_command("verify"))
        self.btn_save_report.clicked.connect(self._save_report)
        self.btn_cancel.clicked.connect(self._cancel_worker)
        self.btn_close.clicked.connect(self.close)
        self.btn_previous.clicked.connect(self._go_previous_step)
        self.btn_next.clicked.connect(self._go_next_step)
        self.btn_save_report.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self._update_execution_state(False)

        step_check_layout.addWidget(self.btn_readiness)
        step_check_layout.addWidget(self.btn_inspect)
        step_check_layout.addWidget(self.btn_preflight)
        step_prepare_layout.addWidget(self.btn_guide)
        step_prepare_layout.addWidget(self.btn_plan)
        step_execute_layout.addWidget(self.btn_migrate)
        step_execute_layout.addWidget(self.btn_resume)
        step_verify_layout.addWidget(self.btn_verify)
        step_verify_layout.addWidget(self.btn_save_report)

        step_layout.addWidget(step_check, 3)
        step_layout.addWidget(step_prepare, 2)
        step_layout.addWidget(step_execute, 2)
        step_layout.addWidget(step_verify, 2)
        action_layout.addLayout(step_layout)

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

    def _show_step(self, step_id: str):
        self.current_step_id = step_id
        for page_id, page in self.step_pages.items():
            page.setVisible(page_id == step_id)
        if hasattr(self, "btn_previous"):
            self.btn_previous.setEnabled(self._current_step_index() > 0)
        if hasattr(self, "btn_next"):
            is_last = self._current_step_index() == len(self.step_ids) - 1
            self.btn_next.setText("완료" if is_last else "다음")
        self._refresh_direction_summary()

    def _go_previous_step(self):
        index = self._current_step_index()
        if index > 0:
            self._show_step(self.step_ids[index - 1])

    def _go_next_step(self):
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
            },
            "guide_options": {
                "row_limit": self.spin_guide_row_limit.value(),
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
        self._append_log(f"[{command}] 시작")
        self._set_running(True)
        self.worker = CrossEngineMigrationWorker(command, payload)
        self.worker.phase_changed.connect(lambda phase, message: self._append_log(f"[phase:{phase}] {message}"))
        self.worker.table_progress.connect(lambda table, status: self._append_log(f"[table:{table}] {status}"))
        self.worker.row_progress.connect(lambda table, rows, total: self._append_log(f"[rows:{table}] {rows}/{total if total is not None else '?'}"))
        self.worker.checkpoint.connect(self._save_checkpoint)
        self.worker.issue.connect(lambda issue: self._append_log(f"[{issue.severity}] {issue.location}: {issue.message}"))
        self.worker.failed.connect(lambda message: self._append_log(f"[error] {message}"))
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
            self._set_execution_unlocked(False)
            table_count = len(schema.get("tables", [])) if isinstance(schema.get("tables"), list) else 0
            self.lbl_schema_status.setText(f"Rust Core 검사 완료: {table_count}개 테이블")
            self._append_log("스키마 검사 결과를 입력에 반영했습니다.")
        unsupported_objects = payload.get("unsupported_objects")
        if isinstance(unsupported_objects, list):
            self.unsupported_objects = [str(item) for item in unsupported_objects]
            if self.unsupported_objects:
                self._append_log(
                    f"지원 제외 객체 {len(self.unsupported_objects)}개를 preflight warning 대상으로 저장했습니다."
                )
        if payload.get("command") == "readiness":
            self._append_readiness_summary(payload)
        if payload.get("command") == "guide":
            self._append_guide_summary(payload)
        if payload.get("command") in ("preflight", "plan"):
            self._set_execution_unlocked(bool(payload.get("success")))
            if self._execution_unlocked:
                self._append_log("사전 확인이 완료되어 DB 변경 실행이 활성화되었습니다.")
            else:
                self._append_log("차단 이슈가 있어 DB 변경 실행은 계속 잠겨 있습니다.")
        self._append_log(json.dumps(payload, ensure_ascii=False, indent=2))

        if payload.get("command") == "inspect" and payload.get("success") and self._pending_after_inspect:
            next_command = self._pending_after_inspect
            self._pending_after_inspect = None
            QTimer.singleShot(0, lambda: self._start_command(next_command, workflow=self._workflow_active))

    def _append_readiness_summary(self, payload: Dict):
        directions = payload.get("directions")
        if not isinstance(directions, list):
            return
        self._append_log("[양방향 점검 결과]")
        for direction in directions:
            if not isinstance(direction, dict):
                continue
            status = "가능" if direction.get("success") else "불가"
            issues = direction.get("issues")
            blocking_count = 0
            warning_count = 0
            if isinstance(issues, list):
                blocking_count = sum(
                    1 for issue in issues if isinstance(issue, dict) and issue.get("blocking")
                )
                warning_count = sum(
                    1 for issue in issues if isinstance(issue, dict) and not issue.get("blocking")
                )
            self._append_log(
                f"- {direction.get('direction', '')}: {status} "
                f"(tables={direction.get('table_count', 0)}, "
                f"blocking={blocking_count}, warnings={warning_count})"
            )

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
        self._set_running(False)
        self._append_log("완료" if success else "실패")
        if self._workflow_active and self._current_command:
            next_command = next_workflow_command(self._current_command, success)
            if next_command:
                QTimer.singleShot(0, lambda: self._start_command(next_command, workflow=True))
            else:
                self._workflow_active = False
                self._current_command = None

    def _confirm_migration_execution(self) -> bool:
        reply = QMessageBox.question(
            self,
            "DB 전환 실행 확인",
            "대상 데이터베이스에 스키마 생성 및 데이터 적재를 실행합니다. 계속하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

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
        for button in (
            self.btn_full_run,
            self.btn_auto_inspect,
            self.btn_inspect,
            self.btn_preflight,
            self.btn_readiness,
            self.btn_guide,
            self.btn_plan,
            self.btn_migrate,
            self.btn_resume,
            self.btn_verify,
        ):
            button.setEnabled(not running)
        self._update_execution_state(running)
        self.btn_cancel.setEnabled(running)

    def _set_execution_unlocked(self, unlocked: bool):
        self._execution_unlocked = unlocked
        running = bool(self.worker and self.worker.isRunning())
        self._update_execution_state(running)

    def _update_execution_state(self, running: bool):
        if hasattr(self, "btn_migrate"):
            self.btn_migrate.setEnabled((not running) and self._execution_unlocked)
        if hasattr(self, "lbl_execution_lock"):
            if self._execution_unlocked:
                self.lbl_execution_lock.setText("점검이 통과되어 DB 변경 실행을 사용할 수 있습니다.")
            else:
                self.lbl_execution_lock.setText("DB 변경 실행은 사전 점검 또는 계획 생성 성공 후 활성화됩니다.")

    def _lock_execution_due_to_input_change(self):
        if self._execution_unlocked:
            self._set_execution_unlocked(False)

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

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "작업 진행 중",
                "DB 전환 작업이 진행 중입니다. 종료하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            if not self.worker.wait(3000):
                self.worker.terminate()
                self.worker.wait(1000)
        event.accept()


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
        engine_label = {
            "mysql": "MySQL",
            "postgresql": "PostgreSQL",
        }.get(engine, "엔진 미확인")
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
