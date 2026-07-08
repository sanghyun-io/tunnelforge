"""
마이그레이션 수정 위저드 3단계: 이슈별 수정 옵션 선택 페이지
"""
from PyQt6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QRadioButton, QVBoxLayout, QWizardPage
)
from typing import Dict, List, Optional

from src.core.migration_constants import IssueType
from src.core.migration_fix_wizard import FixOption, FixStrategy, FixWizardStep


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


class FixOptionPage(QWizardPage):
    """3단계: 이슈별 수정 옵션 선택 (문자셋 제외)

    개선 사항:
    - 전체 일괄 옵션 적용
    - FK 연관 테이블 Tree 시각화
    - FK 연관테이블 일괄 변경 시 자동 포함 (옵션 선택만 건너뜀)
    - 자동 포함된 테이블 건너뛰기 네비게이션

    참고: 문자셋 이슈는 CharsetFixPage에서 처리됨
    """

    def __init__(self, wizard: "FixWizardDialog"):
        super().__init__(wizard)
        self.wizard_dialog = wizard

        self.setTitle("수정 옵션 선택")
        self.setSubTitle("각 이슈에 대한 수정 방법을 선택하세요. (문자셋 이슈는 이전 단계에서 처리됨)")

        self.current_index = 0
        self.option_buttons: List[QRadioButton] = []
        self.option_labels: List[QLabel] = []
        self.input_field: Optional[QLineEdit] = None

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

        btn_layout.addWidget(self.btn_batch_apply)
        btn_layout.addStretch()
        progress_layout.addLayout(btn_layout)

        layout.addWidget(progress_group)

        # === 중앙 영역: 이슈 정보 ===
        # 참고: 문자셋(Collation) 이슈는 CharsetFixPage에서 별도로 처리되므로
        # 이 페이지의 wizard_steps에는 절대 포함되지 않는다. 과거 이 페이지가
        # 문자셋 이슈까지 함께 다루던 시절의 FK 연관 테이블 Tree/자동 포함 UI는
        # 도달 불가능한 코드였으므로 제거했다.
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

        # 다른 이슈가 없으면 이 페이지 건너뛰기 (show_current_issue에서 빈 상태 처리)
        if not self.wizard_dialog.wizard_steps:
            return

        self.show_current_issue()

    def update_progress_display(self):
        """진행률 업데이트"""
        steps = self.wizard_dialog.wizard_steps
        total = len(steps)

        if total > 0:
            self.lbl_progress.setText(f"이슈 {self.current_index + 1} / {total}")
            self.progress_bar.setValue(int((self.current_index + 1) / total * 100))
        else:
            self.lbl_progress.setText("이슈 0 / 0")
            self.progress_bar.setValue(100)

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
            desc_label = QLabel(desc_text)
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet("color: #666; font-size: 11px;")
            self.options_layout.addWidget(desc_label)
            self.option_labels.append(desc_label)

        # 입력 필드 초기화
        self.update_input_field()

        # 네비게이션 버튼 상태
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        """네비게이션 버튼 상태 업데이트"""
        steps = self.wizard_dialog.wizard_steps
        self.btn_prev_issue.setEnabled(self.current_index > 0)
        self.btn_next_issue.setEnabled(self.current_index < len(steps) - 1)

    def on_option_changed(self, checked: bool, option: FixOption):
        """옵션 변경 시"""
        if not checked:
            return

        step = self.wizard_dialog.wizard_steps[self.current_index]
        step.selected_option = option

        self.update_input_field()
        self.update_progress_display()

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
        """이전 이슈"""
        self.save_current_selection()

        if self.current_index > 0:
            self.current_index -= 1
            self.show_current_issue()

    def next_issue(self):
        """다음 이슈"""
        self.save_current_selection()

        steps = self.wizard_dialog.wizard_steps
        if self.current_index < len(steps) - 1:
            self.current_index += 1
            self.show_current_issue()

    def show_batch_option_dialog(self):
        """전체 일괄 적용 다이얼로그 표시"""
        dialog = BatchOptionDialog(self.wizard_dialog.wizard_steps, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # 모든 옵션이 적용되었으므로 다음 단계로 이동
            self.wizard_dialog.next()

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

        # 모든 이슈에 옵션이 선택되었는지 확인
        for step in self.wizard_dialog.wizard_steps:
            if not step.selected_option:
                QMessageBox.warning(self, "선택 필요", f"'{step.location}'의 수정 옵션을 선택하세요.")
                return False

            if step.selected_option.requires_input and not step.user_input:
                QMessageBox.warning(self, "입력 필요", f"'{step.location}'의 추가 입력값을 입력하세요.")
                return False

        return True
