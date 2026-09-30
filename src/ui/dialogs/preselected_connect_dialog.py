"""미리 선택한 터널로 마법사를 열 때의 연결 대기창 (TF-STATUS-120).

DBConnectionWorker 가 연결/정리를 백그라운드에서 수행하고, 이 모달 창은 이벤트 루프를 돌려
GUI 가 멈추지 않게 한다. 소유권 순서(native QThread.finished 이후 queued 완료 -> 연결 인수
또는 정리 -> 활성 집합에서 제거)는 DBConnectionWorker 가 그대로 보장한다.
"""
from typing import Optional

from PyQt6.QtWidgets import QDialog, QLabel, QProgressBar, QPushButton, QVBoxLayout

from src.ui.workers.db_connection_worker import (
    DBConnectionWorker, TunnelStartWorker, is_tunnel_start_in_flight,
)


class PreselectedConnectDialog(QDialog):
    def __init__(self, parent, connector):
        super().__init__(parent)
        self.setWindowTitle("DB 연결")
        self.connector = None
        self.failure: Optional[str] = None  # 실패 메시지 (취소면 None)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("DB에 연결하는 중… 취소하면 결과를 사용하지 않습니다."))
        bar = QProgressBar()
        bar.setRange(0, 0)  # 진행률을 알 수 없으므로 busy 표시
        layout.addWidget(bar)
        self.btn_cancel = QPushButton("취소")
        self.btn_cancel.clicked.connect(self.reject)
        layout.addWidget(self.btn_cancel)

        self._worker = DBConnectionWorker(connector, "connect")
        self._worker.connection_finished.connect(self._connection_finished)
        # 창이 파괴돼도 워커는 계속 실행될 수 있으므로 결과를 채택하지 않도록 취소 표시
        self.destroyed.connect(self._worker.cancel)

    def start(self):
        self._worker.start()

    def _connection_finished(self, worker):
        if worker is not self._worker:
            return
        self._worker = None
        if worker.error or not worker.success:
            self.failure = worker.message
            super().reject()
        else:
            self.connector = worker.take_connector()
            self.accept()

    def take_connector(self):
        connector, self.connector = self.connector, None
        return connector

    def reject(self):
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        super().reject()


class TunnelStartDialog(QDialog):
    """터널 시작 대기창: 이벤트 루프를 돌려 GUI 를 유지하고, 취소를 지원한다."""

    def __init__(self, parent, engine, config, check_port=None):
        super().__init__(parent)
        self.setWindowTitle("터널 연결")
        self.result_pair = None  # (success, message); 취소면 None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"'{config.get('name', '')}' 터널을 연결하는 중… 취소하면 결과를 사용하지 않습니다."))
        bar = QProgressBar()
        bar.setRange(0, 0)
        layout.addWidget(bar)
        self.btn_cancel = QPushButton("취소")
        self.btn_cancel.clicked.connect(self.reject)
        layout.addWidget(self.btn_cancel)

        self._worker = TunnelStartWorker(engine, config, check_port)
        self._worker.tunnel_finished.connect(self._tunnel_finished)
        self.destroyed.connect(self._worker.cancel)

    def start(self):
        self._worker.start()

    def _tunnel_finished(self, worker):
        if worker is not self._worker:
            return
        self._worker = None
        self.result_pair = (worker.success, worker.message)
        self.accept()

    def reject(self):
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        super().reject()


def start_tunnel_with_progress(parent, engine, config, check_port=None):
    """터널을 시작하고 (success, message) 를 반환한다. 사용자가 취소하면 None.

    호출자 시그니처는 동기지만 대기 중 이벤트 루프가 돌아 GUI 가 멈추지 않는다.
    """
    if is_tunnel_start_in_flight(config.get('id')):
        return False, "이전 연결 시도를 정리하는 중입니다. 잠시 후 다시 시도하세요."
    dialog = TunnelStartDialog(parent, engine, config, check_port)
    dialog.start()
    dialog.exec()
    return dialog.result_pair
