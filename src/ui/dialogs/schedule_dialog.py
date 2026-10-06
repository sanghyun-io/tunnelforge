"""
스케줄 백업 관리 다이얼로그
- 스케줄 추가/수정
- 스케줄 목록 관리
"""
import os
import re
import uuid
from datetime import datetime
from typing import Optional, List

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QComboBox, QSpinBox, QCheckBox,
    QPushButton, QGroupBox, QRadioButton, QButtonGroup,
    QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QWidget, QTimeEdit, QTabWidget, QTextEdit,
)
from PyQt6.QtCore import Qt, QTime, pyqtSignal

from src.core.schedule_time import validate_expression
from src.core.scheduler import ScheduleConfig, CronParser, BackupScheduler, ScheduleTaskType
from src.core.logger import get_logger
from src.core.i18n import translate_text

logger = get_logger(__name__)


class ScheduleEditDialog(QDialog):
    """스케줄 추가/수정 다이얼로그"""

    def __init__(self, parent=None, tunnel_list: List[tuple] = None,
                 schedule: ScheduleConfig = None, min_interval_minutes: int = 15,
                 tunnel_engines: dict = None, database_lister=None, tunnel_environments: dict = None):
        """
        Args:
            parent: 부모 위젯
            tunnel_list: [(tunnel_id, tunnel_name), ...] 터널 목록
            schedule: 수정할 스케줄 (None이면 새로 생성)
            min_interval_minutes: 허용되는 최소 실행 간격(분)
            tunnel_engines: {tunnel_id: db_engine} - PostgreSQL 터널에서만 데이터베이스 선택을 보여준다
            database_lister: tunnel_id -> (데이터베이스 목록, 오류) - "목록 불러오기" 버튼용
        """
        super().__init__(parent)
        self.tunnel_engines = tunnel_engines or {}
        self.tunnel_environments = tunnel_environments or {}
        self.database_lister = database_lister
        self.tunnel_list = tunnel_list or []
        self.schedule = schedule
        self.min_interval_minutes = min_interval_minutes
        self.result_config: Optional[ScheduleConfig] = None

        self._setup_ui()
        self._connect_signals()

        if schedule:
            self._load_schedule(schedule)

    def _setup_ui(self):
        """UI 구성"""
        self.setWindowTitle("스케줄 작업 추가" if not self.schedule else "스케줄 작업 수정")
        self.setMinimumWidth(600)
        self.setMinimumHeight(550)

        layout = QVBoxLayout(self)
        # 예약은 백업만 지원한다 (예약 SQL 실행 금지). 이전 버전의 SQL 일정은 삭제만 가능하다.
        self.unattended_note = QLabel(
            "예약 백업은 사람이 없는 상태로 실행됩니다. 처음 보는 SSH 호스트 키와 비밀번호가 필요한 SSH 개인키는 "
            "자동으로 수락/입력되지 않고 실패하며, 이 경우 작업 목록에 사유가 기록됩니다."
        )
        self.unattended_note.setWordWrap(True)
        self.unattended_note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self.unattended_note)
        self.unsupported_label = QLabel(
            "이 일정은 예약 SQL 실행 작업입니다. 예약 SQL 실행은 지원되지 않으므로 실행되지 않으며 삭제만 할 수 있습니다."
        )
        self.unsupported_label.setWordWrap(True)
        self.unsupported_label.setStyleSheet("color: #c0392b; font-weight: bold;")
        self.unsupported_label.setVisible(False)
        layout.addWidget(self.unsupported_label)
        layout.addWidget(self._build_basic_info_group())

        self.backup_page = self._build_backup_page()
        layout.addWidget(self.backup_page)

        layout.addWidget(self._build_schedule_group())

        self.enabled_check = QCheckBox("스케줄 활성화")
        self.enabled_check.setChecked(True)
        layout.addWidget(self.enabled_check)

        self.catch_up_check = QCheckBox("절전/앱 미실행으로 놓친 실행은 복귀 후 한 번만 따라잡기")
        self.catch_up_check.setChecked(True)
        layout.addWidget(self.catch_up_check)

        layout.addWidget(self._build_rehearsal_group())

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("저장")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._save)
        btn_layout.addWidget(self.save_btn)

        layout.addLayout(btn_layout)

    def _build_basic_info_group(self) -> QGroupBox:
        basic_group = QGroupBox("기본 정보")
        basic_layout = QFormLayout(basic_group)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("작업 이름")
        basic_layout.addRow("이름:", self.name_edit)

        self.tunnel_combo = QComboBox()
        for tunnel_id, tunnel_name in self.tunnel_list:
            self.tunnel_combo.addItem(tunnel_name, tunnel_id)
        basic_layout.addRow("터널:", self.tunnel_combo)

        self.database_combo = QComboBox()
        self.database_combo.setEditable(True)
        self.database_combo.lineEdit().setPlaceholderText("비우면 postgres")
        self.database_load_btn = QPushButton("목록 불러오기")
        self.database_load_btn.clicked.connect(self._load_databases)
        database_row = QWidget()
        database_layout = QHBoxLayout(database_row)
        database_layout.setContentsMargins(0, 0, 0, 0)
        database_layout.addWidget(self.database_combo, 1)
        database_layout.addWidget(self.database_load_btn)
        self.database_row = database_row
        basic_layout.addRow("데이터베이스 (PostgreSQL):", database_row)
        self.tunnel_combo.currentIndexChanged.connect(self._update_database_row)

        self.schema_edit = QLineEdit()
        self.schema_edit.setPlaceholderText("대상 스키마 (MySQL은 데이터베이스)")
        basic_layout.addRow("스키마:", self.schema_edit)

        return basic_group

    def _build_rehearsal_group(self) -> QGroupBox:
        group = QGroupBox("복원 리허설 (선택)")
        form = QFormLayout(group)
        self.rehearsal_check = QCheckBox("백업 직후 복원 리허설 실행 (후보 복원 → 행/digest 검증 → 후보 정리)")
        form.addRow(self.rehearsal_check)
        note = QLabel(
            "방금 만든 백업을 아래 비운영 대상에 안전 복원(별도 후보 네임스페이스)으로 복원해 검증하고, 이 실행이 만든 "
            "후보만 정리합니다. 대상의 기존 네임스페이스는 변경되지 않으며 반드시 미리 존재해야 합니다. "
            "환경이 개발/스테이징으로 설정된 프로필만 선택할 수 있고 운영 프로필은 선택할 수 없습니다."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow(note)
        self.rehearsal_tunnel_combo = QComboBox()
        for tunnel_id, tunnel_name in self.tunnel_list:
            if self.tunnel_environments.get(tunnel_id) in ("development", "staging"):
                self.rehearsal_tunnel_combo.addItem(tunnel_name, tunnel_id)
        form.addRow("리허설 대상 터널:", self.rehearsal_tunnel_combo)
        self.rehearsal_database_edit = QLineEdit()
        self.rehearsal_database_edit.setPlaceholderText("PostgreSQL 데이터베이스 (비우면 postgres)")
        form.addRow("리허설 데이터베이스:", self.rehearsal_database_edit)
        self.rehearsal_schema_edit = QLineEdit()
        self.rehearsal_schema_edit.setPlaceholderText("이미 존재하는 대상 스키마 (MySQL은 데이터베이스)")
        form.addRow("리허설 스키마:", self.rehearsal_schema_edit)
        has_targets = self.rehearsal_tunnel_combo.count() > 0
        self.rehearsal_check.setEnabled(has_targets)
        if not has_targets:
            self.rehearsal_check.setToolTip("환경이 개발/스테이징으로 설정된 프로필이 없습니다.")
        return group

    def _tunnel_is_postgresql(self) -> bool:
        return self.tunnel_engines.get(self.tunnel_combo.currentData()) == "postgresql"

    def _update_database_row(self, *_):
        # 엔진을 모르면(tunnel_engines 없음) 숨기지 않는다. MySQL 터널에서는 의미가 없으므로 숨긴다.
        visible = not self.tunnel_engines or self._tunnel_is_postgresql()
        self.database_row.setVisible(visible)
        self.database_load_btn.setEnabled(self.database_lister is not None)

    def _load_databases(self):
        if not self.database_lister:
            return
        databases, error = self.database_lister(self.tunnel_combo.currentData())
        if error:
            QMessageBox.warning(self, "데이터베이스 목록", error)
            return
        current = self.database_combo.currentText()
        self.database_combo.clear()
        self.database_combo.addItems(databases)
        self.database_combo.setCurrentText(current)

    def _build_backup_page(self) -> QWidget:
        backup_page = QWidget()
        backup_layout = QVBoxLayout(backup_page)
        backup_layout.setContentsMargins(0, 0, 0, 0)

        backup_detail_group = QGroupBox("백업 설정")
        backup_detail_layout = QFormLayout(backup_detail_group)

        self.tables_edit = QLineEdit()
        self.tables_edit.setPlaceholderText("테이블1, 테이블2, ... (비워두면 전체)")
        backup_detail_layout.addRow("테이블:", self.tables_edit)

        # 출력 디렉토리
        output_layout = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("백업 파일 저장 위치")
        output_layout.addWidget(self.output_edit)
        self.browse_btn = QPushButton("찾아보기...")
        self.browse_btn.clicked.connect(self._browse_output_dir)
        output_layout.addWidget(self.browse_btn)
        backup_detail_layout.addRow("출력 경로:", output_layout)

        # 보관 정책
        self.retention_count_spin = QSpinBox()
        self.retention_count_spin.setRange(1, 100)
        self.retention_count_spin.setValue(5)
        backup_detail_layout.addRow("최대 백업 수:", self.retention_count_spin)

        self.retention_days_spin = QSpinBox()
        self.retention_days_spin.setRange(1, 365)
        self.retention_days_spin.setValue(30)
        backup_detail_layout.addRow("보관 기간 (일):", self.retention_days_spin)

        backup_layout.addWidget(backup_detail_group)
        return backup_page

    def _build_schedule_group(self) -> QGroupBox:
        schedule_group = QGroupBox("스케줄 설정")
        schedule_layout = QVBoxLayout(schedule_group)

        # 간편 설정 / 고급 설정 탭
        self.schedule_tabs = QTabWidget()

        # 간편 설정 탭
        simple_tab = QWidget()
        simple_layout = QVBoxLayout(simple_tab)

        self.schedule_type_group = QButtonGroup(self)
        types_layout = QHBoxLayout()

        self.daily_radio = QRadioButton("매일")
        self.weekly_radio = QRadioButton("매주")
        self.monthly_radio = QRadioButton("매월")
        self.hourly_radio = QRadioButton("매시간")
        self.daily_radio.setChecked(True)

        self.schedule_type_group.addButton(self.daily_radio, 0)
        self.schedule_type_group.addButton(self.weekly_radio, 1)
        self.schedule_type_group.addButton(self.monthly_radio, 2)
        self.schedule_type_group.addButton(self.hourly_radio, 3)

        types_layout.addWidget(self.daily_radio)
        types_layout.addWidget(self.weekly_radio)
        types_layout.addWidget(self.monthly_radio)
        types_layout.addWidget(self.hourly_radio)
        types_layout.addStretch()
        simple_layout.addLayout(types_layout)

        # 요일 선택 (매주용)
        self.dow_widget = QWidget()
        dow_layout = QHBoxLayout(self.dow_widget)
        dow_layout.setContentsMargins(0, 0, 0, 0)
        dow_layout.addWidget(QLabel("요일:"))
        self.dow_combo = QComboBox()
        self.dow_combo.addItems(["일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일"])
        self.dow_combo.setCurrentIndex(1)  # 월요일
        dow_layout.addWidget(self.dow_combo)
        dow_layout.addStretch()
        simple_layout.addWidget(self.dow_widget)
        self.dow_widget.hide()

        # 날짜 선택 (매월용)
        self.day_widget = QWidget()
        day_layout = QHBoxLayout(self.day_widget)
        day_layout.setContentsMargins(0, 0, 0, 0)
        day_layout.addWidget(QLabel("일:"))
        self.day_spin = QSpinBox()
        self.day_spin.setRange(1, 28)
        self.day_spin.setValue(1)
        day_layout.addWidget(self.day_spin)
        day_layout.addStretch()
        simple_layout.addWidget(self.day_widget)
        self.day_widget.hide()

        # 분 선택 (매시간용)
        self.minute_widget = QWidget()
        minute_layout = QHBoxLayout(self.minute_widget)
        minute_layout.setContentsMargins(0, 0, 0, 0)
        minute_layout.addWidget(QLabel("분:"))
        self.minute_spin = QSpinBox()
        self.minute_spin.setRange(0, 59)
        self.minute_spin.setValue(0)
        minute_layout.addWidget(self.minute_spin)
        minute_layout.addStretch()
        simple_layout.addWidget(self.minute_widget)
        self.minute_widget.hide()

        # 시간 선택 (매일/매주/매월용)
        self.time_widget = QWidget()
        time_layout = QHBoxLayout(self.time_widget)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.addWidget(QLabel("시간:"))
        self.time_edit = QTimeEdit()
        self.time_edit.setTime(QTime(3, 0))  # 기본 03:00
        self.time_edit.setDisplayFormat("HH:mm")
        time_layout.addWidget(self.time_edit)
        time_layout.addStretch()
        simple_layout.addWidget(self.time_widget)

        simple_layout.addStretch()
        self.schedule_tabs.addTab(simple_tab, "간편 설정")

        # 고급 설정 탭
        advanced_tab = QWidget()
        advanced_layout = QVBoxLayout(advanced_tab)

        cron_label = QLabel("Cron 표현식 (분 시 일 월 요일):")
        advanced_layout.addWidget(cron_label)

        self.cron_edit = QLineEdit()
        self.cron_edit.setPlaceholderText("예: 0 3 * * * (매일 03:00)")
        advanced_layout.addWidget(self.cron_edit)

        self.cron_desc_label = QLabel("")
        self.cron_desc_label.setStyleSheet("color: gray;")
        advanced_layout.addWidget(self.cron_desc_label)

        help_text = QLabel(
            "예시:\n"
            "  0 3 * * *   = 매일 03:00\n"
            "  0 0 * * 0   = 매주 일요일 00:00\n"
            "  0 12 1 * *  = 매월 1일 12:00\n"
            "  30 6 * * 1-5 = 평일 06:30\n"
            "  0 * * * *   = 매시간 정각"
        )
        help_text.setStyleSheet("color: gray; font-size: 11px;")
        advanced_layout.addWidget(help_text)

        advanced_layout.addStretch()
        self.schedule_tabs.addTab(advanced_tab, "고급 설정")

        schedule_layout.addWidget(self.schedule_tabs)
        return schedule_group

    def _connect_signals(self):
        """시그널 연결"""
        self.schedule_type_group.idClicked.connect(self._on_schedule_type_changed)
        self.cron_edit.textChanged.connect(self._on_cron_changed)

    def _on_schedule_type_changed(self, button_id: int):
        """스케줄 타입 변경"""
        self.dow_widget.setVisible(button_id == 1)  # 매주
        self.day_widget.setVisible(button_id == 2)  # 매월
        self.minute_widget.setVisible(button_id == 3)  # 매시간
        self.time_widget.setVisible(button_id != 3)  # 매시간이 아닐 때만 시간 표시

    def _on_cron_changed(self, text: str):
        """Cron 표현식 변경"""
        if text.strip():
            desc = CronParser.describe(text)
            next_run = CronParser.get_next_run(text)
            if next_run:
                self.cron_desc_label.setText(
                    f"{desc}\n다음 실행: {next_run.strftime('%Y-%m-%d %H:%M')}"
                )
            else:
                self.cron_desc_label.setText("잘못된 표현식")
        else:
            self.cron_desc_label.setText("")

    def _browse_output_dir(self):
        """출력 디렉토리 선택"""
        self._browse_dir(self.output_edit, "백업 저장 위치 선택")

    def _browse_dir(self, line_edit: QLineEdit, title: str):
        current = line_edit.text() or os.path.expanduser("~")
        dir_path = QFileDialog.getExistingDirectory(self, title, current)
        if dir_path:
            line_edit.setText(dir_path)

    def _load_schedule(self, schedule: ScheduleConfig):
        """기존 스케줄 로드"""
        self.name_edit.setText(schedule.name)

        # 터널 선택
        for i in range(self.tunnel_combo.count()):
            if self.tunnel_combo.itemData(i) == schedule.tunnel_id:
                self.tunnel_combo.setCurrentIndex(i)
                break

        self.schema_edit.setText(schedule.schema)
        # 이전 버전 일정(database 없음)은 항상 postgres에 접속했으므로 그대로 표시한다
        self.database_combo.setCurrentText(schedule.database or ("postgres" if self._tunnel_is_postgresql() else ""))
        self._update_database_row()

        self.catch_up_check.setChecked(bool(getattr(schedule, "catch_up_missed", True)))
        if getattr(schedule, "rehearsal_tunnel_id", ""):
            index = self.rehearsal_tunnel_combo.findData(schedule.rehearsal_tunnel_id)
            if index < 0:  # 이제 대상이 될 수 없는 프로필(환경 변경 등): 저장 시 검증이 막도록 그대로 보여 준다
                self.rehearsal_tunnel_combo.addItem(schedule.rehearsal_tunnel_id, schedule.rehearsal_tunnel_id)
                index = self.rehearsal_tunnel_combo.count() - 1
            self.rehearsal_tunnel_combo.setCurrentIndex(index)
            self.rehearsal_check.setEnabled(True)
            self.rehearsal_check.setChecked(True)
            self.rehearsal_database_edit.setText(schedule.rehearsal_database)
            self.rehearsal_schema_edit.setText(schedule.rehearsal_schema)

        # 작업 유형
        if schedule.is_sql_query_task():
            self.unsupported_label.setVisible(True)
            self.save_btn.setEnabled(False)
            self.backup_page.setVisible(False)
        else:
            self.tables_edit.setText(", ".join(schedule.tables) if schedule.tables else "")
            self.output_edit.setText(schedule.output_dir)
            self.retention_count_spin.setValue(schedule.retention_count)
            self.retention_days_spin.setValue(schedule.retention_days)

        # Cron 표현식
        self.cron_edit.setText(schedule.cron_expression)
        self.schedule_tabs.setCurrentIndex(1)  # 고급 탭

        self.enabled_check.setChecked(schedule.enabled)

    def _get_cron_expression(self) -> str:
        """설정에서 Cron 표현식 생성"""
        if self.schedule_tabs.currentIndex() == 1:  # 고급 탭
            return self.cron_edit.text().strip()

        # 간편 설정에서 생성
        if self.hourly_radio.isChecked():
            # 매시간
            minute = self.minute_spin.value()
            return f"{minute} * * * *"

        time = self.time_edit.time()
        minute = time.minute()
        hour = time.hour()

        if self.daily_radio.isChecked():
            return f"{minute} {hour} * * *"
        elif self.weekly_radio.isChecked():
            dow = self.dow_combo.currentIndex()  # 0=일요일
            return f"{minute} {hour} * * {dow}"
        else:  # 매월
            day = self.day_spin.value()
            return f"{minute} {hour} {day} * *"

    def _save(self):
        """저장"""
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "입력 오류", "이름을 입력하세요.")
            self.name_edit.setFocus()
            return

        if self.tunnel_combo.currentIndex() < 0:
            QMessageBox.warning(self, "입력 오류", "터널을 선택하세요.")
            return

        schema = self.schema_edit.text().strip()
        if self.schedule is not None and self.schedule.is_sql_query_task():
            QMessageBox.warning(self, "지원되지 않음", "예약 SQL 실행은 지원되지 않습니다.")
            return
        rehearsal = {"tunnel_id": "", "database": "", "schema": ""}
        if self.rehearsal_check.isChecked():
            rehearsal = {"tunnel_id": self.rehearsal_tunnel_combo.currentData() or "",
                         "database": self.rehearsal_database_edit.text().strip(),
                         "schema": self.rehearsal_schema_edit.text().strip()}
            if not rehearsal["tunnel_id"] or not rehearsal["schema"]:
                QMessageBox.warning(self, "입력 오류", "복원 리허설 대상 터널과 스키마를 지정하세요.")
                return
        task_fields = self._validate_and_build_backup_task(schema)
        if task_fields is None:
            return

        cron_expr = self._get_cron_expression()
        if not cron_expr:
            QMessageBox.warning(self, "입력 오류", "스케줄을 설정하세요.")
            return

        # Cron 유효성 검사 (형식 + 최소 실행 간격)
        error = validate_expression(cron_expr, self.min_interval_minutes)
        if error:
            QMessageBox.warning(self, "입력 오류", error)
            return
        next_run = CronParser.get_next_run(cron_expr)

        # ScheduleConfig 생성
        self.result_config = ScheduleConfig(
            id=self.schedule.id if self.schedule else str(uuid.uuid4()),
            name=name,
            tunnel_id=self.tunnel_combo.currentData(),
            schema=schema,
            database=self.database_combo.currentText().strip() if self._tunnel_is_postgresql() else "",
            tables=task_fields["tables"],
            output_dir=task_fields["output_dir"],
            cron_expression=cron_expr,
            enabled=self.enabled_check.isChecked(),
            retention_count=task_fields["retention_count"],
            retention_days=task_fields["retention_days"],
            catch_up_missed=self.catch_up_check.isChecked(),
            rehearsal_tunnel_id=rehearsal["tunnel_id"],
            rehearsal_database=rehearsal["database"],
            rehearsal_schema=rehearsal["schema"],
            last_run=self.schedule.last_run if self.schedule else None,
            next_run=next_run.isoformat(),
            task_type=ScheduleTaskType.BACKUP.value,
        )

        self.accept()

    def _validate_and_build_backup_task(self, schema: str) -> Optional[dict]:
        if not schema:
            QMessageBox.warning(self, "입력 오류", "스키마를 입력하세요.")
            self.schema_edit.setFocus()
            return None

        output_dir = self.output_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "입력 오류", "출력 경로를 선택하세요.")
            return None

        tables_text = self.tables_edit.text().strip()
        tables = [t.strip() for t in tables_text.split(',') if t.strip()] if tables_text else []

        return {
            "tables": tables,
            "output_dir": output_dir,
            "retention_count": self.retention_count_spin.value(),
            "retention_days": self.retention_days_spin.value(),
        }


