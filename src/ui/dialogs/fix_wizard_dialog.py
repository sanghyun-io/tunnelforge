"""
마이그레이션 자동 수정 위저드 UI

5단계 QWizard:
1. IssueSelectionPage: 수정할 이슈 선택
2. CharsetFixPage: 문자셋 이슈 테이블 선택 (FK 안전 변경)
3. FixOptionPage: 기타 이슈별 수정 옵션 선택
4. PreviewPage: SQL 미리보기 및 Dry-run
5. ExecutionPage: Dry-run 재확인 및 수동 SQL 안내
"""

import os
from datetime import datetime

from PyQt6.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QRadioButton, QCheckBox,
    QButtonGroup, QGroupBox, QTextEdit, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
    QWidget, QFrame, QSplitter, QMessageBox, QApplication,
    QTreeWidget, QTreeWidgetItem, QDialog, QDialogButtonBox,
    QComboBox, QSpacerItem, QSizePolicy, QFileDialog
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QIcon
from typing import List, Optional, Dict, Set

from src.core.db_connector import MySQLConnector
from src.core.migration_analyzer import CompatibilityIssue
from src.core.migration_constants import IssueType
from src.core.migration_fix_wizard import (
    FixStrategy, FixOption, FixWizardStep,
    SmartFixGenerator, BatchFixExecutor, create_wizard_steps,
    CollationFKGraphBuilder, CharsetFixPlanBuilder, CharsetTableInfo,
    FKSafeCharsetChanger
)
from src.core.platform_paths import rollback_dir
from src.ui.workers.fix_wizard_worker import FixWizardWorker


class FixWizardDialog(QWizard):
    """마이그레이션 자동 수정 위저드

    5단계 QWizard:
    1. IssueSelectionPage: 수정할 이슈 선택
    2. CharsetFixPage: 문자셋 이슈 테이블 선택 (FK 안전 변경)
    3. FixOptionPage: 이슈별 수정 옵션 선택 (문자셋 제외)
    4. PreviewPage: SQL 미리보기 및 Dry-run
    5. ExecutionPage: Dry-run 재확인 및 수동 SQL 안내
    """

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
        self._is_closing = False

        # 문자셋 이슈 분리
        self.charset_issues: List[CompatibilityIssue] = []
        self.other_issues: List[CompatibilityIssue] = []

        # 문자셋 수정 계획
        self.charset_plan_builder: Optional[CharsetFixPlanBuilder] = None
        self.charset_tables_to_fix: Set[str] = set()  # 실제 수정할 테이블

        self.init_ui()

    def closeEvent(self, event):
        """위저드 닫기 이벤트 - Worker 정리"""
        self._is_closing = True

        # 실행 중인 Worker 확인
        workers_running = []

        # PreviewPage의 worker
        if hasattr(self, 'preview_page') and self.preview_page.worker:
            if self.preview_page.worker.isRunning():
                workers_running.append(("미리보기", self.preview_page.worker))

        # ExecutionPage의 worker
        if hasattr(self, 'execution_page') and self.execution_page.worker:
            if self.execution_page.worker.isRunning():
                workers_running.append(("실행", self.execution_page.worker))

        if workers_running:
            from src.core.logger import get_logger
            logger = get_logger('fix_wizard_dialog')

            # Worker 종료 대기
            for name, worker in workers_running:
                logger.info(f"🛑 {name} Worker 종료 대기 중...")
                worker.quit()
                if not worker.wait(3000):  # 3초 대기
                    logger.warning(f"⚠️ {name} Worker가 시간 내에 종료되지 않음, 강제 종료")
                    worker.terminate()
                    worker.wait(1000)

        event.accept()

    def init_ui(self):
        self.setWindowTitle("🔧 마이그레이션 자동 수정 위저드")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.resize(900, 700)

        # 페이지 추가 (ID 저장)
        self.issue_page = IssueSelectionPage(self)
        self.charset_fix_page = CharsetFixPage(self)
        self.option_page = FixOptionPage(self)
        self.preview_page = PreviewPage(self)
        self.execution_page = ExecutionPage(self)

        self.issue_page_id = self.addPage(self.issue_page)
        self.charset_fix_page_id = self.addPage(self.charset_fix_page)
        self.option_page_id = self.addPage(self.option_page)
        self.preview_page_id = self.addPage(self.preview_page)
        self.execution_page_id = self.addPage(self.execution_page)

        # 버튼 텍스트 변경
        self.setButtonText(QWizard.WizardButton.NextButton, "다음 >")
        self.setButtonText(QWizard.WizardButton.BackButton, "< 이전")
        self.setButtonText(QWizard.WizardButton.FinishButton, "완료")
        self.setButtonText(QWizard.WizardButton.CancelButton, "취소")

    def has_charset_issues(self) -> bool:
        """문자셋 이슈가 있는지 확인"""
        return len(self.charset_issues) > 0

    def has_other_issues(self) -> bool:
        """문자셋 외 다른 이슈가 있는지 확인"""
        return len(self.other_issues) > 0


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

        filter_layout.addWidget(self.chk_error)
        filter_layout.addWidget(self.chk_warning)
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

        for i, issue in enumerate(self.wizard_dialog.issues):
            visible = True

            # 심각도 필터
            if issue.severity == "error" and not show_error:
                visible = False
            elif issue.severity == "warning" and not show_warning:
                visible = False

            self.table.setRowHidden(i, not visible)

        self.update_count()

    def select_all(self):
        """전체 선택 (대량 항목 최적화)"""
        # UI 업데이트 일시 중지
        self.table.setUpdatesEnabled(False)
        try:
            for i, chk in enumerate(self.checkboxes):
                if not self.table.isRowHidden(i):
                    # 시그널 차단하여 update_count() 반복 호출 방지
                    chk.blockSignals(True)
                    chk.setChecked(True)
                    chk.blockSignals(False)
        finally:
            self.table.setUpdatesEnabled(True)
        # 완료 후 한 번만 업데이트
        self.update_count()

    def deselect_all(self):
        """전체 해제 (대량 항목 최적화)"""
        # UI 업데이트 일시 중지
        self.table.setUpdatesEnabled(False)
        try:
            for chk in self.checkboxes:
                chk.blockSignals(True)
                chk.setChecked(False)
                chk.blockSignals(False)
        finally:
            self.table.setUpdatesEnabled(True)
        # 완료 후 한 번만 업데이트
        self.update_count()

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

        # 문자셋 이슈와 다른 이슈 분리
        charset_issues = []
        other_issues = []

        for issue in selected:
            if issue.issue_type == IssueType.CHARSET_ISSUE:
                charset_issues.append(issue)
            else:
                other_issues.append(issue)

        self.wizard_dialog.charset_issues = charset_issues
        self.wizard_dialog.other_issues = other_issues

        # 문자셋 수정 계획 빌더 초기화
        if charset_issues:
            # 원본 이슈 테이블 집합 추출
            original_tables = set()
            for issue in charset_issues:
                parts = issue.location.split('.')
                if len(parts) >= 2:
                    original_tables.add(parts[1])  # schema.table → table

            self.wizard_dialog.charset_plan_builder = CharsetFixPlanBuilder(
                self.wizard_dialog.connector,
                self.wizard_dialog.schema,
                original_tables
            )
        else:
            self.wizard_dialog.charset_plan_builder = None

        # 다른 이슈에 대한 위저드 단계 생성 (문자셋 제외)
        self.wizard_dialog.wizard_steps = create_wizard_steps(
            other_issues,
            self.wizard_dialog.connector,
            self.wizard_dialog.schema
        )

        return True


