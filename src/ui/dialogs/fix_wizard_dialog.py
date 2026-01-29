"""
마이그레이션 자동 수정 위저드 UI

4단계 QWizard:
1. IssueSelectionPage: 수정할 이슈 선택
2. FixOptionPage: 이슈별 수정 옵션 선택
3. PreviewPage: SQL 미리보기 및 Dry-run
4. ExecutionPage: 실제 실행 및 결과 표시
"""

from PyQt6.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QRadioButton, QCheckBox,
    QButtonGroup, QGroupBox, QTextEdit, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
    QWidget, QFrame, QSplitter, QMessageBox, QApplication
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QColor
from typing import List, Optional, Dict

from src.core.db_connector import MySQLConnector
from src.core.migration_analyzer import CompatibilityIssue
from src.core.migration_constants import IssueType
from src.core.migration_fix_wizard import (
    FixStrategy, FixOption, FixWizardStep,
    SmartFixGenerator, BatchFixExecutor, create_wizard_steps
)
from src.ui.workers.fix_wizard_worker import FixWizardWorker


class FixWizardDialog(QWizard):
    """마이그레이션 자동 수정 위저드"""

    def __init__(
        self,
        parent=None,
        connector: MySQLConnector = None,
        issues: List[CompatibilityIssue] = None,
        schema: str = ""
    ):
        super().__init__(parent)
        self.connector = connector
        self.issues = issues or []
        self.schema = schema

        # 위저드 단계 생성
        self.wizard_steps: List[FixWizardStep] = []
        self.selected_issues: List[CompatibilityIssue] = []

        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("🔧 마이그레이션 자동 수정 위저드")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.resize(900, 650)

        # 페이지 추가
        self.issue_page = IssueSelectionPage(self)
        self.option_page = FixOptionPage(self)
        self.preview_page = PreviewPage(self)
        self.execution_page = ExecutionPage(self)

        self.addPage(self.issue_page)
        self.addPage(self.option_page)
        self.addPage(self.preview_page)
        self.addPage(self.execution_page)

        # 버튼 텍스트 변경
        self.setButtonText(QWizard.WizardButton.NextButton, "다음 >")
        self.setButtonText(QWizard.WizardButton.BackButton, "< 이전")
        self.setButtonText(QWizard.WizardButton.FinishButton, "완료")
        self.setButtonText(QWizard.WizardButton.CancelButton, "취소")


