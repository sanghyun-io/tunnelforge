"""
연결 테스트 진행 다이얼로그
"""
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QTextEdit, QProgressBar
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

from src.ui.workers.connection_test_worker import CANCELLED_MESSAGE


class TestProgressDialog(QDialog):
    """연결 테스트 진행 다이얼로그"""

    # 취소 버튼/ESC로 사용자가 취소를 요청했을 때 발화 (attach_cancel로 worker에 연결)
    cancel_requested = pyqtSignal()

    def __init__(self, parent, title: str = "연결 테스트"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(480, 320)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)
        self._cancelling = False
        self._cancelled = False  # worker가 실제로 취소 메시지로 끝났는지
        self._done = False
        self._elapsed = 0

        layout = QVBoxLayout(self)

        # 상태 메시지
        self.status_label = QLabel("테스트 준비 중...")
        self.status_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self.status_label)

        # 경과 시간 (응답 없는 호스트에서도 진행 중임을 알 수 있도록)
        self.elapsed_label = QLabel(f"경과 {self._elapsed}초")
        self.elapsed_label.setStyleSheet("color: #7f8c8d;")
        layout.addWidget(self.elapsed_label)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._elapsed_timer.start()

        # 진행 표시
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # Indeterminate
        layout.addWidget(self.progress)

        # 상세 로그
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(180)
        layout.addWidget(self.log_text, 1)

        # 취소 버튼 (attach_cancel()로 worker가 연결된 경우에만 표시)
        self.btn_cancel = QPushButton("취소")
        self.btn_cancel.hide()
        self.btn_cancel.clicked.connect(self.request_cancel)
        layout.addWidget(self.btn_cancel)

        # 결과 버튼 (초기 숨김)
        self.btn_close = QPushButton("닫기")
        self.btn_close.hide()
        self.btn_close.clicked.connect(self.accept)
        layout.addWidget(self.btn_close)

    def attach_cancel(self, cancel):
        """취소 요청을 worker의 cancel 콜백에 연결하고 취소 버튼을 보인다."""
        self.cancel_requested.connect(cancel)
        self.btn_cancel.show()

    def _tick_elapsed(self):
        self._elapsed += 1
        self.elapsed_label.setText(f"경과 {self._elapsed}초")

    def request_cancel(self):
        """협조적 취소 요청. 다이얼로그는 worker 스레드가 끝난 뒤(worker_stopped)에 닫힌다."""
        if self._cancelling or self._done:
            return
        self._cancelling = True
        self.btn_cancel.setEnabled(False)
        self.status_label.setText("취소 중…")
        self.cancel_requested.emit()

    def worker_stopped(self):
        """worker 스레드(QThread.finished)가 끝난 뒤 호출. 실제로 취소된 테스트면 다이얼로그를 닫는다.

        취소가 늦어 테스트가 이미 결과를 냈다면 그 결과를 그대로 보여주고 닫지 않는다.
        """
        self._elapsed_timer.stop()
        if self._cancelled:
            self.accept()

    def update_progress(self, msg: str):
        """진행 상태 업데이트"""
        if not self._cancelling:
            self.status_label.setText(msg)
        self.log_text.append(msg)

    def show_result(self, success: bool, msg: str):
        """결과 표시"""
        self._done = True
        self._elapsed_timer.stop()
        self.progress.hide()
        self.btn_cancel.hide()
        self._cancelled = self._cancelling and msg == CANCELLED_MESSAGE
        if self._cancelled:
            self.status_label.setText("⏹ 테스트 취소됨")
            color = "#7f8c8d"
        else:
            self.status_label.setText("✅ 테스트 완료!" if success else "❌ 테스트 실패")
            color = "#27ae60" if success else "#e74c3c"
        self.status_label.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {color};")
        self.log_text.append(f"\n{'='*40}")
        self.log_text.append(msg)
        self.btn_close.show()
