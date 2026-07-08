"""
스키마 동기화 스크립트 미리보기/저장 다이얼로그
"""
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QTextEdit, QVBoxLayout
)
from PyQt6.QtGui import QFont


class SyncScriptDialog(QDialog):
    """동기화 스크립트 다이얼로그"""

    def __init__(self, parent=None, script: str = ""):
        super().__init__(parent)
        self.script = script
        self._setup_ui()

    def _setup_ui(self):
        """UI 구성"""
        self.setWindowTitle("동기화 스크립트")
        self.setMinimumSize(700, 500)

        layout = QVBoxLayout(self)

        # 경고
        warning = QLabel(
            "⚠️ 주의: 이 스크립트를 실행하기 전에 반드시 타겟 데이터베이스를 백업하세요!"
        )
        warning.setStyleSheet(
            "background-color: #fff3cd; color: #856404; "
            "padding: 10px; border-radius: 4px; font-weight: bold;"
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)

        # 데이터 미복사 경고
        data_warning = QLabel(
            "📋 이 스크립트는 테이블 구조(DDL)만 동기화합니다.\n"
            "데이터는 복사되지 않습니다. 데이터 이전은 Export/Import 기능을 사용하세요."
        )
        data_warning.setStyleSheet(
            "background-color: #d1ecf1; color: #0c5460; "
            "padding: 10px; border-radius: 4px; font-weight: bold;"
        )
        data_warning.setWordWrap(True)
        layout.addWidget(data_warning)

        # 스크립트
        self.script_text = QTextEdit()
        self.script_text.setPlainText(self.script)
        self.script_text.setFont(QFont("Consolas", 10))
        self.script_text.setReadOnly(True)
        layout.addWidget(self.script_text)

        # 버튼
        btn_layout = QHBoxLayout()

        copy_btn = QPushButton("클립보드에 복사")
        copy_btn.clicked.connect(self._copy_to_clipboard)
        btn_layout.addWidget(copy_btn)

        save_btn = QPushButton("파일로 저장")
        save_btn.clicked.connect(self._save_to_file)
        btn_layout.addWidget(save_btn)

        btn_layout.addStretch()

        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

    def _copy_to_clipboard(self):
        """클립보드에 복사"""
        clipboard = QApplication.clipboard()
        clipboard.setText(self.script)
        QMessageBox.information(self, "복사 완료", "스크립트가 클립보드에 복사되었습니다.")

    def _save_to_file(self):
        """파일로 저장"""
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "스크립트 저장",
            "sync_script.sql",
            "SQL Files (*.sql);;All Files (*)"
        )

        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.script)
                QMessageBox.information(
                    self, "저장 완료",
                    f"스크립트가 저장되었습니다:\n{file_path}"
                )
            except Exception as e:
                QMessageBox.critical(self, "저장 실패", f"파일 저장 실패: {e}")
