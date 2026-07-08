"""
마이그레이션 수정 위저드 4단계: SQL 미리보기 및 Dry-run 페이지
"""
from PyQt6.QtWidgets import (
    QGroupBox, QHBoxLayout, QProgressBar, QPushButton, QTextEdit,
    QVBoxLayout, QWizardPage
)
from typing import Optional

from src.core.migration_fix_wizard import FKSafeCharsetChanger, FixStrategy
from src.ui.workers.fix_wizard_worker import FixWizardWorker


class PreviewPage(QWizardPage):
    """4단계: SQL 미리보기 및 Dry-run

    1. 문자셋 변경 SQL (FK 안전 변경)
    2. 기타 이슈 수정 SQL
    """

    def __init__(self, wizard: "FixWizardDialog"):
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
        )

        if steps:
            lines.append("-- ===== Part 2: 기타 이슈 수정 =====")
            lines.append(f"-- 대상 이슈: {other_execute_count}개")
            lines.append("")

            # 이미 출력한 SQL 추적 (FK 일괄 변경 중복 방지)
            processed_sql_hashes: set = set()

            for step in steps:
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
