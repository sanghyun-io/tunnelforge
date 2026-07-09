"""GitHub Releases에서 설치 프로그램 다운로드

bootstrapper/downloader.py의 로직을 메인 앱용으로 추출한 모듈입니다.
"""

import os
import platform
import tempfile
import requests
from typing import Optional, Callable, Tuple, Sequence, Mapping, Any

from src.version import GITHUB_OWNER, GITHUB_REPO


# GitHub API URL
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

# 다운로드 대상 파일 패턴 (버전별 파일명 사용)
WINDOWS_INSTALLER_FILENAME_PREFIX = "TunnelForge-Setup-"
MACOS_PACKAGE_FILENAME_PREFIX = "TunnelForge-macOS-"


class DownloadError(Exception):
    """다운로드 관련 오류"""
    pass


def select_release_asset(
    assets: Sequence[Mapping[str, Any]],
    platform_name: Optional[str] = None,
    arch_name: Optional[str] = None,
) -> Optional[Tuple[str, int]]:
    """Return the download URL and size for the current platform's release asset."""
    system = platform_name or platform.system()
    arch = arch_name or platform.machine()
    candidates = []

    for asset in assets:
        asset_name = str(asset.get('name', ''))
        url = asset.get('browser_download_url')
        if not url:
            continue

        if system == "Windows":
            if (
                asset_name.startswith(WINDOWS_INSTALLER_FILENAME_PREFIX)
                and asset_name.endswith('.exe')
                and 'WebSetup' not in asset_name
            ):
                candidates.append((str(url), int(asset.get('size', 0)), 0))
        elif system == "Darwin":
            if asset_name.startswith(MACOS_PACKAGE_FILENAME_PREFIX):
                arch_rank = _macos_arch_rank(asset_name, arch)
                if arch_rank is None:
                    continue

                if asset_name.endswith('.dmg'):
                    candidates.append((str(url), int(asset.get('size', 0)), arch_rank))
                elif asset_name.endswith('.zip'):
                    candidates.append((str(url), int(asset.get('size', 0)), arch_rank + 1))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[2])
    url, size, _ = candidates[0]
    return url, size


def _macos_arch_rank(asset_name: str, arch_name: str) -> Optional[int]:
    """Return rank offset for a macOS package architecture, or None if unusable."""
    normalized_arch = _normalize_macos_arch(arch_name)
    if not normalized_arch:
        return 4

    if f"-{normalized_arch}." in asset_name:
        return 0
    if "-universal." in asset_name or "-universal2." in asset_name:
        return 2
    if "-arm64." in asset_name or "-x86_64." in asset_name:
        return None
    return 4


def _normalize_macos_arch(arch_name: str) -> str:
    arch = arch_name.lower()
    if arch in {"amd64", "x64"}:
        return "x86_64"
    if arch == "aarch64":
        return "arm64"
    return arch


