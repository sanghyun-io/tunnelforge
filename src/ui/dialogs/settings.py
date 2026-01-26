"""설정 관련 다이얼로그"""
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QRadioButton, QCheckBox,
                             QButtonGroup, QGroupBox)


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


class SettingsDialog(QDialog):
    """앱 설정 다이얼로그"""
    def __init__(self, parent=None, config_manager=None):
        super().__init__(parent)
        self.config_mgr = config_manager
        self.setWindowTitle("설정")
        self.setFixedSize(400, 220)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 종료 동작 설정 그룹
        group_box = QGroupBox("창 닫기(X) 버튼 동작")
        group_layout = QVBoxLayout(group_box)

        self.btn_group = QButtonGroup(self)

        self.radio_ask = QRadioButton("매번 묻기")
        self.btn_group.addButton(self.radio_ask)
        group_layout.addWidget(self.radio_ask)

        self.radio_minimize = QRadioButton("항상 시스템 트레이로 최소화")
        self.btn_group.addButton(self.radio_minimize)
        group_layout.addWidget(self.radio_minimize)

        self.radio_exit = QRadioButton("항상 프로그램 종료")
        self.btn_group.addButton(self.radio_exit)
        group_layout.addWidget(self.radio_exit)

        layout.addWidget(group_box)

        # 현재 설정 로드
        current_action = self.config_mgr.get_app_setting('close_action', 'ask')
        if current_action == 'minimize':
            self.radio_minimize.setChecked(True)
        elif current_action == 'exit':
            self.radio_exit.setChecked(True)
        else:  # 'ask' or default
            self.radio_ask.setChecked(True)

        layout.addStretch()

        # 버튼
        button_layout = QHBoxLayout()
        btn_save = QPushButton("저장")
        btn_save.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_save.clicked.connect(self.save_settings)

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
        button_layout.addWidget(btn_save)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)

    def save_settings(self):
        """설정 저장"""
        if self.radio_minimize.isChecked():
            action = 'minimize'
        elif self.radio_exit.isChecked():
            action = 'exit'
        else:
            action = 'ask'

        self.config_mgr.set_app_setting('close_action', action)
        self.accept()
