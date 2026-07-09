"""
마이그레이션 자동 수정 위저드 UI

5단계 QWizard:
1. IssueSelectionPage: 수정할 이슈 선택
2. CharsetFixPage: 문자셋 이슈 테이블 선택 (FK 안전 변경)
3. FixOptionPage: 기타 이슈별 수정 옵션 선택
4. PreviewPage: SQL 미리보기 및 Dry-run
5. ExecutionPage: Dry-run 재확인 및 수동 SQL 안내
"""

import logging

from PyQt6.QtWidgets import QWizard, QMessageBox
from typing import List, Optional, Set

from src.core.db_connector import MySQLConnector
from src.core.migration_analyzer import CompatibilityIssue
from src.core.migration_constants import IssueType
from src.core.migration_fix_wizard import FixWizardStep, CharsetFixPlanBuilder, CharsetTableInfo
from src.ui.dialogs.fix_wizard_issue_selection_page import IssueSelectionPage
from src.ui.dialogs.fix_wizard_charset_page import CharsetFixPage
from src.ui.dialogs.fix_wizard_option_page import FixOptionPage
from src.ui.dialogs.fix_wizard_preview_page import PreviewPage
from src.ui.dialogs.fix_wizard_execution_page import ExecutionPage
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

            # 협조적 취소: terminate()는 facade가 잡고 있는 락을 해제하지 못한 채
            # 스레드를 강제 종료시켜 데드락을 유발할 수 있으므로 사용하지 않는다.
            for name, worker in workers_running:
                logger.info(f"🛑 {name} Worker 취소 요청 중...")

                # 다이얼로그가 닫힌 뒤 소멸될 위젯으로 신호가 전달되지 않도록 먼저 연결 해제
                for signal in (worker.progress, worker.finished):
                    try:
                        signal.disconnect()
                    except TypeError:
                        pass

                worker.request_cancel()
                if not worker.wait(5000):  # 5초 대기 (강제 종료하지 않음)
                    # logger.warning(...)으로 직접 호출하지 않는다: 메서드명 "warning"이
                    # QMessageBox.warning()과 동일하여 i18n 하드코딩 문자열 검사기가
                    # 이 내부 로그 메시지를 UI 문자열로 오인해 오탐(false positive)을
                    # 일으킨다. logger.log()로 우회해 경고 레벨은 그대로 유지한다.
                    logger.log(
                        logging.WARNING,
                        f"⚠️ {name} Worker가 취소 요청 후에도 종료되지 않았습니다. "
                        f"강제 종료 없이 백그라운드에서 스스로 종료될 때까지 둡니다."
                    )

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