class IssueSelectionPage(QWizardPage):
    """1단계: 수정할 이슈 선택"""

    def __init__(self, wizard: FixWizardDialog):
        super().__init__(wizard)
        self.wizard_dialog = wizard

        self.setTitle("수정할 이슈 선택")
        self.setSubTitle("자동 수정을 적용할 호환성 이슈를 선택하세요.")

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 필터
        filter_group = QGroupBox("필터")
        filter_layout = QHBoxLayout(filter_group)

        self.chk_error = QCheckBox("Error")
        self.chk_error.setChecked(True)
        self.chk_error.stateChanged.connect(self.filter_issues)

        self.chk_warning = QCheckBox("Warning")
        self.chk_warning.setChecked(True)
        self.chk_warning.stateChanged.connect(self.filter_issues)

        self.chk_auto_fixable = QCheckBox("자동 수정 가능만")
        self.chk_auto_fixable.setChecked(False)
        self.chk_auto_fixable.stateChanged.connect(self.filter_issues)

        filter_layout.addWidget(self.chk_error)
        filter_layout.addWidget(self.chk_warning)
        filter_layout.addWidget(self.chk_auto_fixable)
        filter_layout.addStretch()

        layout.addWidget(filter_group)

        # 이슈 테이블
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            "선택", "심각도", "유형", "위치", "설명"
        ])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 50)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)

        # 전체 선택/해제
        btn_layout = QHBoxLayout()

        btn_select_all = QPushButton("전체 선택")
        btn_select_all.clicked.connect(self.select_all)

        btn_deselect_all = QPushButton("전체 해제")
        btn_deselect_all.clicked.connect(self.deselect_all)

        self.lbl_count = QLabel("선택: 0개")

        btn_layout.addWidget(btn_select_all)
        btn_layout.addWidget(btn_deselect_all)
        btn_layout.addStretch()
        btn_layout.addWidget(self.lbl_count)

        layout.addLayout(btn_layout)

        # 체크박스 목록 (테이블 내부)
        self.checkboxes: List[QCheckBox] = []

    def initializePage(self):
        """페이지 초기화 시"""
        self.populate_table()

    def populate_table(self):
        """이슈 테이블 채우기"""
        issues = self.wizard_dialog.issues
        self.table.setRowCount(len(issues))
        self.checkboxes.clear()

        # 자동 수정 가능한 이슈 타입
        auto_fixable_types = {
            IssueType.INVALID_DATE,
            IssueType.CHARSET_ISSUE,
            IssueType.ZEROFILL_USAGE,
            IssueType.FLOAT_PRECISION,
            IssueType.INT_DISPLAY_WIDTH,
            IssueType.DEPRECATED_ENGINE,
            IssueType.ENUM_EMPTY_VALUE,
        }

        type_names = {
            IssueType.INVALID_DATE: "잘못된 날짜",
            IssueType.CHARSET_ISSUE: "문자셋",
            IssueType.ZEROFILL_USAGE: "ZEROFILL",
            IssueType.FLOAT_PRECISION: "FLOAT 정밀도",
            IssueType.INT_DISPLAY_WIDTH: "INT 표시 너비",
            IssueType.DEPRECATED_ENGINE: "deprecated 엔진",
            IssueType.ENUM_EMPTY_VALUE: "ENUM 빈 값",
            IssueType.AUTH_PLUGIN_ISSUE: "인증 플러그인",
            IssueType.RESERVED_KEYWORD: "예약어",
            IssueType.FK_NAME_LENGTH: "FK 이름 길이",
        }

        for i, issue in enumerate(issues):
            # 체크박스
            chk = QCheckBox()
            chk.stateChanged.connect(self.update_count)
            self.checkboxes.append(chk)

            # 체크박스를 셀에 배치
            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(i, 0, chk_widget)

            # 심각도
            severity_item = QTableWidgetItem(issue.severity.upper())
            if issue.severity == "error":
                severity_item.setForeground(QColor("#e74c3c"))
            elif issue.severity == "warning":
                severity_item.setForeground(QColor("#f39c12"))
            self.table.setItem(i, 1, severity_item)

            # 유형
            type_name = type_names.get(issue.issue_type, str(issue.issue_type.value))
            type_item = QTableWidgetItem(type_name)

            # 자동 수정 가능 표시
            if issue.issue_type in auto_fixable_types:
                type_item.setText(f"✨ {type_name}")
                type_item.setToolTip("자동 수정 가능")
            self.table.setItem(i, 2, type_item)

            # 위치
            self.table.setItem(i, 3, QTableWidgetItem(issue.location))

            # 설명
            self.table.setItem(i, 4, QTableWidgetItem(issue.description))

        self.filter_issues()

    def filter_issues(self):
        """이슈 필터링"""
        show_error = self.chk_error.isChecked()
        show_warning = self.chk_warning.isChecked()
        auto_fixable_only = self.chk_auto_fixable.isChecked()

        auto_fixable_types = {
            IssueType.INVALID_DATE,
            IssueType.CHARSET_ISSUE,
            IssueType.ZEROFILL_USAGE,
            IssueType.FLOAT_PRECISION,
            IssueType.INT_DISPLAY_WIDTH,
            IssueType.DEPRECATED_ENGINE,
            IssueType.ENUM_EMPTY_VALUE,
        }

        for i, issue in enumerate(self.wizard_dialog.issues):
            visible = True

            # 심각도 필터
            if issue.severity == "error" and not show_error:
                visible = False
            elif issue.severity == "warning" and not show_warning:
                visible = False

            # 자동 수정 가능 필터
            if auto_fixable_only and issue.issue_type not in auto_fixable_types:
                visible = False

            self.table.setRowHidden(i, not visible)

        self.update_count()

    def select_all(self):
        """전체 선택"""
        for i, chk in enumerate(self.checkboxes):
            if not self.table.isRowHidden(i):
                chk.setChecked(True)

    def deselect_all(self):
        """전체 해제"""
        for chk in self.checkboxes:
            chk.setChecked(False)

    def update_count(self):
        """선택 개수 업데이트"""
        count = sum(1 for i, chk in enumerate(self.checkboxes)
                    if chk.isChecked() and not self.table.isRowHidden(i))
        self.lbl_count.setText(f"선택: {count}개")
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        """다음 단계 진행 가능 여부"""
        return any(chk.isChecked() for chk in self.checkboxes)

    def validatePage(self) -> bool:
        """페이지 유효성 검사 및 데이터 전달"""
        # 선택된 이슈 추출
        selected = []
        for i, chk in enumerate(self.checkboxes):
            if chk.isChecked() and not self.table.isRowHidden(i):
                selected.append(self.wizard_dialog.issues[i])

        self.wizard_dialog.selected_issues = selected

        # 위저드 단계 생성
        self.wizard_dialog.wizard_steps = create_wizard_steps(
            selected,
            self.wizard_dialog.connector,
            self.wizard_dialog.schema
        )

        return True


