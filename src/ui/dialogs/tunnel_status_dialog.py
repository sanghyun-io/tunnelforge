"""
터널 상태 상세 다이얼로그
- 현재 상태 정보
- 연결 지속 시간
- 최근 이벤트 히스토리
- 자동 재연결 설정
"""
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QCheckBox, QSpinBox,
    QWidget
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from src.core.tunnel_monitor import TunnelMonitor, TunnelState, TunnelStatus
from src.core.logger import get_logger

logger = get_logger(__name__)


class TunnelStatusDialog(QDialog):
    """터널 상태 상세 다이얼로그"""

    def __init__(self, parent=None, tunnel_monitor: TunnelMonitor = None,
                 tunnel_id: str = None, tunnel_name: str = ""):
        """
        Args:
            parent: 부모 위젯
            tunnel_monitor: TunnelMonitor 인스턴스
            tunnel_id: 터널 ID
            tunnel_name: 터널 이름 (표시용)
        """
        super().__init__(parent)
        self.monitor = tunnel_monitor
        self.tunnel_id = tunnel_id
        self.tunnel_name = tunnel_name

        self._setup_ui()
        self._start_refresh_timer()
        self._refresh_status()

    def _setup_ui(self):
        """UI 구성"""
        self.setWindowTitle(f"터널 상태: {self.tunnel_name}")
        self.setMinimumSize(500, 450)

        layout = QVBoxLayout(self)

        # 현재 상태
        status_group = QGroupBox("현재 상태")
        status_layout = QFormLayout(status_group)

        self.state_label = QLabel()
        self.state_label.setFont(QFont("", 12, QFont.Weight.Bold))
        status_layout.addRow("상태:", self.state_label)

        self.duration_label = QLabel("-")
        status_layout.addRow("연결 시간:", self.duration_label)

        self.latency_label = QLabel("-")
        status_layout.addRow("현재 Latency:", self.latency_label)

        self.avg_latency_label = QLabel("-")
        status_layout.addRow("평균 Latency:", self.avg_latency_label)

        self.reconnect_label = QLabel("0")
        status_layout.addRow("재연결 횟수:", self.reconnect_label)

        self.last_check_label = QLabel("-")
        status_layout.addRow("마지막 확인:", self.last_check_label)

        layout.addWidget(status_group)

        # 최근 이벤트
        event_group = QGroupBox("최근 이벤트")
        event_layout = QVBoxLayout(event_group)

        self.event_table = QTableWidget()
        self.event_table.setColumnCount(3)
        self.event_table.setHorizontalHeaderLabels(["시간", "이벤트", "메시지"])
        self.event_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.event_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.event_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.event_table.setMaximumHeight(200)
        event_layout.addWidget(self.event_table)

        layout.addWidget(event_group)

        # 자동 재연결 설정
        reconnect_group = QGroupBox("자동 재연결 설정")
        reconnect_layout = QVBoxLayout(reconnect_group)

        self.auto_reconnect_check = QCheckBox("자동 재연결 활성화")
        self.auto_reconnect_check.setChecked(
            self.monitor.is_auto_reconnect_enabled() if self.monitor else True
        )
        self.auto_reconnect_check.toggled.connect(self._on_auto_reconnect_changed)
        reconnect_layout.addWidget(self.auto_reconnect_check)

        max_attempts_layout = QHBoxLayout()
        max_attempts_layout.addWidget(QLabel("최대 재연결 시도 횟수:"))
        self.max_attempts_spin = QSpinBox()
        self.max_attempts_spin.setRange(1, 20)
        self.max_attempts_spin.setValue(
            self.monitor.get_max_reconnect_attempts() if self.monitor else 5
        )
        self.max_attempts_spin.valueChanged.connect(self._on_max_attempts_changed)
        max_attempts_layout.addWidget(self.max_attempts_spin)
        max_attempts_layout.addStretch()
        reconnect_layout.addLayout(max_attempts_layout)

        layout.addWidget(reconnect_group)

        # 버튼
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.refresh_btn = QPushButton("새로고침")
        self.refresh_btn.clicked.connect(self._refresh_status)
        btn_layout.addWidget(self.refresh_btn)

        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

    def _start_refresh_timer(self):
        """자동 갱신 타이머 시작"""
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_status)
        self._refresh_timer.start(1000)  # 1초마다 갱신

    def _refresh_status(self):
        """상태 갱신"""
        if not self.monitor or not self.tunnel_id:
            return

        status = self.monitor.get_status(self.tunnel_id)

        # 상태 표시
        state_text, state_color = self._get_state_display(status.state)
        self.state_label.setText(state_text)
        self.state_label.setStyleSheet(f"color: {state_color};")

        # 연결 시간
        self.duration_label.setText(status.format_duration())

        # Latency
        if status.latency_ms is not None and status.latency_ms >= 0:
            self.latency_label.setText(f"{status.latency_ms:.1f} ms")
        else:
            self.latency_label.setText("-")

        # 평균 Latency
        avg_latency = status.get_average_latency()
        if avg_latency is not None:
            self.avg_latency_label.setText(f"{avg_latency:.1f} ms (최근 10회)")
        else:
            self.avg_latency_label.setText("-")

        # 재연결 횟수
        self.reconnect_label.setText(str(status.reconnect_count))

        # 마지막 확인
        if status.last_check:
            self.last_check_label.setText(
                status.last_check.strftime('%H:%M:%S')
            )
        else:
            self.last_check_label.setText("-")

        # 이벤트 갱신
        self._refresh_events()

    def _get_state_display(self, state: TunnelState) -> tuple:
        """상태 표시 텍스트와 색상"""
        displays = {
            TunnelState.DISCONNECTED: ("⚪ 연결 안됨", "#888888"),
            TunnelState.CONNECTING: ("🔵 연결 중...", "#3498db"),
            TunnelState.CONNECTED: ("🟢 연결됨", "#27ae60"),
            TunnelState.RECONNECTING: ("🟡 재연결 중...", "#f39c12"),
            TunnelState.ERROR: ("🔴 오류", "#e74c3c"),
        }
        return displays.get(state, ("알 수 없음", "#888888"))

    def _refresh_events(self):
        """이벤트 목록 갱신"""
        if not self.monitor or not self.tunnel_id:
            return

        events = self.monitor.get_recent_events(self.tunnel_id, limit=20)

        self.event_table.setRowCount(0)

        for event in events:
            row = self.event_table.rowCount()
            self.event_table.insertRow(row)

            # 시간
            time_item = QTableWidgetItem(
                event.timestamp.strftime('%Y-%m-%d %H:%M:%S')
            )
            self.event_table.setItem(row, 0, time_item)

            # 이벤트 타입
            type_item = QTableWidgetItem(event.event_type)
            # 이벤트 타입별 색상
            type_colors = {
                "connected": "#27ae60",
                "disconnected": "#888888",
                "reconnected": "#3498db",
                "reconnecting": "#f39c12",
                "error": "#e74c3c"
            }
            # color는 향후 행 배경색에 사용 예정
            _color = type_colors.get(event.event_type, "#000000")  # noqa: F841
            type_item.setForeground(Qt.GlobalColor.darkGreen if event.event_type in ["connected", "reconnected"] else Qt.GlobalColor.black)
            self.event_table.setItem(row, 1, type_item)

            # 메시지
            msg_item = QTableWidgetItem(event.message)
            self.event_table.setItem(row, 2, msg_item)

    def _on_auto_reconnect_changed(self, checked: bool):
        """자동 재연결 설정 변경"""
        if self.monitor:
            self.monitor.set_auto_reconnect(checked)

    def _on_max_attempts_changed(self, value: int):
        """최대 재연결 횟수 변경"""
        if self.monitor:
            self.monitor.set_max_reconnect_attempts(value)

    def closeEvent(self, event):
        """다이얼로그 닫힐 때"""
        if hasattr(self, '_refresh_timer'):
            self._refresh_timer.stop()
        super().closeEvent(event)


