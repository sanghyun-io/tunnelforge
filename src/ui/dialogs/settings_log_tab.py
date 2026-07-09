"""Settings dialog log viewer tab."""
import os

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.logger import (
    clear_log_file,
    filter_log_by_level,
    get_log_dir,
    get_log_file_path,
    read_log_file,
)
from src.ui.styles import ButtonStyles


class LogViewerTab(QWidget):
    """Log file viewer tab for the settings dialog."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        control_layout = QHBoxLayout()

        filter_label = QLabel("로그 레벨:")
        filter_label.setStyleSheet("font-size: 12px;")
        control_layout.addWidget(filter_label)

        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["ALL", "DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_level_combo.setCurrentText("ALL")
        self.log_level_combo.currentTextChanged.connect(self._on_log_filter_changed)
        self.log_level_combo.setStyleSheet("font-size: 12px; padding: 4px; min-width: 100px;")
        control_layout.addWidget(self.log_level_combo)

        control_layout.addStretch()

        btn_refresh_log = QPushButton("새로고침")
        btn_refresh_log.setStyleSheet(ButtonStyles.INFO_SMALL)
        btn_refresh_log.clicked.connect(self._refresh_log_viewer)
        control_layout.addWidget(btn_refresh_log)

        btn_open_log_folder = QPushButton("로그 폴더 열기")
        btn_open_log_folder.setStyleSheet(ButtonStyles.MUTED_SMALL)
        btn_open_log_folder.clicked.connect(self._open_log_folder)
        control_layout.addWidget(btn_open_log_folder)

        btn_clear_log = QPushButton("로그 초기화")
        btn_clear_log.setStyleSheet(ButtonStyles.DANGER_SMALL)
        btn_clear_log.clicked.connect(self._clear_log_file)
        control_layout.addWidget(btn_clear_log)

        layout.addLayout(control_layout)

        log_path_label = QLabel(f"로그 파일: {get_log_file_path()}")
        log_path_label.setStyleSheet("font-size: 10px; color: #666; margin: 5px 0;")
        log_path_label.setWordWrap(True)
        layout.addWidget(log_path_label)

        self.log_viewer = QTextEdit()
        self.log_viewer.setReadOnly(True)
        self.log_viewer.setFont(QFont("Consolas", 9))
        self.log_viewer.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 8px;
            }
        """)
        self.log_viewer.setPlaceholderText("로그 파일이 없습니다.")
        layout.addWidget(self.log_viewer)

        self._refresh_log_viewer()

    def _refresh_log_viewer(self):
        content = read_log_file(max_lines=500)
        level = self.log_level_combo.currentText()
        filtered_content = filter_log_by_level(content, level)
        self.log_viewer.setPlainText(filtered_content)
        scrollbar = self.log_viewer.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_log_filter_changed(self, level: str):
        self._refresh_log_viewer()

    def _open_log_folder(self):
        log_dir = get_log_dir()
        if os.path.exists(log_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(log_dir))
        else:
            QMessageBox.information(self, "알림", "로그 폴더가 아직 생성되지 않았습니다.")

    def _clear_log_file(self):
        reply = QMessageBox.question(
            self, "로그 초기화",
            "로그 파일을 초기화하시겠습니까?\n이 작업은 되돌릴 수 없습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            success, msg = clear_log_file()
            if success:
                self._refresh_log_viewer()
                QMessageBox.information(self, "알림", msg)
            else:
                QMessageBox.warning(self, "오류", msg)
