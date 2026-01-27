"""GitHub Releases API를 통한 업데이트 확인

GitHub 저장소의 최신 릴리스를 확인하고 현재 버전과 비교합니다.
"""

import requests
from packaging import version
from typing import Tuple, Optional
from src.version import __version__, GITHUB_OWNER, GITHUB_REPO


class UpdateChecker:
    """GitHub Releases를 통한 업데이트 확인"""

    RELEASES_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    TIMEOUT = 5  # 네트워크 요청 타임아웃 (초)

    def __init__(self):
        self.current_version = __version__

    def check_update(self) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """최신 버전 확인

        Returns:
            (업데이트 필요 여부, 최신 버전, 다운로드 URL, 에러 메시지)

        Example:
            >>> checker = UpdateChecker()
            >>> needs_update, latest, url, error = checker.check_update()
            >>> if needs_update:
            ...     print(f"새 버전 {latest} 사용 가능: {url}")
        """
        try:
            # GitHub API 호출
            response = requests.get(self.RELEASES_URL, timeout=self.TIMEOUT)
            response.raise_for_status()

            release_data = response.json()
            latest_version = release_data.get('tag_name', '').lstrip('v')
            download_url = release_data.get('html_url', '')

            if not latest_version:
                return False, None, None, "버전 정보를 찾을 수 없습니다"

            # 버전 비교
            current = version.parse(self.current_version)
            latest = version.parse(latest_version)

            needs_update = latest > current

            return needs_update, latest_version, download_url, None

        except requests.exceptions.RequestException as e:
            # 네트워크 오류는 앱 실행에 영향을 주지 않도록 조용히 처리
            error_msg = f"네트워크 오류: {str(e)}"
            return False, None, None, error_msg

        except Exception as e:
            # 기타 오류
            error_msg = f"업데이트 확인 실패: {str(e)}"
            return False, None, None, error_msg

    def get_current_version(self) -> str:
        """현재 버전 반환"""
        return self.current_version

    def get_latest_release_info(self) -> Optional[dict]:
        """최신 릴리스 정보 전체 반환 (상세 정보 필요 시)

        Returns:
            릴리스 정보 딕셔너리 또는 None (실패 시)
        """
        try:
            response = requests.get(self.RELEASES_URL, timeout=self.TIMEOUT)
            response.raise_for_status()
            return response.json()
        except Exception:
            return None
