"""GitHub Releases에서 설치 프로그램 다운로드

bootstrapper/downloader.py의 로직을 메인 앱용으로 추출한 모듈입니다.
"""

import os
import platform
import tempfile
from dataclasses import dataclass
import requests
from typing import Optional, Callable, Tuple, Sequence, Mapping, Any
from urllib.parse import urlparse

from src.update_integrity import (
    IntegrityError,
    parse_sha256_digest,
    verify_file_integrity,
)
from src.version import GITHUB_OWNER, GITHUB_REPO


# GitHub API URL
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

# 다운로드 대상 파일 패턴 (버전별 파일명 사용)
WINDOWS_INSTALLER_FILENAME_PREFIX = "TunnelForge-Setup-"
MACOS_PACKAGE_FILENAME_PREFIX = "TunnelForge-macOS-"

# Release installers are expected to remain well below this 2 GiB safety cap.
MAX_INSTALLER_SIZE = 2 * 1024 * 1024 * 1024


class DownloadError(Exception):
    """다운로드 관련 오류"""
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int
    sha256: str


def select_release_asset(
    assets: Sequence[Mapping[str, Any]],
    release_version: str,
    platform_name: Optional[str] = None,
    arch_name: Optional[str] = None,
) -> Optional[ReleaseAsset]:
    """Return the version-bound release asset for the current platform."""
    system = platform_name or platform.system()
    arch = arch_name or platform.machine()
    candidates = []

    for asset in assets:
        asset_name = str(asset.get('name', ''))
        if system == "Windows":
            expected_name = f"{WINDOWS_INSTALLER_FILENAME_PREFIX}{release_version}.exe"
            if asset_name != expected_name:
                continue
            rank = 0
        elif system == "Darwin":
            expected_prefix = f"{MACOS_PACKAGE_FILENAME_PREFIX}{release_version}"
            suffix = asset_name[len(expected_prefix):] if asset_name.startswith(expected_prefix) else ""
            valid_suffixes = {
                ".dmg",
                ".zip",
                "-arm64.dmg",
                "-arm64.zip",
                "-x86_64.dmg",
                "-x86_64.zip",
                "-universal.dmg",
                "-universal.zip",
                "-universal2.dmg",
                "-universal2.zip",
            }
            if suffix not in valid_suffixes:
                continue

            arch_rank = _macos_arch_rank(asset_name, arch)
            if arch_rank is None:
                continue
            rank = arch_rank if asset_name.endswith('.dmg') else arch_rank + 1
        else:
            continue

        url = asset.get('browser_download_url')
        if not url:
            raise DownloadError("release asset download URL is missing")

        try:
            size = int(asset.get('size', 0))
        except (TypeError, ValueError) as exc:
            raise DownloadError("release asset size must be positive") from exc
        if size <= 0:
            raise DownloadError("release asset size must be positive")
        if size > MAX_INSTALLER_SIZE:
            raise DownloadError("release asset size exceeds maximum installer size")

        try:
            sha256 = parse_sha256_digest(asset.get('digest'))
        except IntegrityError as exc:
            raise DownloadError(str(exc)) from exc

        candidates.append(
            (rank, ReleaseAsset(asset_name, str(url), size, sha256))
        )

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


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
        self.expected_sha256: Optional[str] = None
        self.installer_filename: Optional[str] = None
        self._cancelled = False
        self._config_manager = config_manager
        self._owned_temp_dirs = set()

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

    @staticmethod
    def _valid_content_length(headers: Mapping[str, Any]) -> Optional[int]:
        try:
            content_length = int(headers.get("content-length"))
        except (TypeError, ValueError):
            return None
        return content_length if content_length >= 0 else None

    def get_installer_info(self) -> Tuple[str, str, int]:
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
                timeout=self.timeout,
                headers={'Accept': 'application/vnd.github.v3+json'}
            )
            response.raise_for_status()

            release_data = response.json()
            self.latest_version = release_data.get('tag_name', '').lstrip('v')

            if not self.latest_version:
                raise DownloadError("버전 정보를 찾을 수 없습니다")

            assets = release_data.get('assets', [])
            selected = select_release_asset(assets, self.latest_version)
            if selected:
                self.installer_filename = selected.name
                self.download_url = selected.url
                self.file_size = selected.size
                self.expected_sha256 = selected.sha256

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
        if (
            not self.expected_sha256
            or self.file_size <= 0
            or self.file_size > MAX_INSTALLER_SIZE
        ):
            raise DownloadError("release asset integrity metadata is missing or invalid")

        temp_dir = tempfile.mkdtemp(prefix="tunnelforge-update-")
        self._owned_temp_dirs.add(os.path.realpath(temp_dir))
        filename = self.installer_filename or os.path.basename(
            urlparse(self.download_url).path
        ) or "TunnelForge-Update"
        file_path = os.path.join(temp_dir, filename)
        part_path = f"{file_path}.part"

        try:
            self._raise_if_cancelled()
            response = requests.get(
                self.download_url,
                stream=True,
                timeout=self.download_timeout
            )
            response.raise_for_status()

            content_length = self._valid_content_length(response.headers)
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
        """Verify a downloaded package against the selected release metadata."""
        if not self.expected_sha256:
            raise DownloadError("release asset SHA-256 digest is missing or invalid")
        try:
            verify_file_integrity(path, self.expected_sha256, self.file_size)
        except (IntegrityError, OSError) as exc:
            raise DownloadError(str(exc)) from exc

    def discard_downloaded_installer(self, path: str) -> None:
        """Remove a returned installer only when its parent is task-owned."""
        installer_path = os.path.realpath(path)
        temp_dir = os.path.dirname(installer_path)
        if temp_dir not in self._owned_temp_dirs:
            return
        self._cleanup_failed_download(
            temp_dir,
            f"{installer_path}.part",
            installer_path,
        )

    def _raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise DownloadError("다운로드가 취소되었습니다")

    def _cleanup_failed_download(self, temp_dir: str, *paths: str) -> None:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass
        self._owned_temp_dirs.discard(os.path.realpath(temp_dir))


def format_size(size_bytes: int) -> str:
    """바이트 크기를 읽기 쉬운 형식으로 변환"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"
