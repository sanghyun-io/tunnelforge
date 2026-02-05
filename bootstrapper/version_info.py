"""부트스트래퍼 버전 정보

이 파일은 부트스트래퍼의 버전과 GitHub 저장소 정보를 관리합니다.
"""

__bootstrapper_version__ = "1.0.0"
__app_name__ = "TunnelForge"

# GitHub 저장소 정보 (src/version.py와 동일)
GITHUB_OWNER = "sanghyun-io"
GITHUB_REPO = "tunnelforge"

# GitHub API URL
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

# 다운로드 대상 파일 패턴 (버전별 파일명 사용)
INSTALLER_FILENAME_PREFIX = "TunnelForge-Setup-"
