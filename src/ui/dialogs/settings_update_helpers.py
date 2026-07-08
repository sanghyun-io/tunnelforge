"""
설정 다이얼로그 - 업데이트 확인/패키지 실행 관련 헬퍼
"""
from dataclasses import dataclass
from typing import Optional
from PyQt6.QtCore import QThread, pyqtSignal

from src.core.platform_integration import update_package_launch_strategy


@dataclass(frozen=True)
class UpdatePackageActionText:
    button: str
    done_message: str
    confirm_title: str
    confirm_question: str
    confirm_body: str


def update_package_action_text(strategy: Optional[str] = None) -> UpdatePackageActionText:
    launch_strategy = strategy or update_package_launch_strategy()
    if launch_strategy == "open":
        return UpdatePackageActionText(
            button="📂 패키지 열기",
            done_message="✅ 다운로드 완료! '패키지 열기' 버튼을 클릭하세요.",
            confirm_title="패키지 열기 확인",
            confirm_question="다운로드한 TunnelForge 패키지를 여시겠습니까?",
            confirm_body="다운로드한 패키지를 열면 현재 앱이 종료됩니다.",
        )

    return UpdatePackageActionText(
        button="🚀 설치 시작",
        done_message="✅ 다운로드 완료! '설치 시작' 버튼을 클릭하세요.",
        confirm_title="설치 확인",
        confirm_question="TunnelForge 설치를 시작하시겠습니까?",
        confirm_body="설치를 위해 현재 앱이 종료됩니다.",
    )


class UpdateCheckerThread(QThread):
    """업데이트 확인 백그라운드 스레드"""
    update_checked = pyqtSignal(bool, str, str, str)  # needs_update, latest_version, download_url, error_msg

    def __init__(self, config_manager=None):
        super().__init__()
        self._config_manager = config_manager

    def run(self):
        try:
            from src.core.update_checker import UpdateChecker
            checker = UpdateChecker(config_manager=self._config_manager)
            needs_update, latest_version, download_url, error_msg = checker.check_update()
            self.update_checked.emit(needs_update, latest_version or "", download_url or "", error_msg or "")
        except Exception as e:
            self.update_checked.emit(False, "", "", f"업데이트 확인 실패: {str(e)}")