class FixOptionPage(QWizardPage):
    """2단계: 이슈별 수정 옵션 선택"""

    def __init__(self, wizard: FixWizardDialog):
        super().__init__(wizard)
        self.wizard_dialog = wizard

        self.setTitle("수정 옵션 선택")
        self.setSubTitle("각 이슈에 대한 수정 방법을 선택하세요.")

        self.current_index = 0
        self.option_buttons: List[QRadioButton] = []
        self.input_field: Optional[QLineEdit] = None

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 진행 표시
        progress_layout = QHBoxLayout()
        self.lbl_progress = QLabel("이슈 1 / 1")
        self.lbl_progress.setStyleSheet("font-weight: bold; font-size: 14px;")
        progress_layout.addWidget(self.lbl_progress)
        progress_layout.addStretch()
        layout.addLayout(progress_layout)

        # 이슈 정보
        self.grp_issue = QGroupBox("현재 이슈")
        issue_layout = QFormLayout(self.grp_issue)

        self.lbl_type = QLabel()
        self.lbl_location = QLabel()
        self.lbl_description = QLabel()
        self.lbl_description.setWordWrap(True)

        issue_layout.addRow("유형:", self.lbl_type)
        issue_layout.addRow("위치:", self.lbl_location)
        issue_layout.addRow("설명:", self.lbl_description)

        layout.addWidget(self.grp_issue)

        # 옵션 선택
        self.grp_options = QGroupBox("수정 옵션")
        self.options_layout = QVBoxLayout(self.grp_options)
        self.btn_group = QButtonGroup(self)

        layout.addWidget(self.grp_options)

        # 사용자 입력 필드 (필요 시 표시)
        self.input_group = QGroupBox("추가 입력")
        input_layout = QHBoxLayout(self.input_group)
        self.input_label = QLabel()
        self.input_field = QLineEdit()
        input_layout.addWidget(self.input_label)
        input_layout.addWidget(self.input_field)
        self.input_group.setVisible(False)
        layout.addWidget(self.input_group)

        # 네비게이션
        nav_layout = QHBoxLayout()

        self.btn_prev_issue = QPushButton("< 이전 이슈")
        self.btn_prev_issue.clicked.connect(self.prev_issue)

        self.btn_next_issue = QPushButton("다음 이슈 >")
        self.btn_next_issue.clicked.connect(self.next_issue)

        nav_layout.addWidget(self.btn_prev_issue)
        nav_layout.addStretch()
        nav_layout.addWidget(self.btn_next_issue)

        layout.addLayout(nav_layout)
        layout.addStretch()

    def initializePage(self):
        """페이지 초기화"""
        self.current_index = 0
        self.show_current_issue()

    def show_current_issue(self):
        """현재 이슈 표시"""
        steps = self.wizard_dialog.wizard_steps
        if not steps or self.current_index >= len(steps):
            return

        step = steps[self.current_index]

        # 진행 표시 업데이트
        self.lbl_progress.setText(f"이슈 {self.current_index + 1} / {len(steps)}")

        # 이슈 정보 업데이트
        type_names = {
            IssueType.INVALID_DATE: "잘못된 날짜 (0000-00-00)",
            IssueType.CHARSET_ISSUE: "문자셋 이슈",
            IssueType.ZEROFILL_USAGE: "ZEROFILL 속성",
            IssueType.FLOAT_PRECISION: "FLOAT 정밀도 구문",
            IssueType.INT_DISPLAY_WIDTH: "INT 표시 너비",
            IssueType.DEPRECATED_ENGINE: "deprecated 스토리지 엔진",
            IssueType.ENUM_EMPTY_VALUE: "ENUM 빈 문자열",
        }

        self.lbl_type.setText(type_names.get(step.issue_type, str(step.issue_type.value)))
        self.lbl_location.setText(step.location)
        self.lbl_description.setText(step.description)

        # 기존 옵션 버튼 제거
        for btn in self.option_buttons:
            self.btn_group.removeButton(btn)
            self.options_layout.removeWidget(btn)
            btn.deleteLater()
        self.option_buttons.clear()

        # 새 옵션 버튼 생성
        for i, option in enumerate(step.options):
            # 권장 옵션 표시
            label = option.label
            if option.is_recommended:
                label = f"⭐ {label}"

            radio = QRadioButton(label)
            radio.setToolTip(option.description)

            # 이전에 선택한 옵션이 있으면 복원
            if step.selected_option and step.selected_option.strategy == option.strategy:
                radio.setChecked(True)
            elif i == 0 and not step.selected_option:
                # 첫 번째 옵션 기본 선택
                radio.setChecked(True)

            radio.toggled.connect(lambda checked, opt=option: self.on_option_changed(checked, opt))

            self.btn_group.addButton(radio, i)
            self.options_layout.addWidget(radio)

            # 설명 라벨
            desc_label = QLabel(f"    {option.description}")
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet("color: #666; font-size: 11px;")
            self.options_layout.addWidget(desc_label)

            self.option_buttons.append(radio)

        # 입력 필드 초기화
        self.update_input_field()

        # 네비게이션 버튼 상태
        self.btn_prev_issue.setEnabled(self.current_index > 0)
        self.btn_next_issue.setEnabled(self.current_index < len(steps) - 1)

    def on_option_changed(self, checked: bool, option: FixOption):
        """옵션 변경 시"""
        if checked:
            step = self.wizard_dialog.wizard_steps[self.current_index]
            step.selected_option = option
            self.update_input_field()

    def update_input_field(self):
        """입력 필드 표시/숨김"""
        step = self.wizard_dialog.wizard_steps[self.current_index]
        option = step.selected_option

        if option and option.requires_input:
            self.input_group.setVisible(True)
            self.input_label.setText(option.input_label or "값:")
            self.input_field.setText(step.user_input or option.input_default or "")
        else:
            self.input_group.setVisible(False)

    def save_current_selection(self):
        """현재 선택 저장"""
        step = self.wizard_dialog.wizard_steps[self.current_index]

        # 선택된 옵션 저장
        checked_id = self.btn_group.checkedId()
        if checked_id >= 0 and checked_id < len(step.options):
            step.selected_option = step.options[checked_id]

        # 입력값 저장
        if step.selected_option and step.selected_option.requires_input:
            step.user_input = self.input_field.text()

    def prev_issue(self):
        """이전 이슈"""
        self.save_current_selection()
        if self.current_index > 0:
            self.current_index -= 1
            self.show_current_issue()

    def next_issue(self):
        """다음 이슈"""
        self.save_current_selection()
        if self.current_index < len(self.wizard_dialog.wizard_steps) - 1:
            self.current_index += 1
            self.show_current_issue()

    def validatePage(self) -> bool:
        """페이지 유효성 검사"""
        self.save_current_selection()

        # 모든 이슈에 옵션이 선택되었는지 확인
        for step in self.wizard_dialog.wizard_steps:
            if not step.selected_option:
                QMessageBox.warning(self, "선택 필요", f"'{step.location}'의 수정 옵션을 선택하세요.")
                return False

            # 입력 필드 검증
            if step.selected_option.requires_input and not step.user_input:
                QMessageBox.warning(self, "입력 필요", f"'{step.location}'의 추가 입력값을 입력하세요.")
                return False

        return True