class ScheduleListDialog(QDialog):
    """스케줄 목록 관리 다이얼로그"""

    schedule_changed = pyqtSignal()
    # BackupScheduler 콜백은 백그라운드 실행 스레드에서 호출되므로,
    # 시그널을 거쳐 GUI 스레드로 안전하게 전달한다 (Qt Queued Connection).
    _execution_finished = pyqtSignal(str, bool, str)

    def __init__(self, parent=None, scheduler: BackupScheduler = None,
                 tunnel_list: List[tuple] = None, tunnel_engines: dict = None, tunnel_environments: dict = None):
        """
        Args:
            parent: 부모 위젯
            scheduler: BackupScheduler 인스턴스
            tunnel_list: [(tunnel_id, tunnel_name), ...] 터널 목록
            tunnel_engines: {tunnel_id: db_engine}
        """
        super().__init__(parent)
        self.tunnel_engines = tunnel_engines or {}
        self.tunnel_environments = tunnel_environments or {}
        self.scheduler = scheduler
        self.tunnel_list = tunnel_list or []
        self._refreshing = False

        self._setup_ui()
        self._connect_signals()
        self._refresh_table()

        if self.scheduler:
            self.scheduler.add_callback(self._on_schedule_completed)

    def _setup_ui(self):
        """UI 구성"""
        self.setWindowTitle("스케줄 작업 관리")
        self.setMinimumSize(800, 450)

        layout = QVBoxLayout(self)

        # 탭 위젯
        tabs = QTabWidget()

        # 스케줄 목록 탭
        schedule_tab = QWidget()
        schedule_layout = QVBoxLayout(schedule_tab)

        # 테이블
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            "유형", "이름", "스케줄", "다음 실행", "마지막 실행", "상태", "활성화"
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        schedule_layout.addWidget(self.table)

        # 버튼
        btn_layout = QHBoxLayout()

        self.add_btn = QPushButton("추가")
        self.add_btn.clicked.connect(self._add_schedule)
        btn_layout.addWidget(self.add_btn)

        self.edit_btn = QPushButton("수정")
        self.edit_btn.clicked.connect(self._edit_schedule)
        btn_layout.addWidget(self.edit_btn)

        self.delete_btn = QPushButton("삭제")
        self.delete_btn.clicked.connect(self._delete_schedule)
        btn_layout.addWidget(self.delete_btn)

        btn_layout.addStretch()

        self.run_now_btn = QPushButton("즉시 실행")
        self.run_now_btn.clicked.connect(self._run_now)
        btn_layout.addWidget(self.run_now_btn)

        self.refresh_btn = QPushButton("새로고침")
        self.refresh_btn.clicked.connect(self._refresh_table)
        btn_layout.addWidget(self.refresh_btn)

        schedule_layout.addLayout(btn_layout)
        tabs.addTab(schedule_tab, "스케줄 목록")

        # 백업 로그 탭
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)

        log_btn_layout = QHBoxLayout()
        log_btn_layout.addStretch()
        self.refresh_log_btn = QPushButton("로그 새로고침")
        self.refresh_log_btn.clicked.connect(self._refresh_logs)
        log_btn_layout.addWidget(self.refresh_log_btn)
        log_layout.addLayout(log_btn_layout)

        tabs.addTab(log_tab, "실행 로그")

        layout.addWidget(tabs)

        # 닫기 버튼
        close_layout = QHBoxLayout()
        close_layout.addStretch()
        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.accept)
        close_layout.addWidget(self.close_btn)
        layout.addLayout(close_layout)

    def _connect_signals(self):
        """시그널 연결"""
        self.table.cellDoubleClicked.connect(self._edit_schedule)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self._execution_finished.connect(self._handle_execution_finished)
        self.finished.connect(self._on_dialog_finished)

    def _update_buttons(self):
        """버튼 상태 업데이트"""
        has_selection = len(self.table.selectedItems()) > 0
        self.edit_btn.setEnabled(has_selection)
        self.delete_btn.setEnabled(has_selection)
        self.run_now_btn.setEnabled(has_selection)

    def _refresh_table(self):
        """테이블 새로고침

        체크박스 아이템을 다시 세팅하는 동안 itemChanged가 발생하므로,
        재진입 방지를 위해 _refreshing 플래그로 감싼다.
        """
        self._refreshing = True
        try:
            self._refresh_table_inner()
        finally:
            self._refreshing = False

    def _refresh_table_inner(self):
        self.table.setRowCount(0)

        if not self.scheduler:
            return

        schedules = self.scheduler.get_schedules()

        for schedule in schedules:
            row = self.table.rowCount()
            self.table.insertRow(row)

            # 유형 (아이콘으로 구분)
            if schedule.is_sql_query_task():
                type_item = QTableWidgetItem("📝 SQL")
                type_item.setToolTip("SQL 쿼리 실행")
            else:
                type_item = QTableWidgetItem("🗄️ 백업")
                type_item.setToolTip("Rust DB Core Export")
            self.table.setItem(row, 0, type_item)

            # 이름
            self.table.setItem(row, 1, QTableWidgetItem(schedule.name))

            # 스케줄 (Cron 설명)
            cron_desc = CronParser.describe(schedule.cron_expression)
            self.table.setItem(row, 2, QTableWidgetItem(cron_desc))

            # 다음 실행
            if schedule.next_run:
                try:
                    next_run = datetime.fromisoformat(schedule.next_run)
                    self.table.setItem(row, 3, QTableWidgetItem(
                        next_run.strftime('%Y-%m-%d %H:%M')
                    ))
                except ValueError:
                    self.table.setItem(row, 3, QTableWidgetItem("-"))
            else:
                self.table.setItem(row, 3, QTableWidgetItem("-"))

            # 마지막 실행
            if schedule.last_run:
                try:
                    last_run = datetime.fromisoformat(schedule.last_run)
                    self.table.setItem(row, 4, QTableWidgetItem(
                        last_run.strftime('%Y-%m-%d %H:%M')
                    ))
                except ValueError:
                    self.table.setItem(row, 4, QTableWidgetItem("-"))
            else:
                self.table.setItem(row, 4, QTableWidgetItem("-"))

            # 상태 (예약 SQL 실행은 지원되지 않아 실행되지 않는다)
            if schedule.is_sql_query_task():
                status = "지원 중단 (실행 안 됨)"
            else:
                status = "대기 중" if schedule.enabled else "비활성"
            self.table.setItem(row, 5, QTableWidgetItem(status))

            # 활성화 체크박스
            enabled_item = QTableWidgetItem()
            enabled_item.setCheckState(
                Qt.CheckState.Checked if schedule.enabled else Qt.CheckState.Unchecked
            )
            enabled_item.setData(Qt.ItemDataRole.UserRole, schedule.id)
            self.table.setItem(row, 6, enabled_item)

        self._update_buttons()

    def _get_selected_schedule_id(self) -> Optional[str]:
        """선택된 스케줄 ID 반환"""
        selected = self.table.selectedItems()
        if not selected:
            return None

        row = selected[0].row()
        id_item = self.table.item(row, 6)  # 유형 컬럼 추가로 인덱스 변경
        return id_item.data(Qt.ItemDataRole.UserRole) if id_item else None

    def _on_table_item_changed(self, item: QTableWidgetItem):
        """활성화 체크박스 토글을 scheduler.set_enabled()에 반영

        _refresh_table_inner()가 체크박스를 다시 세팅할 때도 itemChanged가
        발생하므로 _refreshing 플래그로 사용자 클릭과 구분한다.
        """
        if self._refreshing or item.column() != 6 or not self.scheduler:
            return

        schedule_id = item.data(Qt.ItemDataRole.UserRole)
        if not schedule_id:
            return

        enabled = item.checkState() == Qt.CheckState.Checked
        try:
            self.scheduler.set_enabled(schedule_id, enabled)
            self.schedule_changed.emit()
        except Exception as e:
            QMessageBox.critical(self, "오류", f"스케줄 상태 변경 실패: {e}")

        self._refresh_table()

    def _on_schedule_completed(self, name: str, success: bool, message: str):
        """BackupScheduler 콜백 (백그라운드 실행 스레드에서 호출)

        GUI를 직접 건드리지 않고 시그널만 emit해 GUI 스레드로 넘긴다.
        """
        self._execution_finished.emit(name, success, message)

    def _handle_execution_finished(self, name: str, success: bool, message: str):
        """실행 완료 알림 (GUI 스레드, _execution_finished 시그널 슬롯)

        run_now는 등록만 하고 비동기로 실행되므로, 실제 완료 시점에
        목록/로그를 갱신해 최신 상태를 보여준다. 예약된(cron) 실행도
        같은 콜백을 타므로 팝업 없이 조용히 갱신만 한다.
        """
        self._refresh_table()
        self._refresh_logs()

    def _on_dialog_finished(self, result: int):
        """다이얼로그 종료 시 콜백 해제 (누수 방지)"""
        if self.scheduler:
            self.scheduler.remove_callback(self._on_schedule_completed)

    def _add_schedule(self):
        """스케줄 추가"""
        dialog = ScheduleEditDialog(self, self.tunnel_list, min_interval_minutes=self.scheduler.min_interval_minutes(),
                                    tunnel_engines=self.tunnel_engines, database_lister=self.scheduler.list_databases,
                                    tunnel_environments=self.tunnel_environments)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_config:
            try:
                self.scheduler.add_schedule(dialog.result_config)
                self._refresh_table()
                self.schedule_changed.emit()
            except Exception as e:
                QMessageBox.critical(self, "오류", f"스케줄 추가 실패: {e}")

    def _edit_schedule(self):
        """스케줄 수정"""
        schedule_id = self._get_selected_schedule_id()
        if not schedule_id:
            return

        schedule = self.scheduler.get_schedule(schedule_id)
        if not schedule:
            return

        dialog = ScheduleEditDialog(self, self.tunnel_list, schedule,
                                    min_interval_minutes=self.scheduler.min_interval_minutes(),
                                    tunnel_engines=self.tunnel_engines, database_lister=self.scheduler.list_databases,
                                    tunnel_environments=self.tunnel_environments)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_config:
            try:
                self.scheduler.update_schedule(dialog.result_config)
                self._refresh_table()
                self.schedule_changed.emit()
            except Exception as e:
                QMessageBox.critical(self, "오류", f"스케줄 수정 실패: {e}")

    def _delete_schedule(self):
        """스케줄 삭제"""
        schedule_id = self._get_selected_schedule_id()
        if not schedule_id:
            return

        schedule = self.scheduler.get_schedule(schedule_id)
        if not schedule:
            return

        reply = QMessageBox.question(
            self, "삭제 확인",
            f"스케줄 '{schedule.name}'을(를) 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                self.scheduler.remove_schedule(schedule_id)
                self._refresh_table()
                self.schedule_changed.emit()
            except Exception as e:
                QMessageBox.critical(self, "오류", f"스케줄 삭제 실패: {e}")

    def _run_now(self):
        """즉시 실행"""
        schedule_id = self._get_selected_schedule_id()
        if not schedule_id:
            return

        schedule = self.scheduler.get_schedule(schedule_id)
        if not schedule:
            return

        task_type = "SQL 쿼리" if schedule.is_sql_query_task() else "백업"
        reply = QMessageBox.question(
            self, "즉시 실행",
            f"'{schedule.name}' {task_type}을(를) 지금 실행하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            # run_now는 실행 큐에 등록만 하고 즉시 반환한다 (비동기).
            # 실제 완료 여부는 scheduler의 add_callback 통지로 이 다이얼로그가
            # 받아 _handle_execution_finished에서 표/로그를 갱신한다.
            success, message = self.scheduler.run_now(schedule_id)
            if success:
                QMessageBox.information(self, translate_text("실행 등록됨"), message)
            else:
                QMessageBox.warning(self, "실행 실패", message)
            self._refresh_table()
            self._refresh_logs()

    def _refresh_logs(self):
        """실행 로그 새로고침"""
        if not self.scheduler:
            return

        logs = self.scheduler.get_backup_logs(days=7)

        self.log_text.clear()
        if not logs:
            self.log_text.setPlainText(translate_text("실행 로그가 없습니다."))
            return

        lines = []
        for log in logs:
            status_icon = "✅" if log['status'] == "성공" else "❌"
            lines.append(f"[{log['timestamp']}] {status_icon} {log['name']}: {log['message']}")

        self.log_text.setPlainText("\n".join(lines))
