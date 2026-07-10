"""GitHub Releases에서 설치 프로그램 다운로드

최신 릴리스를 확인하고 설치 프로그램을 다운로드합니다.
"""

import os
import tempfile
import requests
from typing import Optional, Callable, Tuple

from src.update_integrity import (
    IntegrityError,
    MAX_INSTALLER_SIZE,
    parse_content_length,
    parse_sha256_digest,
    verify_file_integrity,
)

from .version_info import (
    RELEASES_API_URL,
    RELEASES_PAGE_URL,
    INSTALLER_FILENAME_PREFIX,
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
        self.expected_sha256: Optional[str] = None
        self.installer_filename: Optional[str] = None
        self._cancelled = False

    def cancel(self):
        """다운로드 취소"""
        self._cancelled = True

    def reset_cancellation(self):
        """새 다운로드 작업을 시작하기 전에 취소 상태를 초기화한다."""
        self._cancelled = False

    def get_latest_release(self) -> Tuple[str, str, int]:
        """최신 릴리스 정보 조회

        Returns:
            (버전, 다운로드 URL, 파일 크기)

        Raises:
            DownloadError: API 호출 실패 또는 설치 파일을 찾을 수 없는 경우
        """
        self.latest_version = None
        self.download_url = None
        self.file_size = 0
        self.expected_sha256 = None
        self.installer_filename = None

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

            expected_name = f"{INSTALLER_FILENAME_PREFIX}{self.latest_version}.exe"
            assets = release_data.get('assets', [])
            for asset in assets:
                asset_name = asset.get('name', '')
                if asset_name != expected_name:
                    continue

                download_url = asset.get('browser_download_url')
                if not download_url:
                    raise DownloadError("release asset download URL is missing")
                try:
                    file_size = int(asset.get('size', 0))
                except (TypeError, ValueError) as exc:
                    raise DownloadError("release asset size must be positive") from exc
                if file_size <= 0:
                    raise DownloadError("release asset size must be positive")
                if file_size > MAX_INSTALLER_SIZE:
                    raise DownloadError(
                        "release asset size exceeds maximum installer size"
                    )
                try:
                    expected_sha256 = parse_sha256_digest(asset.get('digest'))
                except IntegrityError as exc:
                    raise DownloadError(str(exc)) from exc

                self.download_url = str(download_url)
                self.file_size = file_size
                self.expected_sha256 = expected_sha256
                self.installer_filename = expected_name
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
        if (
            not self.expected_sha256
            or self.file_size <= 0
            or self.file_size > MAX_INSTALLER_SIZE
            or not self.installer_filename
        ):
            raise DownloadError("release asset integrity metadata is missing or invalid")

        temp_dir = tempfile.mkdtemp(prefix="tunnelforge-bootstrapper-")
        file_path = os.path.join(temp_dir, self.installer_filename)
        part_path = f"{file_path}.part"

        try:
            self._raise_if_cancelled()
            response = requests.get(
                self.download_url,
                stream=True,
                timeout=30
            )
            response.raise_for_status()

            content_length = parse_content_length(
                response.headers.get("content-length")
            )
            if content_length is not None and content_length != self.file_size:
                raise DownloadError(
                    "Content-Length size does not match release metadata"
                )

            downloaded = 0
            with open(part_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    self._raise_if_cancelled()

                    if chunk:
                        if downloaded + len(chunk) > self.file_size:
                            raise DownloadError(
                                "downloaded content exceeds expected size"
                            )
                        f.write(chunk)
                        downloaded += len(chunk)

                        if progress_callback:
                            progress_callback(downloaded, self.file_size)

            self._raise_if_cancelled()
            self.verify_downloaded_installer(part_path)
            self._raise_if_cancelled()
            os.replace(part_path, file_path)
            self._raise_if_cancelled()
            return file_path

        except DownloadError:
            self._cleanup_failed_download(temp_dir, part_path, file_path)
            raise
        except requests.exceptions.RequestException as e:
            self._cleanup_failed_download(temp_dir, part_path, file_path)
            raise DownloadError(f"다운로드 실패: {str(e)}")
        except OSError as e:
            self._cleanup_failed_download(temp_dir, part_path, file_path)
            raise DownloadError(f"파일 저장 실패: {str(e)}")
        except Exception as e:
            self._cleanup_failed_download(temp_dir, part_path, file_path)
            raise DownloadError(f"다운로드 실패: {str(e)}") from e

    def verify_downloaded_installer(self, path: str) -> None:
        """Verify a package against the selected GitHub release metadata."""
        if not self.expected_sha256:
            raise DownloadError("release asset SHA-256 digest is missing or invalid")
        try:
            verify_file_integrity(path, self.expected_sha256, self.file_size)
        except (IntegrityError, OSError) as exc:
            raise DownloadError(str(exc)) from exc

    def _raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise DownloadError("다운로드가 취소되었습니다")

    @staticmethod
    def _cleanup_failed_download(temp_dir: str, *paths: str) -> None:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass


def format_size(size_bytes: int) -> str:
    """바이트 크기를 읽기 쉬운 형식으로 변환"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"
