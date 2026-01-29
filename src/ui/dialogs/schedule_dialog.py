"""
스케줄 백업 관리 다이얼로그
- 스케줄 추가/수정
- 스케줄 목록 관리
"""
import os
import uuid
from datetime import datetime
from typing import Optional, List

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QComboBox, QSpinBox, QCheckBox,
    QPushButton, QGroupBox, QRadioButton, QButtonGroup,
    QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QWidget, QTimeEdit, QTabWidget, QTextEdit
)
from PyQt6.QtCore import Qt, QTime, pyqtSignal
from PyQt6.QtGui import QIcon

from src.core.scheduler import ScheduleConfig, CronParser, BackupScheduler
from src.core.logger import get_logger

logger = get_logger(__name__)


class ScheduleEditDialog(QDialog):
    """스케줄 추가/수정 다이얼로그"""

    def __init__(self, parent=None, tunnel_list: List[tuple] = None,
                 schedule: ScheduleConfig = None):
        """
        Args:
            parent: 부모 위젯
            tunnel_list: [(tunnel_id, tunnel_name), ...] 터널 목록
            schedule: 수정할 스케줄 (None이면 새로 생성)
        """
        super().__init__(parent)
        self.tunnel_list = tunnel_list or []
        self.schedule = schedule
        self.result_config: Optional[ScheduleConfig] = None

        self._setup_ui()
        self._connect_signals()

        if schedule:
            self._load_schedule(schedule)

    def _setup_ui(self):
        """UI 구성"""
        self.setWindowTitle("스케줄 백업 추가" if not self.schedule else "스케줄 백업 수정")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)

        # 기본 정보
        basic_group = QGroupBox("기본 정보")
        basic_layout = QFormLayout(basic_group)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("백업 작업 이름")
        basic_layout.addRow("이름:", self.name_edit)

        self.tunnel_combo = QComboBox()
        for tunnel_id, tunnel_name in self.tunnel_list:
            self.tunnel_combo.addItem(tunnel_name, tunnel_id)
        basic_layout.addRow("터널:", self.tunnel_combo)

        self.schema_edit = QLineEdit()
        self.schema_edit.setPlaceholderText("백업할 데이터베이스 이름")
        basic_layout.addRow("스키마:", self.schema_edit)

        self.tables_edit = QLineEdit()
        self.tables_edit.setPlaceholderText("테이블1, 테이블2, ... (비워두면 전체)")
        basic_layout.addRow("테이블:", self.tables_edit)

        # 출력 디렉토리
        output_layout = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("백업 파일 저장 위치")
        output_layout.addWidget(self.output_edit)
        self.browse_btn = QPushButton("찾아보기...")
        self.browse_btn.clicked.connect(self._browse_output_dir)
        output_layout.addWidget(self.browse_btn)
        basic_layout.addRow("출력 경로:", output_layout)

        layout.addWidget(basic_group)

        # 스케줄 설정
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
        self.daily_radio.setChecked(True)

        self.schedule_type_group.addButton(self.daily_radio, 0)
        self.schedule_type_group.addButton(self.weekly_radio, 1)
        self.schedule_type_group.addButton(self.monthly_radio, 2)

        types_layout.addWidget(self.daily_radio)
        types_layout.addWidget(self.weekly_radio)
        types_layout.addWidget(self.monthly_radio)
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

        # 시간 선택
        time_layout = QHBoxLayout()
        time_layout.addWidget(QLabel("시간:"))
        self.time_edit = QTimeEdit()
        self.time_edit.setTime(QTime(3, 0))  # 기본 03:00
        self.time_edit.setDisplayFormat("HH:mm")
        time_layout.addWidget(self.time_edit)
        time_layout.addStretch()
        simple_layout.addLayout(time_layout)

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
            "  30 6 * * 1-5 = 평일 06:30"
        )
        help_text.setStyleSheet("color: gray; font-size: 11px;")
        advanced_layout.addWidget(help_text)

        advanced_layout.addStretch()
        self.schedule_tabs.addTab(advanced_tab, "고급 설정")

        schedule_layout.addWidget(self.schedule_tabs)
        layout.addWidget(schedule_group)

        # 보관 정책
        retention_group = QGroupBox("보관 정책")
        retention_layout = QFormLayout(retention_group)

        self.retention_count_spin = QSpinBox()
        self.retention_count_spin.setRange(1, 100)
        self.retention_count_spin.setValue(5)
        retention_layout.addRow("최대 백업 수:", self.retention_count_spin)

        self.retention_days_spin = QSpinBox()
        self.retention_days_spin.setRange(1, 365)
        self.retention_days_spin.setValue(30)
        retention_layout.addRow("보관 기간 (일):", self.retention_days_spin)

        layout.addWidget(retention_group)

        # 활성화 체크박스
        self.enabled_check = QCheckBox("스케줄 활성화")
        self.enabled_check.setChecked(True)
        layout.addWidget(self.enabled_check)

        # 버튼
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

    def _connect_signals(self):
        """시그널 연결"""
        self.schedule_type_group.idClicked.connect(self._on_schedule_type_changed)
        self.cron_edit.textChanged.connect(self._on_cron_changed)

    def _on_schedule_type_changed(self, button_id: int):
        """스케줄 타입 변경"""
        self.dow_widget.setVisible(button_id == 1)  # 매주
        self.day_widget.setVisible(button_id == 2)  # 매월

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
        current = self.output_edit.text() or os.path.expanduser("~")
        dir_path = QFileDialog.getExistingDirectory(
            self, "백업 저장 위치 선택", current
        )
        if dir_path:
            self.output_edit.setText(dir_path)

    def _load_schedule(self, schedule: ScheduleConfig):
        """기존 스케줄 로드"""
        self.name_edit.setText(schedule.name)

        # 터널 선택
        for i in range(self.tunnel_combo.count()):
            if self.tunnel_combo.itemData(i) == schedule.tunnel_id:
                self.tunnel_combo.setCurrentIndex(i)
                break

        self.schema_edit.setText(schedule.schema)
        self.tables_edit.setText(", ".join(schedule.tables) if schedule.tables else "")
        self.output_edit.setText(schedule.output_dir)

        # Cron 표현식
        self.cron_edit.setText(schedule.cron_expression)
        self.schedule_tabs.setCurrentIndex(1)  # 고급 탭

        self.retention_count_spin.setValue(schedule.retention_count)
        self.retention_days_spin.setValue(schedule.retention_days)
        self.enabled_check.setChecked(schedule.enabled)

    def _get_cron_expression(self) -> str:
        """설정에서 Cron 표현식 생성"""
        if self.schedule_tabs.currentIndex() == 1:  # 고급 탭
            return self.cron_edit.text().strip()

        # 간편 설정에서 생성
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
        # 유효성 검사
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "입력 오류", "이름을 입력하세요.")
            self.name_edit.setFocus()
            return

        if self.tunnel_combo.currentIndex() < 0:
            QMessageBox.warning(self, "입력 오류", "터널을 선택하세요.")
            return

        schema = self.schema_edit.text().strip()
        if not schema:
            QMessageBox.warning(self, "입력 오류", "스키마를 입력하세요.")
            self.schema_edit.setFocus()
            return

        output_dir = self.output_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "입력 오류", "출력 경로를 선택하세요.")
            return

        cron_expr = self._get_cron_expression()
        if not cron_expr:
            QMessageBox.warning(self, "입력 오류", "스케줄을 설정하세요.")
            return

        # Cron 유효성 검사
        next_run = CronParser.get_next_run(cron_expr)
        if not next_run:
            QMessageBox.warning(self, "입력 오류", "잘못된 Cron 표현식입니다.")
            return

        # 테이블 목록
        tables_text = self.tables_edit.text().strip()
        tables = [t.strip() for t in tables_text.split(',') if t.strip()] if tables_text else []

        # ScheduleConfig 생성
        self.result_config = ScheduleConfig(
            id=self.schedule.id if self.schedule else str(uuid.uuid4()),
            name=name,
            tunnel_id=self.tunnel_combo.currentData(),
            schema=schema,
            tables=tables,
            output_dir=output_dir,
            cron_expression=cron_expr,
            enabled=self.enabled_check.isChecked(),
            retention_count=self.retention_count_spin.value(),
            retention_days=self.retention_days_spin.value(),
            last_run=self.schedule.last_run if self.schedule else None,
            next_run=next_run.isoformat()
        )

        self.accept()