class CharsetFixPage(QWizardPage):
    """2단계: 문자셋 변경 대상 테이블 선택

    FK 안전 변경 방식으로 일괄 처리합니다.
    - 모든 테이블이 기본 선택됨
    - 체크 해제 시 = 건너뛰기
    - 건너뛰기 시 FK 연쇄 영향 확인 다이얼로그 표시
    """

    def __init__(self, wizard: FixWizardDialog):
        super().__init__(wizard)
        self.wizard_dialog = wizard

        self.setTitle("문자셋 변경 대상 테이블")
        self.setSubTitle("FK 안전 변경 방식으로 일괄 처리됩니다. (FK DROP → charset 변경 → FK 재생성)")

        self.table_checkboxes: Dict[str, QCheckBox] = {}
        self.table_infos: List[CharsetTableInfo] = []
        self._updating_checkboxes = False  # 연쇄 업데이트 중 플래그

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 안내 텍스트
        info_frame = QFrame()
        info_frame.setStyleSheet("""
            QFrame {
                background-color: #e8f4fd;
                border: 1px solid #90caf9;
                border-radius: 4px;
                padding: 10px;
            }
        """)
        info_layout = QVBoxLayout(info_frame)

        info_label = QLabel(
            "ℹ️ <b>FK 안전 변경 방식</b>으로 모든 테이블이 일괄 처리됩니다.<br>"
            "체크 해제 시 해당 테이블을 건너뜁니다.<br>"
            "FK 관계로 인해 연쇄적으로 건너뛰어야 하는 테이블이 있을 수 있습니다."
        )
        info_label.setWordWrap(True)
        info_layout.addWidget(info_label)

        layout.addWidget(info_frame)

        # 테이블 목록 영역
        self.table_group = QGroupBox("대상 테이블")
        table_layout = QVBoxLayout(self.table_group)

        # 스크롤 영역
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setMinimumHeight(300)

        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        scroll_area.setWidget(self.scroll_content)
        table_layout.addWidget(scroll_area)

        layout.addWidget(self.table_group)

        # 통계 라벨
        stats_layout = QHBoxLayout()
        self.lbl_stats = QLabel("선택됨: 0개 | 건너뛰기: 0개 | 총 FK: 0개")
        self.lbl_stats.setStyleSheet("font-weight: bold; color: #333;")
        stats_layout.addWidget(self.lbl_stats)
        stats_layout.addStretch()
        layout.addLayout(stats_layout)

        # 버튼
        btn_layout = QHBoxLayout()

        btn_select_all = QPushButton("전체 선택")
        btn_select_all.clicked.connect(self.select_all)

        btn_deselect_all = QPushButton("전체 해제")
        btn_deselect_all.clicked.connect(self.deselect_all)

        btn_layout.addWidget(btn_select_all)
        btn_layout.addWidget(btn_deselect_all)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)

    def initializePage(self):
        """페이지 초기화"""
        # 문자셋 이슈가 없으면 이 페이지 건너뛰기
        if not self.wizard_dialog.has_charset_issues():
            return

        # 기존 체크박스 제거
        for i in reversed(range(self.scroll_layout.count())):
            widget = self.scroll_layout.itemAt(i).widget()
            if widget:
                widget.deleteLater()
        self.table_checkboxes.clear()

        # 테이블 목록 빌드
        plan_builder = self.wizard_dialog.charset_plan_builder
        if not plan_builder:
            return

        self.table_infos = plan_builder.build_full_table_list()

        # 테이블별 체크박스 생성
        for info in self.table_infos:
            widget = self._create_table_widget(info)
            self.scroll_layout.addWidget(widget)

        self.update_stats()

    def _create_table_widget(self, info: CharsetTableInfo) -> QWidget:
        """테이블 위젯 생성"""
        widget = QFrame()
        widget.setStyleSheet("""
            QFrame {
                background-color: #fafafa;
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                padding: 8px;
                margin: 2px;
            }
            QFrame:hover {
                background-color: #f0f0f0;
            }
        """)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)

        # 첫 번째 줄: 체크박스 + 테이블명 + 태그
        header_layout = QHBoxLayout()

        chk = QCheckBox()
        chk.setChecked(not info.skip)
        chk.stateChanged.connect(lambda state, t=info.table_name: self.on_table_check_changed(t, state))
        self.table_checkboxes[info.table_name] = chk
        header_layout.addWidget(chk)

        # 테이블명
        lbl_name = QLabel(f"<b>{info.table_name}</b>")
        header_layout.addWidget(lbl_name)

        # 태그: 원본 이슈 / FK 연관
        if info.is_original_issue:
            tag = QLabel("원본 이슈")
            tag.setStyleSheet("""
                QLabel {
                    background-color: #e74c3c;
                    color: white;
                    padding: 2px 6px;
                    border-radius: 3px;
                    font-size: 10px;
                }
            """)
        else:
            tag = QLabel("FK 연관")
            tag.setStyleSheet("""
                QLabel {
                    background-color: #3498db;
                    color: white;
                    padding: 2px 6px;
                    border-radius: 3px;
                    font-size: 10px;
                }
            """)
        header_layout.addWidget(tag)
        header_layout.addStretch()

        # 현재 charset
        lbl_charset = QLabel(f"{info.current_charset} / {info.current_collation}")
        lbl_charset.setStyleSheet("color: #666; font-size: 11px;")
        header_layout.addWidget(lbl_charset)

        layout.addLayout(header_layout)

        # 두 번째 줄: FK 관계
        if info.fk_parents or info.fk_children:
            fk_layout = QHBoxLayout()
            fk_layout.setContentsMargins(24, 0, 0, 0)

            fk_parts = []
            if info.fk_parents:
                fk_parts.append(f"부모: {', '.join(info.fk_parents)}")
            if info.fk_children:
                fk_parts.append(f"자식: {', '.join(info.fk_children)}")

            lbl_fk = QLabel("└─ FK: " + " | ".join(fk_parts))
            lbl_fk.setStyleSheet("color: #888; font-size: 10px;")
            fk_layout.addWidget(lbl_fk)
            fk_layout.addStretch()

            layout.addLayout(fk_layout)

        return widget

    def on_table_check_changed(self, table_name: str, state: int):
        """테이블 체크 상태 변경"""
        if self._updating_checkboxes:
            return

        is_checked = (state == Qt.CheckState.Checked.value)

        if not is_checked:
            # 건너뛰기 선택 → 연쇄 확인
            self._handle_skip_table(table_name)
        else:
            # 선택 복원
            self._handle_restore_table(table_name)

        self.update_stats()
        self.completeChanged.emit()

    def _handle_skip_table(self, table_name: str):
        """테이블 건너뛰기 처리"""
        plan_builder = self.wizard_dialog.charset_plan_builder
        if not plan_builder:
            return

        # 연쇄 건너뛰기 테이블 계산
        cascade_tables = plan_builder.get_cascade_skip_tables(table_name)

        if cascade_tables:
            # 확인 다이얼로그
            cascade_list = '\n'.join(f"• {t}" for t in sorted(cascade_tables))
            reply = QMessageBox.question(
                self,
                "연쇄 건너뛰기 확인",
                f"'{table_name}' 테이블을 건너뛰면\n"
                f"FK 관계로 인해 다음 테이블도 함께 건너뛰어야 합니다:\n\n"
                f"{cascade_list}\n\n"
                f"진행하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                # 연쇄 테이블도 함께 건너뛰기
                self._skip_tables({table_name} | cascade_tables)
            else:
                # 체크박스 복원
                self._restore_checkbox(table_name)
        else:
            # 연쇄 영향 없음 → 바로 건너뛰기
            self._skip_tables({table_name})

    def _handle_restore_table(self, table_name: str):
        """테이블 복원 처리 (건너뛰기 해제)"""
        # table_infos에서 해당 테이블 찾아서 skip 해제
        for info in self.table_infos:
            if info.table_name == table_name:
                info.skip = False
                break

    def _skip_tables(self, tables: Set[str]):
        """테이블들을 건너뛰기 처리"""
        self._updating_checkboxes = True
        try:
            for table in tables:
                # table_infos 업데이트
                for info in self.table_infos:
                    if info.table_name == table:
                        info.skip = True
                        break

                # 체크박스 업데이트
                if table in self.table_checkboxes:
                    self.table_checkboxes[table].setChecked(False)
        finally:
            self._updating_checkboxes = False

    def _restore_checkbox(self, table_name: str):
        """체크박스 복원 (건너뛰기 취소)"""
        self._updating_checkboxes = True
        try:
            if table_name in self.table_checkboxes:
                self.table_checkboxes[table_name].setChecked(True)
        finally:
            self._updating_checkboxes = False

    def select_all(self):
        """전체 선택"""
        self._updating_checkboxes = True
        try:
            for info in self.table_infos:
                info.skip = False
            for chk in self.table_checkboxes.values():
                chk.setChecked(True)
        finally:
            self._updating_checkboxes = False
        self.update_stats()
        self.completeChanged.emit()

    def deselect_all(self):
        """전체 해제"""
        self._updating_checkboxes = True
        try:
            for info in self.table_infos:
                info.skip = True
            for chk in self.table_checkboxes.values():
                chk.setChecked(False)
        finally:
            self._updating_checkboxes = False
        self.update_stats()
        self.completeChanged.emit()

    def update_stats(self):
        """통계 업데이트"""
        total = len(self.table_infos)
        selected = sum(1 for info in self.table_infos if not info.skip)
        skipped = total - selected

        # FK 개수 계산
        fk_count = 0
        if self.wizard_dialog.charset_plan_builder:
            tables_to_fix = {info.table_name for info in self.table_infos if not info.skip}
            if tables_to_fix:
                changer = FKSafeCharsetChanger(
                    self.wizard_dialog.connector,
                    self.wizard_dialog.schema
                )
                fks = changer.get_related_fks(tables_to_fix)
                fk_count = len(fks)

        self.lbl_stats.setText(f"선택됨: {selected}개 | 건너뛰기: {skipped}개 | 총 FK: {fk_count}개")

    def isComplete(self) -> bool:
        """다음 단계 진행 가능 여부"""
        # 문자셋 이슈가 없으면 무조건 통과
        if not self.wizard_dialog.has_charset_issues():
            return True

        # 최소 1개 테이블 선택 필요
        return any(not info.skip for info in self.table_infos)

    def nextId(self) -> int:
        """다음 페이지 결정

        다른 이슈가 없으면 FixOptionPage 건너뛰기
        """
        # 문자셋 이슈가 없으면 다음 페이지(FixOptionPage)로
        if not self.wizard_dialog.has_charset_issues():
            # 다른 이슈도 없으면 PreviewPage로
            if not self.wizard_dialog.has_other_issues():
                return self.wizard_dialog.preview_page_id
            return self.wizard_dialog.option_page_id

        # 다른 이슈가 없으면 PreviewPage로
        if not self.wizard_dialog.has_other_issues():
            return self.wizard_dialog.preview_page_id

        # 기본: 다음 페이지 (FixOptionPage)
        return self.wizard_dialog.option_page_id

    def validatePage(self) -> bool:
        """페이지 유효성 검사 및 데이터 저장"""
        # 선택된 테이블 저장
        tables_to_fix = {info.table_name for info in self.table_infos if not info.skip}
        self.wizard_dialog.charset_tables_to_fix = tables_to_fix

        return True


class BatchOptionDialog(QDialog):
    """전체 일괄 옵션 적용 다이얼로그

    이슈 유형별로 기본 옵션을 선택하여 모든 이슈에 일괄 적용합니다.

    주의사항:
    - 공통 옵션(strategy)만 표시 (모든 이슈에 있는 옵션)
    - 적용 시 각 step의 실제 옵션에서 matching strategy를 찾아 적용
    - 예: nullable이 아닌 컬럼에는 "NULL로 변경"이 없으므로 fallback
    """

    def __init__(self, steps: List[FixWizardStep], parent=None):
        super().__init__(parent)
        self.steps = steps
        self.option_combos: Dict[IssueType, QComboBox] = {}
        self.type_warnings: Dict[IssueType, str] = {}  # 유형별 경고 메시지

        self.setWindowTitle("전체 일괄 옵션 적용")
        self.setMinimumWidth(500)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 안내 텍스트
        info_label = QLabel(
            "이슈 유형별로 기본 옵션을 선택하세요.\n"
            "선택한 옵션이 해당 유형의 모든 이슈에 적용됩니다."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #666; margin-bottom: 10px;")
        layout.addWidget(info_label)

        # 이슈 유형별 그룹
        type_counts: Dict[IssueType, int] = {}
        type_steps: Dict[IssueType, List[FixWizardStep]] = {}

        for step in self.steps:
            if step.issue_type not in type_counts:
                type_counts[step.issue_type] = 0
                type_steps[step.issue_type] = []
            type_counts[step.issue_type] += 1
            type_steps[step.issue_type].append(step)

        type_names = {
            IssueType.INVALID_DATE: "잘못된 날짜",
            IssueType.CHARSET_ISSUE: "문자셋 이슈",
            IssueType.ZEROFILL_USAGE: "ZEROFILL 속성",
            IssueType.FLOAT_PRECISION: "FLOAT 정밀도",
            IssueType.INT_DISPLAY_WIDTH: "INT 표시 너비",
            IssueType.DEPRECATED_ENGINE: "deprecated 엔진",
            IssueType.ENUM_EMPTY_VALUE: "ENUM 빈 값",
            IssueType.AUTH_PLUGIN_ISSUE: "인증 플러그인",
        }

        for issue_type, count in type_counts.items():
            type_name = type_names.get(issue_type, str(issue_type.value))
            group = QGroupBox(f"{type_name} ({count}개)")
            group_layout = QVBoxLayout(group)

            # 공통 옵션 추출 (모든 step에 있는 strategy만)
            common_options = self._get_common_options(type_steps[issue_type])

            combo = QComboBox()
            recommended_index = 0  # 기본값

            for i, option in enumerate(common_options):
                label = option.label
                if option.is_recommended:
                    label = f"⭐ {label} (권장)"
                    recommended_index = i  # 권장 옵션 인덱스 저장
                combo.addItem(label, option)

            # 권장 옵션을 기본 선택 (특히 FK 일괄 변경)
            combo.setCurrentIndex(recommended_index)

            group_layout.addWidget(combo)
            self.option_combos[issue_type] = combo

            # 경고 메시지 (일부 이슈에만 있는 옵션이 있는 경우)
            warning = self._get_warning_message(issue_type, type_steps[issue_type], common_options)
            if warning:
                self.type_warnings[issue_type] = warning
                warning_label = QLabel(warning)
                warning_label.setWordWrap(True)
                warning_label.setStyleSheet("color: #e67e22; font-size: 11px; margin-top: 4px;")
                group_layout.addWidget(warning_label)

            layout.addWidget(group)

        layout.addStretch()

        # 버튼
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Apply
        )
        btn_box.button(QDialogButtonBox.StandardButton.Apply).setText("적용")
        btn_box.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        btn_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply_options)

        layout.addWidget(btn_box)

    def _get_common_options(self, steps: List[FixWizardStep]) -> List[FixOption]:
        """모든 step에 공통으로 있는 옵션 추출"""
        if not steps:
            return []

        # 첫 번째 step의 strategy 집합
        common_strategies = {opt.strategy for opt in steps[0].options}

        # 다른 step들과 교집합
        for step in steps[1:]:
            step_strategies = {opt.strategy for opt in step.options}
            common_strategies &= step_strategies

        # 첫 번째 step의 옵션 중 공통 strategy만 반환 (순서 유지)
        return [opt for opt in steps[0].options if opt.strategy in common_strategies]

    def _get_warning_message(
        self,
        issue_type: IssueType,
        steps: List[FixWizardStep],
        common_options: List[FixOption]
    ) -> str:
        """경고 메시지 생성"""
        if issue_type == IssueType.INVALID_DATE:
            # NULL 옵션이 공통에 없으면 일부 컬럼이 NOT NULL
            has_null_option = any(
                opt.strategy == FixStrategy.DATE_TO_NULL
                for opt in common_options
            )
            if not has_null_option:
                null_count = sum(
                    1 for step in steps
                    if any(opt.strategy == FixStrategy.DATE_TO_NULL for opt in step.options)
                )
                not_null_count = len(steps) - null_count
                if null_count > 0:
                    return f"⚠️ {not_null_count}개 컬럼은 NOT NULL이므로 'NULL로 변경'을 사용할 수 없습니다."

        return ""

    def apply_options(self):
        """선택된 옵션 적용

        각 step의 실제 옵션에서 matching strategy를 찾아 적용합니다.
        matching이 없으면 첫 번째 옵션으로 fallback합니다.
        """
        for step in self.steps:
            if step.issue_type not in self.option_combos:
                continue

            combo = self.option_combos[step.issue_type]
            selected_option = combo.currentData()

            if not selected_option:
                continue

            # 해당 step의 옵션에서 같은 strategy를 찾아서 적용
            matching_option = next(
                (opt for opt in step.options if opt.strategy == selected_option.strategy),
                None
            )

            if matching_option:
                step.selected_option = matching_option
            else:
                # Fallback: 첫 번째 옵션 (보통 권장 옵션)
                step.selected_option = step.options[0] if step.options else None

        self.accept()


