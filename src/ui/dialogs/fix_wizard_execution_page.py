"""
마이그레이션 수정 위저드 5단계: Dry-run 재확인 및 수동 SQL 안내 페이지
"""
import os
from datetime import datetime
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QMessageBox, QProgressBar, QPushButton, QTextEdit,
    QVBoxLayout, QWizardPage
)
from typing import Optional

from src.core.migration_fix_wizard import FixStrategy
from src.core.platform_paths import rollback_dir
from src.ui.workers.fix_wizard_worker import FixWizardWorker


class ExecutionPage(QWizardPage):
    """5단계: Dry-run 재확인 및 수동 SQL 안내"""

    def __init__(self, wizard: "FixWizardDialog"):
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