class PreviewPage(QWizardPage):
    """3단계: SQL 미리보기 및 Dry-run"""

    def __init__(self, wizard: FixWizardDialog):
        super().__init__(wizard)
        self.wizard_dialog = wizard
        self.worker: Optional[FixWizardWorker] = None

        self.setTitle("SQL 미리보기")
        self.setSubTitle("생성된 수정 SQL을 확인하고 Dry-run을 실행하세요.")

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # SQL 미리보기
        self.txt_sql = QTextEdit()
        self.txt_sql.setReadOnly(True)
        self.txt_sql.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 12px;
                background-color: #2d2d2d;
                color: #f8f8f2;
                border-radius: 4px;
            }
        """)
        layout.addWidget(self.txt_sql, 2)

        # Dry-run 결과
        self.grp_dryrun = QGroupBox("Dry-run 결과")
        dryrun_layout = QVBoxLayout(self.grp_dryrun)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        dryrun_layout.addWidget(self.progress_bar)

        self.txt_dryrun = QTextEdit()
        self.txt_dryrun.setReadOnly(True)
        self.txt_dryrun.setMaximumHeight(150)
        self.txt_dryrun.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 11px;
            }
        """)
        dryrun_layout.addWidget(self.txt_dryrun)

        layout.addWidget(self.grp_dryrun, 1)

        # 버튼
        btn_layout = QHBoxLayout()

        self.btn_dryrun = QPushButton("🔍 Dry-run 실행")
        self.btn_dryrun.setStyleSheet("""
            QPushButton {
                background-color: #f39c12; color: white; font-weight: bold;
                padding: 10px 20px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #e67e22; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """)
        self.btn_dryrun.clicked.connect(self.run_dryrun)

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_dryrun)

        layout.addLayout(btn_layout)

    def initializePage(self):
        """페이지 초기화"""
        self.generate_sql_preview()

    def generate_sql_preview(self):
        """SQL 미리보기 생성"""
        lines = []
        lines.append("-- ==========================================")
        lines.append("-- 마이그레이션 자동 수정 SQL")
        lines.append(f"-- 스키마: {self.wizard_dialog.schema}")
        lines.append(f"-- 대상: {len(self.wizard_dialog.wizard_steps)}개 이슈")
        lines.append("-- ==========================================")
        lines.append("")

        for i, step in enumerate(self.wizard_dialog.wizard_steps, 1):
            if step.selected_option and step.selected_option.strategy != FixStrategy.SKIP:
                lines.append(f"-- [{i}] {step.location}")
                lines.append(f"-- 전략: {step.selected_option.label}")

                sql = step.selected_option.sql_template or ""
                if step.selected_option.requires_input and step.user_input:
                    sql = sql.replace("{custom_date}", step.user_input)
                    sql = sql.replace("{precision}", step.user_input)

                lines.append(sql)
                lines.append("")

        self.txt_sql.setText("\n".join(lines))
        self.txt_dryrun.clear()

    def run_dryrun(self):
        """Dry-run 실행"""
        self.btn_dryrun.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.txt_dryrun.clear()
        self.txt_dryrun.append("🔍 Dry-run 시작...")

        # 워커 실행
        self.worker = FixWizardWorker(
            connector=self.wizard_dialog.connector,
            schema=self.wizard_dialog.schema,
            steps=self.wizard_dialog.wizard_steps,
            dry_run=True
        )

        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_dryrun_finished)
        self.worker.start()

    def on_progress(self, message: str):
        """진행 메시지"""
        self.txt_dryrun.append(message)

    def on_dryrun_finished(self, success: bool, message: str, result):
        """Dry-run 완료"""
        self.btn_dryrun.setEnabled(True)
        self.progress_bar.setVisible(False)

        if success and result:
            self.txt_dryrun.append("")
            self.txt_dryrun.append("=" * 50)
            self.txt_dryrun.append(f"✅ Dry-run 완료")
            self.txt_dryrun.append(f"  - 성공: {result.success_count}개")
            self.txt_dryrun.append(f"  - 건너뛰기: {result.skip_count}개")
            self.txt_dryrun.append(f"  - 예상 영향 행: {result.total_affected_rows:,}개")
        else:
            self.txt_dryrun.append(f"❌ Dry-run 오류: {message}")