class IncludedTablesDialog(QDialog):
    """자동 포함된 테이블 목록 다이얼로그

    FK 연관테이블 일괄 변경으로 인해 자동 포함된 테이블 목록을 보여줍니다.
    (옵션 선택 단계만 건너뛰고, 실제 SQL 실행에는 포함됨)
    """

    def __init__(self, steps: List[FixWizardStep], parent=None):
        super().__init__(parent)
        self.steps = steps

        self.setWindowTitle("자동 포함된 테이블 목록")
        self.setMinimumSize(550, 400)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 자동 포함된 테이블 필터
        included_steps = [s for s in self.steps if s.included_by is not None]

        # 안내 텍스트
        info_label = QLabel(
            f"다음 {len(included_steps)}개 테이블은 FK 연관테이블 일괄 변경에 자동 포함되었습니다.\n"
            "(옵션 선택 단계만 건너뛰고, 실제 SQL 실행에는 모두 포함됩니다)"
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("margin-bottom: 10px;")
        layout.addWidget(info_label)

        # 테이블
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["테이블명", "포함 원인"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setRowCount(len(included_steps))
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        for i, step in enumerate(included_steps):
            table_name = step.location.split('.')[-1]
            table.setItem(i, 0, QTableWidgetItem(table_name))
            table.setItem(i, 1, QTableWidgetItem(f"'{step.included_by}'의 FK 일괄 변경에 포함"))

        layout.addWidget(table)

        # 닫기 버튼
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.accept)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)