class UpdateDownloader:
    """GitHub Releases에서 설치 프로그램 다운로드"""

    DEFAULT_TIMEOUT = 10  # 기본 API 요청 타임아웃 (초)
    DEFAULT_DOWNLOAD_TIMEOUT = 30  # 설치 파일 다운로드 스톨 타임아웃 하한 (초, 대용량 파일용)
    CHUNK_SIZE = 8192  # 다운로드 청크 크기 (바이트)

    def __init__(self, config_manager=None):
        self.latest_version: Optional[str] = None
        self.download_url: Optional[str] = None
        self.file_size: int = 0
        self._cancelled = False
        self._config_manager = config_manager

    @property
    def timeout(self) -> int:
        """API 요청 타임아웃 (초) - 설정 파일에서 읽거나 기본값 사용"""
        if self._config_manager is not None:
            return self._config_manager.get_network_timeout_download()
        return self.DEFAULT_TIMEOUT

    @property
    def download_timeout(self) -> int:
        """설치 파일 다운로드용 타임아웃 (초).

        `requests`의 timeout은 총 다운로드 시간이 아니라 read/connect 스톨 허용치다.
        대용량 설치 파일은 API 호출보다 넉넉한 스톨 허용치가 필요하므로,
        설정값(get_network_timeout_download)을 존중하되 최소 DEFAULT_DOWNLOAD_TIMEOUT(30초)을 보장한다.
        """
        return max(self.timeout, self.DEFAULT_DOWNLOAD_TIMEOUT)

    def cancel(self):
        """다운로드 취소"""
        self._cancelled = True

    def get_installer_info(self) -> Tuple[str, str, int]:
        """최신 릴리스 정보 조회

        Returns:
            (버전, 다운로드 URL, 파일 크기)

        Raises:
            DownloadError: API 호출 실패 또는 설치 파일을 찾을 수 없는 경우
        """
        try:
            response = requests.get(
                RELEASES_API_URL,
                timeout=self.timeout,
                headers={'Accept': 'application/vnd.github.v3+json'}
            )
            response.raise_for_status()

            release_data = response.json()
            self.latest_version = release_data.get('tag_name', '').lstrip('v')

            if not self.latest_version:
                raise DownloadError("버전 정보를 찾을 수 없습니다")

            assets = release_data.get('assets', [])
            selected = select_release_asset(assets)
            if selected:
                self.download_url, self.file_size = selected

            if not self.download_url:
                raise DownloadError(
                    f"현재 플랫폼용 설치 파일을 찾을 수 없습니다.\n"
                    f"GitHub에서 직접 다운로드: {RELEASES_PAGE_URL}"
                )

            return self.latest_version, self.download_url, self.file_size

        except requests.exceptions.Timeout:
            raise DownloadError(
                "서버 응답 시간이 초과되었습니다.\n"
                "인터넷 연결을 확인하고 다시 시도해 주세요."
            )
        except requests.exceptions.ConnectionError:
            raise DownloadError(
                "인터넷에 연결할 수 없습니다.\n"
                "네트워크 연결을 확인하고 다시 시도해 주세요."
            )
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise DownloadError(
                    "릴리스를 찾을 수 없습니다.\n"
                    f"GitHub에서 확인: {RELEASES_PAGE_URL}"
                )
            raise DownloadError(f"서버 오류: {e.response.status_code}")
        except requests.exceptions.RequestException as e:
            raise DownloadError(f"네트워크 오류: {str(e)}")

    def download_installer(
        self,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> str:
        """설치 프로그램 다운로드

        Args:
            progress_callback: 진행률 콜백 함수 (downloaded_bytes, total_bytes)

        Returns:
            다운로드된 파일의 경로

        Raises:
            DownloadError: 다운로드 실패 시
        """
        if not self.download_url:
            raise DownloadError("먼저 get_installer_info()를 호출해야 합니다")

        self._cancelled = False

        # 임시 파일 경로 생성
        temp_dir = tempfile.gettempdir()
        filename = os.path.basename(self.download_url) or "TunnelForge-Update"
        file_path = os.path.join(temp_dir, filename)

        try:
            response = requests.get(
                self.download_url,
                stream=True,
                timeout=self.download_timeout
            )
            response.raise_for_status()

            # Content-Length가 없는 경우 대비
            total_size = int(response.headers.get('content-length', self.file_size))

            downloaded = 0
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    if self._cancelled:
                        f.close()
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        raise DownloadError("다운로드가 취소되었습니다")

                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                        if progress_callback and total_size > 0:
                            progress_callback(downloaded, total_size)

            return file_path

        except requests.exceptions.RequestException as e:
            if os.path.exists(file_path):
                os.remove(file_path)
            raise DownloadError(f"다운로드 실패: {str(e)}")
        except IOError as e:
            raise DownloadError(f"파일 저장 실패: {str(e)}")


def format_size(size_bytes: int) -> str:
    """바이트 크기를 읽기 쉬운 형식으로 변환"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"