class ScheduleListDialog(QDialog):
    """스케줄 목록 관리 다이얼로그"""

    schedule_changed = pyqtSignal()

    def __init__(self, parent=None, scheduler: BackupScheduler = None,
                 tunnel_list: List[tuple] = None):
        """
        Args:
            parent: 부모 위젯
            scheduler: BackupScheduler 인스턴스
            tunnel_list: [(tunnel_id, tunnel_name), ...] 터널 목록
        """
        super().__init__(parent)
        self.scheduler = scheduler
        self.tunnel_list = tunnel_list or []

        self._setup_ui()
        self._connect_signals()
        self._refresh_table()

    def _setup_ui(self):
        """UI 구성"""
        self.setWindowTitle("스케줄 백업 관리")
        self.setMinimumSize(700, 400)

        layout = QVBoxLayout(self)

        # 탭 위젯
        tabs = QTabWidget()

        # 스케줄 목록 탭
        schedule_tab = QWidget()
        schedule_layout = QVBoxLayout(schedule_tab)

        # 테이블
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "이름", "스케줄", "다음 실행", "마지막 실행", "상태", "활성화"
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

        tabs.addTab(log_tab, "백업 로그")

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

    def _update_buttons(self):
        """버튼 상태 업데이트"""
        has_selection = len(self.table.selectedItems()) > 0
        self.edit_btn.setEnabled(has_selection)
        self.delete_btn.setEnabled(has_selection)
        self.run_now_btn.setEnabled(has_selection)

    def _refresh_table(self):
        """테이블 새로고침"""
        self.table.setRowCount(0)

        if not self.scheduler:
            return

        schedules = self.scheduler.get_schedules()

        for schedule in schedules:
            row = self.table.rowCount()
            self.table.insertRow(row)

            # 이름
            self.table.setItem(row, 0, QTableWidgetItem(schedule.name))

            # 스케줄 (Cron 설명)
            cron_desc = CronParser.describe(schedule.cron_expression)
            self.table.setItem(row, 1, QTableWidgetItem(cron_desc))

            # 다음 실행
            if schedule.next_run:
                try:
                    next_run = datetime.fromisoformat(schedule.next_run)
                    self.table.setItem(row, 2, QTableWidgetItem(
                        next_run.strftime('%Y-%m-%d %H:%M')
                    ))
                except:
                    self.table.setItem(row, 2, QTableWidgetItem("-"))
            else:
                self.table.setItem(row, 2, QTableWidgetItem("-"))

            # 마지막 실행
            if schedule.last_run:
                try:
                    last_run = datetime.fromisoformat(schedule.last_run)
                    self.table.setItem(row, 3, QTableWidgetItem(
                        last_run.strftime('%Y-%m-%d %H:%M')
                    ))
                except:
                    self.table.setItem(row, 3, QTableWidgetItem("-"))
            else:
                self.table.setItem(row, 3, QTableWidgetItem("-"))

            # 상태
            status = "대기 중" if schedule.enabled else "비활성"
            self.table.setItem(row, 4, QTableWidgetItem(status))

            # 활성화 체크박스
            enabled_item = QTableWidgetItem()
            enabled_item.setCheckState(
                Qt.CheckState.Checked if schedule.enabled else Qt.CheckState.Unchecked
            )
            enabled_item.setData(Qt.ItemDataRole.UserRole, schedule.id)
            self.table.setItem(row, 5, enabled_item)

        self._update_buttons()

    def _get_selected_schedule_id(self) -> Optional[str]:
        """선택된 스케줄 ID 반환"""
        selected = self.table.selectedItems()
        if not selected:
            return None

        row = selected[0].row()
        id_item = self.table.item(row, 5)
        return id_item.data(Qt.ItemDataRole.UserRole) if id_item else None

    def _add_schedule(self):
        """스케줄 추가"""
        dialog = ScheduleEditDialog(self, self.tunnel_list)
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

        dialog = ScheduleEditDialog(self, self.tunnel_list, schedule)
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

        reply = QMessageBox.question(
            self, "즉시 실행",
            f"스케줄 '{schedule.name}'을(를) 지금 실행하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            success, message = self.scheduler.run_now(schedule_id)
            if success:
                QMessageBox.information(self, "백업 완료", message)
            else:
                QMessageBox.warning(self, "백업 실패", message)
            self._refresh_table()
            self._refresh_logs()

    def _refresh_logs(self):
        """백업 로그 새로고침"""
        if not self.scheduler:
            return

        logs = self.scheduler.get_backup_logs(days=7)

        self.log_text.clear()
        if not logs:
            self.log_text.setPlainText("백업 로그가 없습니다.")
            return

        lines = []
        for log in logs:
            status_icon = "✅" if log['status'] == "성공" else "❌"
            lines.append(f"[{log['timestamp']}] {status_icon} {log['name']}: {log['message']}")

        self.log_text.setPlainText("\n".join(lines))
