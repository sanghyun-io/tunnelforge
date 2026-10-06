"""
연결 테스트 진행 다이얼로그
"""
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QTextEdit, QProgressBar
from PyQt6.QtCore import Qt


class TestProgressDialog(QDialog):
    """연결 테스트 진행 다이얼로그"""

    def __init__(self, parent, title: str = "연결 테스트"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(480, 320)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)

        # 상태 메시지
        self.status_label = QLabel("테스트 준비 중...")
        self.status_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self.status_label)

        # 진행 표시
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # Indeterminate
        layout.addWidget(self.progress)

        # 상세 로그
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(180)
        layout.addWidget(self.log_text, 1)

        # 결과 버튼 (초기 숨김)
        self.btn_close = QPushButton("닫기")
        self.btn_close.hide()
        self.btn_close.clicked.connect(self.accept)
        layout.addWidget(self.btn_close)

    def update_progress(self, msg: str):
        """진행 상태 업데이트"""
        self.status_label.setText(msg)
        self.log_text.append(msg)

    def show_result(self, success: bool, msg: str):
        """결과 표시"""
        self.progress.hide()
        self.status_label.setText("✅ 테스트 완료!" if success else "❌ 테스트 실패")
        self.status_label.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {'#27ae60' if success else '#e74c3c'};"
        )
        self.log_text.append(f"\n{'='*40}")
        self.log_text.append(msg)
        self.btn_close.show()
