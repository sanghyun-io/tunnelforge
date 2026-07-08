"""
앱 종료 확인 다이얼로그
"""
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QHBoxLayout, QLabel,
    QPushButton, QRadioButton, QVBoxLayout
)


class CloseConfirmDialog(QDialog):
    """종료 시 선택 다이얼로그"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("프로그램 종료")
        self.setFixedSize(350, 180)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 안내 메시지
        label = QLabel("프로그램을 어떻게 처리하시겠습니까?")
        label.setStyleSheet("font-size: 13px; margin-bottom: 10px;")
        layout.addWidget(label)

        # 라디오 버튼 그룹
        self.btn_group = QButtonGroup(self)

        self.radio_minimize = QRadioButton("시스템 트레이로 최소화 (백그라운드 실행)")
        self.radio_minimize.setChecked(True)
        self.btn_group.addButton(self.radio_minimize)
        layout.addWidget(self.radio_minimize)

        self.radio_exit = QRadioButton("프로그램 완전 종료")
        self.btn_group.addButton(self.radio_exit)
        layout.addWidget(self.radio_exit)

        # 기억 체크박스
        self.chk_remember = QCheckBox("이 선택을 기억하고 다시 묻지 않기")
        self.chk_remember.setStyleSheet("margin-top: 10px;")
        layout.addWidget(self.chk_remember)

        # 버튼
        button_layout = QHBoxLayout()
        btn_ok = QPushButton("확인")
        btn_ok.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_ok.clicked.connect(self.accept)

        btn_cancel = QPushButton("취소")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_cancel.clicked.connect(self.reject)

        button_layout.addStretch()
        button_layout.addWidget(btn_ok)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)

    def get_result(self):
        """선택된 동작과 기억 여부 반환"""
        action = 'minimize' if self.radio_minimize.isChecked() else 'exit'
        remember = self.chk_remember.isChecked()
        return action, remember