class StatusSummaryWidget(QWidget):
    """상태 요약 위젯 (메인 윈도우 임베드용)"""

    def __init__(self, parent=None, tunnel_monitor: TunnelMonitor = None):
        super().__init__(parent)
        self.monitor = tunnel_monitor
        self._setup_ui()

    def _setup_ui(self):
        """UI 구성"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 2, 5, 2)

        self.connected_label = QLabel("연결됨: 0")
        layout.addWidget(self.connected_label)

        layout.addWidget(QLabel("|"))

        self.disconnected_label = QLabel("연결 안됨: 0")
        layout.addWidget(self.disconnected_label)

        layout.addWidget(QLabel("|"))

        self.error_label = QLabel("오류: 0")
        self.error_label.setStyleSheet("color: red;")
        layout.addWidget(self.error_label)

        layout.addStretch()

    def update_summary(self):
        """요약 업데이트"""
        if not self.monitor:
            return

        statuses = self.monitor.get_all_statuses()

        connected = sum(1 for s in statuses.values()
                       if s.state == TunnelState.CONNECTED)
        disconnected = sum(1 for s in statuses.values()
                          if s.state == TunnelState.DISCONNECTED)
        error = sum(1 for s in statuses.values()
                   if s.state in [TunnelState.ERROR, TunnelState.RECONNECTING])

        self.connected_label.setText(f"연결됨: {connected}")
        self.disconnected_label.setText(f"연결 안됨: {disconnected}")
        self.error_label.setText(f"오류: {error}")
