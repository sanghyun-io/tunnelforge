"""Settings group for SQL workspace recovery (P1-4): on/off, autosave interval, delete stored data."""
from PyQt6.QtWidgets import (
    QCheckBox, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from src.core.workspace_store import (
    DEFAULT_AUTOSAVE_SECONDS, MAX_AUTOSAVE_SECONDS, MIN_AUTOSAVE_SECONDS, SETTING_AUTOSAVE_SECONDS,
    SETTING_ENABLED, WorkspaceStore, autosave_seconds,
)

SCOPE_NOTE = (
    "저장되는 것: 열린 SQL 탭의 내용(미저장 초안 포함), 탭 순서와 파일 경로, 선택한 DB/스키마, 커서 위치.\n"
    "저장되지 않는 것: 비밀번호와 접속 정보, 조회 결과, 미커밋 변경, 운영 쓰기 해제 상태.\n"
    "복구 파일은 SQL 히스토리와 같은 앱 데이터 폴더에 암호화 없이 저장됩니다."
)


class WorkspaceRecoverySettingsGroup(QGroupBox):
    def __init__(self, config_manager, parent=None):
        super().__init__("SQL 작업 공간 복구", parent)
        self.config_mgr = config_manager
        layout = QVBoxLayout(self)

        self.chk_enabled = QCheckBox("앱을 다시 열 때 SQL 탭과 미저장 초안 복원")
        self.chk_enabled.setChecked(config_manager.get_app_setting(SETTING_ENABLED, True) is not False)
        layout.addWidget(self.chk_enabled)

        row = QHBoxLayout()
        row.addWidget(QLabel("자동 저장 주기(초):"))
        self.spin_seconds = QSpinBox()
        self.spin_seconds.setRange(MIN_AUTOSAVE_SECONDS, MAX_AUTOSAVE_SECONDS)
        self.spin_seconds.setValue(autosave_seconds(config_manager.get_app_setting(SETTING_AUTOSAVE_SECONDS, DEFAULT_AUTOSAVE_SECONDS)))
        row.addWidget(self.spin_seconds)
        row.addStretch()
        layout.addLayout(row)

        note = QLabel(SCOPE_NOTE)
        note.setStyleSheet("color: gray; font-size: 11px;")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.btn_delete = QPushButton("저장된 작업 공간 지우기")
        self.btn_delete.clicked.connect(self.delete_stored)
        layout.addWidget(self.btn_delete)

        self.chk_enabled.toggled.connect(self.spin_seconds.setEnabled)
        self.spin_seconds.setEnabled(self.chk_enabled.isChecked())

    def _confirm_delete(self) -> bool:
        reply = QMessageBox.question(
            self, "저장된 작업 공간 지우기",
            "저장된 모든 프로필의 SQL 복구 데이터와 손상 백업 파일을 삭제합니다. 되돌릴 수 없습니다. 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def delete_stored(self) -> int:
        if not self._confirm_delete():
            return 0
        removed = WorkspaceStore().delete_all()
        QMessageBox.information(self, "저장된 작업 공간 지우기", f"삭제한 파일: {removed}")
        return removed

    def save(self) -> None:
        enabled = self.chk_enabled.isChecked()
        self.config_mgr.set_app_settings({
            SETTING_ENABLED: enabled,
            SETTING_AUTOSAVE_SECONDS: autosave_seconds(self.spin_seconds.value()),
        })
        # Turning it off takes effect when an editor opens; stored data stays until the user deletes it.