class FixOptionPage(QWizardPage):
    """3단계: 이슈별 수정 옵션 선택 (문자셋 제외)

    개선 사항:
    - 전체 일괄 옵션 적용
    - FK 연관 테이블 Tree 시각화
    - FK 연관테이블 일괄 변경 시 자동 포함 (옵션 선택만 건너뜀)
    - 자동 포함된 테이블 건너뛰기 네비게이션

    참고: 문자셋 이슈는 CharsetFixPage에서 처리됨
    """

    def __init__(self, wizard: FixWizardDialog):
        super().__init__(wizard)
        self.wizard_dialog = wizard

        self.setTitle("수정 옵션 선택")
        self.setSubTitle("각 이슈에 대한 수정 방법을 선택하세요. (문자셋 이슈는 이전 단계에서 처리됨)")

        self.current_index = 0
        self.option_buttons: List[QRadioButton] = []
        self.option_labels: List[QLabel] = []
        self.input_field: Optional[QLineEdit] = None
        self._fk_graph_builder: Optional[CollationFKGraphBuilder] = None

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # === 상단 영역: 진행 표시 + 일괄 적용 버튼 ===
        progress_group = QGroupBox()
        progress_group.setStyleSheet("QGroupBox { border: 1px solid #ddd; border-radius: 4px; padding: 8px; }")
        progress_layout = QVBoxLayout(progress_group)

        # 진행률 텍스트 + 프로그레스바
        progress_text_layout = QHBoxLayout()
        self.lbl_progress = QLabel("이슈 1 / 1")
        self.lbl_progress.setStyleSheet("font-weight: bold; font-size: 13px;")
        progress_text_layout.addWidget(self.lbl_progress)
        progress_text_layout.addStretch()
        progress_layout.addLayout(progress_text_layout)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(8)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                background-color: #e0e0e0;
                border-radius: 4px;
            }
            QProgressBar::chunk {
                background-color: #3498db;
                border-radius: 4px;
            }
        """)
        progress_layout.addWidget(self.progress_bar)

        # 일괄 적용 버튼 영역
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 8, 0, 0)

        self.btn_batch_apply = QPushButton("📋 전체 일괄 적용")
        self.btn_batch_apply.setToolTip("모든 이슈에 동일한 옵션을 일괄 적용합니다")
        self.btn_batch_apply.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 6px 12px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        self.btn_batch_apply.clicked.connect(self.show_batch_option_dialog)

        self.btn_show_included = QPushButton("👁️ 자동 포함된 테이블 (0개)")
        self.btn_show_included.setToolTip("FK 일괄 변경에 자동 포함된 테이블 목록")
        self.btn_show_included.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #219a52; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """)
        self.btn_show_included.clicked.connect(self.show_included_tables_dialog)

        btn_layout.addWidget(self.btn_batch_apply)
        btn_layout.addWidget(self.btn_show_included)
        btn_layout.addStretch()
        progress_layout.addLayout(btn_layout)

        layout.addWidget(progress_group)

        # === 중앙 영역: 이슈 정보 + FK Tree ===
        self.grp_issue = QGroupBox("현재 이슈")
        issue_main_layout = QVBoxLayout(self.grp_issue)

        # 이슈 기본 정보
        issue_info_layout = QFormLayout()
        self.lbl_type = QLabel()
        self.lbl_location = QLabel()
        self.lbl_location.setStyleSheet("font-weight: bold;")
        self.lbl_description = QLabel()
        self.lbl_description.setWordWrap(True)

        issue_info_layout.addRow("유형:", self.lbl_type)
        issue_info_layout.addRow("위치:", self.lbl_location)
        issue_info_layout.addRow("설명:", self.lbl_description)
        issue_main_layout.addLayout(issue_info_layout)

        # FK 연관 테이블 Tree (접을 수 있음)
        self.fk_tree_group = QGroupBox("▼ FK 연관 테이블")
        self.fk_tree_group.setCheckable(False)
        fk_tree_layout = QVBoxLayout(self.fk_tree_group)

        self.fk_tree = QTreeWidget()
        self.fk_tree.setHeaderHidden(True)
        self.fk_tree.setMaximumHeight(150)
        self.fk_tree.setStyleSheet("""
            QTreeWidget {
                border: 1px solid #ddd;
                border-radius: 4px;
                background-color: #fafafa;
            }
            QTreeWidget::item {
                padding: 2px;
            }
            QTreeWidget::item:selected {
                background-color: #e3f2fd;
                color: black;
            }
        """)
        fk_tree_layout.addWidget(self.fk_tree)
        self.fk_tree_group.setVisible(False)

        issue_main_layout.addWidget(self.fk_tree_group)
        layout.addWidget(self.grp_issue)

        # === 하단 영역: 옵션 선택 ===
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

        # === 네비게이션 ===
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
        """페이지 초기화

        참고: 문자셋 이슈는 CharsetFixPage에서 이미 처리됨.
              wizard_steps에는 문자셋 제외 이슈만 포함됨.
        """
        self.current_index = 0
        self._fk_graph_builder = None

        # 다른 이슈가 없으면 이 페이지 건너뛰기 (show_current_issue에서 빈 상태 처리)
        if not self.wizard_dialog.wizard_steps:
            return

        # 첫 번째 미포함(옵션 선택 필요) 이슈로 이동
        self._move_to_first_not_included()
        self.show_current_issue()

    def _move_to_first_not_included(self):
        """첫 번째 옵션 선택 필요 이슈로 이동 (자동 포함된 테이블 제외)"""
        steps = self.wizard_dialog.wizard_steps
        for i, step in enumerate(steps):
            if step.included_by is None:
                self.current_index = i
                return
        self.current_index = 0

    def update_progress_display(self):
        """진행률 업데이트"""
        steps = self.wizard_dialog.wizard_steps
        total = len(steps)
        included = sum(1 for s in steps if s.included_by is not None)
        active_total = total - included

        # 현재 위치 (자동 포함된 테이블 제외 인덱스)
        active_index = sum(
            1 for i, s in enumerate(steps)
            if i <= self.current_index and s.included_by is None
        )

        if active_total > 0:
            self.lbl_progress.setText(
                f"이슈 {active_index} / {active_total} "
                f"(전체 {total}개 중 {included}개 자동 포함)"
            )
            self.progress_bar.setValue(int(active_index / active_total * 100))
        else:
            self.lbl_progress.setText(f"이슈 0 / 0 (전체 {total}개 모두 일괄 처리)")
            self.progress_bar.setValue(100)

        # 자동 포함된 테이블 버튼 업데이트
        self.btn_show_included.setText(f"👁️ 자동 포함된 테이블 ({included}개)")
        self.btn_show_included.setEnabled(included > 0)

    def show_current_issue(self):
        """현재 이슈 표시"""
        steps = self.wizard_dialog.wizard_steps
        if not steps or self.current_index >= len(steps):
            return

        step = steps[self.current_index]

        # 진행 표시 업데이트
        self.update_progress_display()

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

        # FK Tree 업데이트 (Collation 이슈일 때만)
        self._update_fk_tree(step)

        # 기존 옵션 버튼 및 라벨 제거
        for btn in self.option_buttons:
            self.btn_group.removeButton(btn)
            self.options_layout.removeWidget(btn)
            btn.deleteLater()
        self.option_buttons.clear()

        for lbl in self.option_labels:
            self.options_layout.removeWidget(lbl)
            lbl.deleteLater()
        self.option_labels.clear()

        # 새 옵션 버튼 생성
        for i, option in enumerate(step.options):
            label = option.label
            if option.is_recommended:
                label = f"⭐ {label}"

            radio = QRadioButton(label)
            radio.setToolTip(option.description)

            # 이전에 선택한 옵션이 있으면 복원
            if step.selected_option and step.selected_option.strategy == option.strategy:
                radio.setChecked(True)
            elif i == 0 and not step.selected_option:
                radio.setChecked(True)

            radio.toggled.connect(lambda checked, opt=option: self.on_option_changed(checked, opt))

            self.btn_group.addButton(radio, i)
            self.options_layout.addWidget(radio)
            self.option_buttons.append(radio)

            # 설명 라벨
            desc_text = f"    {option.description}"

            # FK 일괄 변경 옵션일 경우 안내 추가
            if option.strategy == FixStrategy.COLLATION_FK_CASCADE and option.related_tables:
                desc_text += f"\n    ✅ 위 {len(option.related_tables)}개 테이블이 함께 처리됩니다"

            desc_label = QLabel(desc_text)
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet("color: #666; font-size: 11px;")
            self.options_layout.addWidget(desc_label)
            self.option_labels.append(desc_label)

        # 입력 필드 초기화
        self.update_input_field()

        # 네비게이션 버튼 상태
        self._update_nav_buttons()

    def _update_fk_tree(self, step: FixWizardStep):
        """FK 연관 테이블 Tree 업데이트"""
        self.fk_tree.clear()

        # Collation 이슈가 아니면 숨김
        if step.issue_type != IssueType.CHARSET_ISSUE or not self._fk_graph_builder:
            self.fk_tree_group.setVisible(False)
            return

        # 현재 테이블명 추출
        location_parts = step.location.split('.')
        if len(location_parts) < 2:
            self.fk_tree_group.setVisible(False)
            return

        current_table = location_parts[1]

        # 연관 테이블 가져오기
        related_tables = self._fk_graph_builder.get_related_tables(current_table)

        if not related_tables:
            self.fk_tree_group.setVisible(False)
            return

        # Tree 구성
        self.fk_tree_group.setTitle(f"▼ FK 연관 테이블 ({len(related_tables) + 1}개)")
        self.fk_tree_group.setVisible(True)

        # 루트 아이템 (현재 테이블 또는 부모 테이블)
        all_tables = related_tables | {current_table}
        ordered = self._fk_graph_builder.get_topological_order(all_tables)

        # 계층 구조로 표시
        root_item = QTreeWidgetItem(self.fk_tree)
        root_item.setText(0, f"📁 {ordered[0]}")
        root_item.setExpanded(True)

        # 나머지 테이블을 자식으로 추가
        for table in ordered[1:]:
            child_item = QTreeWidgetItem(root_item)
            if table == current_table:
                child_item.setText(0, f"📄 {table}  ← 현재")
                child_item.setForeground(0, QColor("#e74c3c"))
            else:
                child_item.setText(0, f"📄 {table}")

        self.fk_tree.expandAll()

    def _update_nav_buttons(self):
        """네비게이션 버튼 상태 업데이트"""
        steps = self.wizard_dialog.wizard_steps

        # 이전 옵션 선택 필요 이슈 존재 여부 (자동 포함 제외)
        has_prev = any(
            s.included_by is None
            for s in steps[:self.current_index]
        )

        # 다음 옵션 선택 필요 이슈 존재 여부 (자동 포함 제외)
        has_next = any(
            s.included_by is None
            for s in steps[self.current_index + 1:]
        )

        self.btn_prev_issue.setEnabled(has_prev)
        self.btn_next_issue.setEnabled(has_next)

    def on_option_changed(self, checked: bool, option: FixOption):
        """옵션 변경 시"""
        if not checked:
            return

        step = self.wizard_dialog.wizard_steps[self.current_index]
        step.selected_option = option

        # FK 일괄 변경 옵션인 경우
        if option.strategy == FixStrategy.COLLATION_FK_CASCADE:
            self._mark_related_tables_as_included(step, option)
        else:
            # 다른 옵션 선택 시 자동 포함 해제
            self._unmark_included_tables(step)

        self.update_input_field()
        self.update_progress_display()

    def _mark_related_tables_as_included(self, source_step: FixWizardStep, option: FixOption):
        """FK 연관 테이블들을 자동 포함 처리 (옵션 선택만 건너뜀, 실제 SQL에는 포함)"""
        if not option.related_tables:
            return

        source_table = source_step.location.split('.')[-1]  # schema.table → table

        for other_step in self.wizard_dialog.wizard_steps:
            other_table = other_step.location.split('.')[-1]

            # 연관 테이블인 경우 자동 포함 처리 (현재 테이블 제외)
            if other_table in option.related_tables and other_table != source_table:
                other_step.included_by = source_table
                other_step.included_reason = f"'{source_table}'의 FK 일괄 변경에 포함"
                other_step.selected_option = option  # 같은 옵션으로 설정

    def _unmark_included_tables(self, source_step: FixWizardStep):
        """이 테이블로 인해 자동 포함된 테이블들의 포함 해제"""
        source_table = source_step.location.split('.')[-1]

        for other_step in self.wizard_dialog.wizard_steps:
            if other_step.included_by == source_table:
                other_step.included_by = None
                other_step.included_reason = ""
                other_step.selected_option = None  # 다시 선택하도록

    def update_input_field(self):
        """입력 필드 표시/숨김"""
        steps = self.wizard_dialog.wizard_steps
        if not steps or self.current_index >= len(steps):
            self.input_group.setVisible(False)
            return

        step = steps[self.current_index]
        option = step.selected_option

        if option and option.requires_input:
            self.input_group.setVisible(True)
            self.input_label.setText(option.input_label or "값:")
            self.input_field.setText(step.user_input or option.input_default or "")
        else:
            self.input_group.setVisible(False)

    def save_current_selection(self):
        """현재 선택 저장"""
        steps = self.wizard_dialog.wizard_steps
        if not steps or self.current_index >= len(steps):
            return

        step = steps[self.current_index]

        # 선택된 옵션 저장
        checked_id = self.btn_group.checkedId()
        if 0 <= checked_id < len(step.options):
            step.selected_option = step.options[checked_id]

        # 입력값 저장
        if step.selected_option and step.selected_option.requires_input:
            step.user_input = self.input_field.text()

    def prev_issue(self):
        """이전 이슈 (자동 포함된 테이블 건너뛰기)"""
        self.save_current_selection()

        prev_idx = self.current_index - 1
        steps = self.wizard_dialog.wizard_steps

        while prev_idx >= 0:
            if steps[prev_idx].included_by is None:
                break
            prev_idx -= 1

        if prev_idx >= 0:
            self.current_index = prev_idx
            self.show_current_issue()

    def next_issue(self):
        """다음 이슈 (자동 포함된 테이블 건너뛰기)"""
        self.save_current_selection()

        next_idx = self.current_index + 1
        steps = self.wizard_dialog.wizard_steps

        while next_idx < len(steps):
            if steps[next_idx].included_by is None:
                break
            next_idx += 1

        if next_idx < len(steps):
            self.current_index = next_idx
            self.show_current_issue()

    def show_batch_option_dialog(self):
        """전체 일괄 적용 다이얼로그 표시"""
        dialog = BatchOptionDialog(self.wizard_dialog.wizard_steps, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # 모든 옵션이 적용되었으므로 다음 단계로 이동
            self.wizard_dialog.next()

    def show_included_tables_dialog(self):
        """자동 포함된 테이블 목록 다이얼로그 표시"""
        dialog = IncludedTablesDialog(self.wizard_dialog.wizard_steps, self)
        dialog.exec()

    def isComplete(self) -> bool:
        """다음 단계 진행 가능 여부"""
        # 다른 이슈가 없으면 무조건 통과
        if not self.wizard_dialog.wizard_steps:
            return True
        return True  # 옵션 선택은 validatePage에서 검증

    def nextId(self) -> int:
        """다음 페이지 결정"""
        # 기본: 다음 페이지 (PreviewPage)
        return self.wizard_dialog.preview_page_id

    def validatePage(self) -> bool:
        """페이지 유효성 검사"""
        # 다른 이슈가 없으면 바로 통과
        if not self.wizard_dialog.wizard_steps:
            return True

        self.save_current_selection()

        # 모든 옵션 선택 필요 이슈에 옵션이 선택되었는지 확인
        for step in self.wizard_dialog.wizard_steps:
            if step.included_by is not None:
                continue  # 자동 포함된 이슈는 검사 스킵 (이미 옵션 선택됨)

            if not step.selected_option:
                QMessageBox.warning(self, "선택 필요", f"'{step.location}'의 수정 옵션을 선택하세요.")
                return False

            if step.selected_option.requires_input and not step.user_input:
                QMessageBox.warning(self, "입력 필요", f"'{step.location}'의 추가 입력값을 입력하세요.")
                return False

        return True


class PreviewPage(QWizardPage):
    """4단계: SQL 미리보기 및 Dry-run

    1. 문자셋 변경 SQL (FK 안전 변경)
    2. 기타 이슈 수정 SQL
    """

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
        """SQL 미리보기 생성

        1. 문자셋 변경 SQL (CharsetFixPage에서 선택한 테이블)
        2. 기타 이슈 SQL (FixOptionPage에서 선택한 옵션)
        """
        lines = []
        counter = 0

        # === 헤더 ===
        lines.append("-- ==========================================")
        lines.append("-- 마이그레이션 자동 수정 SQL")
        lines.append(f"-- 스키마: {self.wizard_dialog.schema}")
        lines.append("-- ==========================================")
        lines.append("")

        # === 1. 문자셋 변경 SQL ===
        charset_tables = self.wizard_dialog.charset_tables_to_fix
        if charset_tables:
            lines.append("-- ===== Part 1: 문자셋 변경 (FK 안전 변경) =====")
            lines.append(f"-- 대상 테이블: {len(charset_tables)}개")
            lines.append(f"-- 테이블 목록: {', '.join(sorted(charset_tables))}")
            lines.append("")

            # FKSafeCharsetChanger를 사용하여 SQL 생성
            changer = FKSafeCharsetChanger(
                self.wizard_dialog.connector,
                self.wizard_dialog.schema
            )
            sql_parts = changer.generate_safe_charset_sql(
                charset_tables,
                charset="utf8mb4",
                collation="utf8mb4_unicode_ci"
            )

            for sql_line in sql_parts['full_sql']:
                lines.append(sql_line)

            lines.append("")
            counter += 1

        # === 2. 기타 이슈 SQL ===
        steps = self.wizard_dialog.wizard_steps
        other_execute_count = sum(
            1 for s in steps
            if s.selected_option and s.selected_option.strategy != FixStrategy.SKIP
            and s.included_by is None
        )

        if steps:
            lines.append("-- ===== Part 2: 기타 이슈 수정 =====")
            lines.append(f"-- 대상 이슈: {other_execute_count}개")
            lines.append("")

            # 이미 출력한 SQL 추적 (FK 일괄 변경 중복 방지)
            processed_sql_hashes: set = set()

            for step in steps:
                # 자동 포함된 테이블은 건너뛰기 (원본 테이블의 SQL에 이미 포함됨)
                if step.included_by is not None:
                    continue

                if step.selected_option and step.selected_option.strategy != FixStrategy.SKIP:
                    sql = step.selected_option.sql_template or ""
                    if step.selected_option.requires_input and step.user_input:
                        sql = sql.replace("{custom_date}", step.user_input)
                        sql = sql.replace("{precision}", step.user_input)

                    # SQL 중복 체크
                    sql_hash = hash(sql)
                    if sql_hash in processed_sql_hashes:
                        continue
                    processed_sql_hashes.add(sql_hash)

                    counter += 1
                    lines.append(f"-- [{counter}] {step.location}")
                    lines.append(f"-- 전략: {step.selected_option.label}")
                    lines.append(sql)
                    lines.append("")

        if counter == 0:
            lines.append("-- (실행할 SQL이 없습니다)")

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
            dry_run=True,
            charset_tables_to_fix=self.wizard_dialog.charset_tables_to_fix
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

            # CombinedExecutionResult 또는 BatchExecutionResult 처리
            if hasattr(result, 'charset_tables_count'):
                # CombinedExecutionResult
                if result.charset_tables_count > 0:
                    self.txt_dryrun.append(f"  - 문자셋 변경: {result.charset_tables_count}개 테이블, {result.charset_fk_count}개 FK")
                if result.other_result:
                    self.txt_dryrun.append(f"  - 기타 이슈: 성공 {result.other_result.success_count}개, 건너뛰기 {result.other_result.skip_count}개")
                self.txt_dryrun.append(f"  - 총 영향: {result.total_affected_rows:,}개")
            else:
                # BatchExecutionResult (하위 호환)
                self.txt_dryrun.append(f"  - 성공: {result.success_count}개")
                self.txt_dryrun.append(f"  - 건너뛰기: {result.skip_count}개")
                self.txt_dryrun.append(f"  - 예상 영향 행: {result.total_affected_rows:,}개")
        else:
            self.txt_dryrun.append(f"❌ Dry-run 오류: {message}")


class ExecutionPage(QWizardPage):
    """5단계: Dry-run 재확인 및 수동 SQL 안내"""

    def __init__(self, wizard: FixWizardDialog):
        super().__init__(wizard)
        self.wizard_dialog = wizard
        self.worker: Optional[FixWizardWorker] = None
        self.executed = False
        self.rollback_sql_path: Optional[str] = None  # 저장된 Rollback SQL 경로

        self.setTitle("SQL 확인")
        self.setSubTitle("Legacy 자동 수정 위저드는 DB 변경을 직접 실행하지 않고 Dry-run 결과와 SQL만 제공합니다.")

        self.setCommitPage(False)

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 경고
        warning_label = QLabel(
            "⚠️ <b>Rust Core 전환:</b> 이 Legacy 자동 수정 위저드는 DB 변경을 직접 실행하지 않습니다. "
            "아래 버튼은 Dry-run으로 SQL과 예상 영향만 확인합니다. 실제 변경은 Rust Core 소유 경로로만 진행해야 합니다."
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
        self.grp_result = QGroupBox("Dry-run 결과")
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

        # Rollback SQL 안내
        self.grp_rollback = QGroupBox("🔄 Rollback SQL")
        self.grp_rollback.setVisible(False)
        rollback_layout = QVBoxLayout(self.grp_rollback)

        self.lbl_rollback_info = QLabel()
        self.lbl_rollback_info.setWordWrap(True)
        self.lbl_rollback_info.setStyleSheet("""
            QLabel {
                background-color: #e8f4fd;
                color: #1565c0;
                padding: 10px;
                border: 1px solid #90caf9;
                border-radius: 4px;
            }
        """)
        rollback_layout.addWidget(self.lbl_rollback_info)

        rollback_btn_layout = QHBoxLayout()
        self.btn_open_rollback = QPushButton("📂 파일 열기")
        self.btn_open_rollback.clicked.connect(self.open_rollback_file)

        self.btn_copy_rollback = QPushButton("📋 SQL 복사")
        self.btn_copy_rollback.clicked.connect(self.copy_rollback_sql)

        self.btn_save_rollback_as = QPushButton("💾 다른 위치에 저장")
        self.btn_save_rollback_as.clicked.connect(self.save_rollback_as)

        rollback_btn_layout.addWidget(self.btn_open_rollback)
        rollback_btn_layout.addWidget(self.btn_copy_rollback)
        rollback_btn_layout.addWidget(self.btn_save_rollback_as)
        rollback_btn_layout.addStretch()

        rollback_layout.addLayout(rollback_btn_layout)
        layout.addWidget(self.grp_rollback)

        # Dry-run 버튼
        btn_layout = QHBoxLayout()

        self.btn_execute = QPushButton("🔍 Dry-run 확인")
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
        self.grp_rollback.setVisible(False)
        self.executed = False

        # 실행할 작업 요약
        charset_count = len(self.wizard_dialog.charset_tables_to_fix)
        steps = self.wizard_dialog.wizard_steps
        other_execute_count = sum(1 for s in steps
                                  if s.selected_option and s.selected_option.strategy != FixStrategy.SKIP)

        self.txt_log.append(f"📋 Dry-run 확인 대기 중...")
        if charset_count > 0:
            self.txt_log.append(f"  - 문자셋 변경: {charset_count}개 테이블 (FK 안전 변경)")
        if steps:
            self.txt_log.append(f"  - 기타 이슈: {other_execute_count}개")
            skip_count = len(steps) - other_execute_count
            if skip_count > 0:
                self.txt_log.append(f"  - 건너뛰기: {skip_count}개")
        self.txt_log.append("")
        self.txt_log.append("'Dry-run 확인' 버튼을 클릭하여 SQL과 예상 영향만 확인하세요.")

    def execute(self):
        """Dry-run 확인"""
        self.btn_execute.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self.txt_log.clear()
        self.txt_log.append("🔍 Dry-run 확인 시작...")

        # Legacy Auto-Fix Wizard must not own DB mutations; keep this path dry-run only.
        self.worker = FixWizardWorker(
            connector=self.wizard_dialog.connector,
            schema=self.wizard_dialog.schema,
            steps=self.wizard_dialog.wizard_steps,
            dry_run=True,
            charset_tables_to_fix=self.wizard_dialog.charset_tables_to_fix
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
            self.txt_log.append("✅ Dry-run 확인 완료!")

            # 결과 요약 표시
            self.grp_result.setVisible(True)

            # CombinedExecutionResult 또는 BatchExecutionResult 처리
            if hasattr(result, 'charset_tables_count'):
                # CombinedExecutionResult
                total_items = result.charset_tables_count
                if result.other_result:
                    total_items += result.other_result.total_steps

                self.lbl_total.setText(str(total_items))
                self.lbl_success.setText(f"{result.total_success_count}개")
                self.lbl_fail.setText(f"{result.total_fail_count}개")
                self.lbl_affected.setText(f"{result.total_affected_rows:,}개")

                fail_count = result.total_fail_count
            else:
                # BatchExecutionResult (하위 호환)
                self.lbl_total.setText(str(result.total_steps))
                self.lbl_success.setText(f"{result.success_count}개")
                self.lbl_fail.setText(f"{result.fail_count}개")
                self.lbl_affected.setText(f"{result.total_affected_rows:,}개")
                fail_count = result.fail_count

            if fail_count > 0:
                self.lbl_fail.setStyleSheet("color: #e74c3c; font-weight: bold;")

            # Rollback SQL 저장 및 표시
            rollback_sql = getattr(result, 'rollback_sql', '')
            if rollback_sql:
                self._save_and_show_rollback(rollback_sql)
        else:
            self.txt_log.append(f"❌ 실행 오류: {message}")

            # 에러 발생 시에도 롤백 SQL 표시 (복원을 위해 중요!)
            if result:
                rollback_sql = getattr(result, 'rollback_sql', '')
                if rollback_sql:
                    self.txt_log.append("")
                    self.txt_log.append("📋 롤백 SQL이 생성되었습니다. 복원에 사용하세요.")
                    self._save_and_show_rollback(rollback_sql)

        self.completeChanged.emit()

    def _get_rollback_dir(self) -> str:
        """Rollback SQL 저장 디렉토리"""
        base_dir = str(rollback_dir())
        os.makedirs(base_dir, exist_ok=True)
        return base_dir

    def _save_and_show_rollback(self, rollback_sql: str):
        """Rollback SQL 저장 및 UI 표시"""
        try:
            # 파일명 생성
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"rollback_{self.wizard_dialog.schema}_{timestamp}.sql"
            rollback_dir = self._get_rollback_dir()
            filepath = os.path.join(rollback_dir, filename)

            # 파일 저장
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(rollback_sql)

            self.rollback_sql_path = filepath
            self._rollback_sql_content = rollback_sql

            # UI 표시
            self.grp_rollback.setVisible(True)
            self.lbl_rollback_info.setText(
                f"💡 <b>Rollback SQL이 자동 저장되었습니다.</b><br><br>"
                f"문제 발생 시 아래 파일을 실행하여 변경사항을 되돌릴 수 있습니다:<br>"
                f"<code>{filepath}</code><br><br>"
                f"⚠️ DDL(ALTER TABLE)은 트랜잭션 롤백이 불가능하므로, "
                f"문제 발생 시 이 SQL을 수동으로 실행하세요."
            )

            self.txt_log.append("")
            self.txt_log.append(f"📝 Rollback SQL 저장됨: {filepath}")

        except Exception as e:
            self.txt_log.append(f"⚠️ Rollback SQL 저장 실패: {e}")
            # 저장 실패해도 메모리에는 보관
            self._rollback_sql_content = rollback_sql
            self.grp_rollback.setVisible(True)
            self.lbl_rollback_info.setText(
                f"⚠️ Rollback SQL 파일 저장에 실패했습니다: {e}<br><br>"
                f"'SQL 복사' 버튼으로 내용을 복사하여 수동으로 저장하세요."
            )
            self.btn_open_rollback.setEnabled(False)

    def open_rollback_file(self):
        """Rollback SQL 파일 열기"""
        if self.rollback_sql_path and os.path.exists(self.rollback_sql_path):
            if os.name == 'nt':
                os.startfile(self.rollback_sql_path)
            else:
                import subprocess
                subprocess.run(['xdg-open', self.rollback_sql_path])
        else:
            QMessageBox.warning(self, "파일 없음", "Rollback SQL 파일을 찾을 수 없습니다.")

    def copy_rollback_sql(self):
        """Rollback SQL 클립보드 복사"""
        if hasattr(self, '_rollback_sql_content') and self._rollback_sql_content:
            clipboard = QApplication.clipboard()
            clipboard.setText(self._rollback_sql_content)
            QMessageBox.information(self, "복사 완료", "Rollback SQL이 클립보드에 복사되었습니다.")
        else:
            QMessageBox.warning(self, "내용 없음", "복사할 Rollback SQL이 없습니다.")

    def save_rollback_as(self):
        """Rollback SQL 다른 위치에 저장"""
        if not hasattr(self, '_rollback_sql_content') or not self._rollback_sql_content:
            QMessageBox.warning(self, "내용 없음", "저장할 Rollback SQL이 없습니다.")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"rollback_{self.wizard_dialog.schema}_{timestamp}.sql"

        filepath, _ = QFileDialog.getSaveFileName(
            self,
            "Rollback SQL 저장",
            default_name,
            "SQL 파일 (*.sql);;모든 파일 (*.*)"
        )

        if filepath:
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(self._rollback_sql_content)
                QMessageBox.information(self, "저장 완료", f"Rollback SQL이 저장되었습니다:\n{filepath}")
            except Exception as e:
                QMessageBox.critical(self, "저장 실패", f"파일 저장 실패:\n{e}")

    def isComplete(self) -> bool:
        """완료 가능 여부"""
        return self.executed
