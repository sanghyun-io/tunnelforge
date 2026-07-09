"""
마이그레이션 수정 위저드 1단계: 수정할 이슈 선택 페이지
"""
from PyQt6.QtWidgets import (
    QCheckBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QWizardPage
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from typing import List

from src.core.migration_constants import (
    IssueType,
    ISSUE_TYPE_DISPLAY_NAMES,
    AUTO_FIXABLE_ISSUE_TYPES,
)
from src.core.migration_fix_wizard import CharsetFixPlanBuilder, create_wizard_steps


class IssueSelectionPage(QWizardPage):
    """1단계: 수정할 이슈 선택"""

    def __init__(self, wizard: "FixWizardDialog"):
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

        # 자동 수정 가능한 이슈 타입 (공유 단일 소스 참조)
        auto_fixable_types = AUTO_FIXABLE_ISSUE_TYPES

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
            type_name = ISSUE_TYPE_DISPLAY_NAMES.get(issue.issue_type, str(issue.issue_type.value))
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
        """다음 단계 진행 가능 여부

        필터로 숨겨진 행은 validatePage()에서도 선택 대상으로 취급하지 않으므로,
        여기서도 화면에 보이는(숨겨지지 않은) 체크된 행만 반영해야 한다.
        """
        return any(
            chk.isChecked() and not self.table.isRowHidden(i)
            for i, chk in enumerate(self.checkboxes)
        )

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