class ExecutionPage(QWizardPage):
    """4단계: 실제 실행 및 결과"""

    def __init__(self, wizard: FixWizardDialog):
        super().__init__(wizard)
        self.wizard_dialog = wizard
        self.worker: Optional[FixWizardWorker] = None
        self.executed = False

        self.setTitle("실행")
        self.setSubTitle("수정 작업을 실행합니다. 이 작업은 되돌릴 수 없습니다.")

        self.setCommitPage(True)  # Commit 버튼 사용

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 경고
        warning_label = QLabel(
            "⚠️ <b>주의:</b> 실행 버튼을 클릭하면 데이터베이스가 수정됩니다. "
            "이 작업은 되돌릴 수 없으니 신중하게 진행하세요."
        )
        warning_label.setWordWrap(True)
        warning_label.setStyleSheet("""
            QLabel {
                background-color: #fff3cd;
                color: #856404;
                padding: 10px;
                border: 1px solid #ffc107;
                border-radius: 4px;
            }
        """)
        layout.addWidget(warning_label)

        # 진행 상황
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        # 실행 로그
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 11px;
            }
        """)
        layout.addWidget(self.txt_log)

        # 결과 요약
        self.grp_result = QGroupBox("실행 결과")
        self.grp_result.setVisible(False)
        result_layout = QFormLayout(self.grp_result)

        self.lbl_total = QLabel()
        self.lbl_success = QLabel()
        self.lbl_fail = QLabel()
        self.lbl_affected = QLabel()

        result_layout.addRow("총 작업:", self.lbl_total)
        result_layout.addRow("성공:", self.lbl_success)
        result_layout.addRow("실패:", self.lbl_fail)
        result_layout.addRow("영향 행:", self.lbl_affected)

        layout.addWidget(self.grp_result)

        # 실행 버튼
        btn_layout = QHBoxLayout()

        self.btn_execute = QPushButton("⚡ 실행")
        self.btn_execute.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c; color: white; font-weight: bold;
                padding: 12px 30px; border-radius: 4px; border: none;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #c0392b; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """)
        self.btn_execute.clicked.connect(self.execute)

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_execute)

        layout.addLayout(btn_layout)

    def initializePage(self):
        """페이지 초기화"""
        self.txt_log.clear()
        self.progress_bar.setValue(0)
        self.grp_result.setVisible(False)
        self.executed = False

        # 실행할 작업 요약
        steps = self.wizard_dialog.wizard_steps
        execute_count = sum(1 for s in steps
                           if s.selected_option and s.selected_option.strategy != FixStrategy.SKIP)

        self.txt_log.append(f"📋 실행 대기 중...")
        self.txt_log.append(f"  - 총 이슈: {len(steps)}개")
        self.txt_log.append(f"  - 실행 예정: {execute_count}개")
        self.txt_log.append(f"  - 건너뛰기: {len(steps) - execute_count}개")
        self.txt_log.append("")
        self.txt_log.append("'실행' 버튼을 클릭하여 수정을 적용하세요.")

    def execute(self):
        """실행"""
        reply = QMessageBox.warning(
            self,
            "실행 확인",
            "선택한 수정 작업을 실행합니다.\n\n"
            "이 작업은 되돌릴 수 없습니다. 계속하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        self.btn_execute.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self.txt_log.clear()
        self.txt_log.append("🔧 실행 시작...")

        # 워커 실행
        self.worker = FixWizardWorker(
            connector=self.wizard_dialog.connector,
            schema=self.wizard_dialog.schema,
            steps=self.wizard_dialog.wizard_steps,
            dry_run=False
        )

        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def on_progress(self, message: str):
        """진행 메시지"""
        self.txt_log.append(message)

    def on_finished(self, success: bool, message: str, result):
        """실행 완료"""
        self.btn_execute.setEnabled(False)  # 다시 실행 방지
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.executed = True

        if success and result:
            self.txt_log.append("")
            self.txt_log.append("=" * 50)
            self.txt_log.append("✅ 실행 완료!")

            # 결과 요약 표시
            self.grp_result.setVisible(True)
            self.lbl_total.setText(str(result.total_steps))
            self.lbl_success.setText(f"{result.success_count}개")
            self.lbl_fail.setText(f"{result.fail_count}개")
            self.lbl_affected.setText(f"{result.total_affected_rows:,}개")

            if result.fail_count > 0:
                self.lbl_fail.setStyleSheet("color: #e74c3c; font-weight: bold;")
        else:
            self.txt_log.append(f"❌ 실행 오류: {message}")

        self.completeChanged.emit()

    def isComplete(self) -> bool:
        """완료 가능 여부"""
        return self.executed
