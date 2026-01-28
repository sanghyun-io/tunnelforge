"""GitHub Releases에서 설치 프로그램 다운로드

최신 릴리스를 확인하고 설치 프로그램을 다운로드합니다.
"""

import os
import tempfile
import requests
from typing import Optional, Callable, Tuple

from .version_info import (
    RELEASES_API_URL,
    RELEASES_PAGE_URL,
    INSTALLER_FILENAME_PATTERN,
)


class DownloadError(Exception):
    """다운로드 관련 오류"""
    pass


class InstallerDownloader:
    """GitHub Releases에서 설치 프로그램 다운로드"""

    TIMEOUT = 10  # API 요청 타임아웃 (초)
    CHUNK_SIZE = 8192  # 다운로드 청크 크기 (바이트)

    def __init__(self):
        self.latest_version: Optional[str] = None
        self.download_url: Optional[str] = None
        self.file_size: int = 0
        self._cancelled = False

    def cancel(self):
        """다운로드 취소"""
        self._cancelled = True

    def get_latest_release(self) -> Tuple[str, str, int]:
        """최신 릴리스 정보 조회

        Returns:
            (버전, 다운로드 URL, 파일 크기)

        Raises:
            DownloadError: API 호출 실패 또는 설치 파일을 찾을 수 없는 경우
        """
        try:
            response = requests.get(
                RELEASES_API_URL,
                timeout=self.TIMEOUT,
                headers={'Accept': 'application/vnd.github.v3+json'}
            )
            response.raise_for_status()

            release_data = response.json()
            self.latest_version = release_data.get('tag_name', '').lstrip('v')

            if not self.latest_version:
                raise DownloadError("버전 정보를 찾을 수 없습니다")

            # assets에서 설치 프로그램 찾기
            assets = release_data.get('assets', [])
            for asset in assets:
                asset_name = asset.get('name', '')
                if INSTALLER_FILENAME_PATTERN in asset_name or asset_name == INSTALLER_FILENAME_PATTERN:
                    self.download_url = asset.get('browser_download_url')
                    self.file_size = asset.get('size', 0)
                    break

            # Setup-latest.exe를 못 찾으면 Setup-{version}.exe 시도
            if not self.download_url:
                for asset in assets:
                    asset_name = asset.get('name', '')
                    if 'TunnelForge-Setup' in asset_name and asset_name.endswith('.exe'):
                        self.download_url = asset.get('browser_download_url')
                        self.file_size = asset.get('size', 0)
                        break

            if not self.download_url:
                raise DownloadError(
                    f"설치 파일을 찾을 수 없습니다.\n"
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
            raise DownloadError("먼저 get_latest_release()를 호출해야 합니다")

        self._cancelled = False

        # 임시 파일 경로 생성
        temp_dir = tempfile.gettempdir()
        filename = os.path.basename(self.download_url) or "TunnelForge-Setup.exe"
        file_path = os.path.join(temp_dir, filename)

        try:
            response = requests.get(
                self.download_url,
                stream=True,
                timeout=30
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
