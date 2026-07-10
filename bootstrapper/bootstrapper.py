"""TunnelForge 온라인 설치 프로그램 (부트스트래퍼)

GitHub에서 최신 버전을 자동 다운로드하고 설치 프로그램을 실행합니다.
단일 파일로 구성되어 PyInstaller 빌드 시 import 문제가 없습니다.
"""

import os
import sys
import subprocess
import threading
import webbrowser
import tempfile
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, Callable, Tuple

import requests

from src.update_integrity import (
    IntegrityError,
    MAX_INSTALLER_SIZE,
    parse_content_length,
    parse_sha256_digest,
    verify_file_integrity,
)

# ============================================================
# Version Info (from version_info.py)
# ============================================================

__bootstrapper_version__ = "1.0.0"
__app_name__ = "TunnelForge"

# GitHub 저장소 정보
GITHUB_OWNER = "sanghyun-io"
GITHUB_REPO = "tunnelforge"

# GitHub API URL
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

# 다운로드 대상 파일 패턴
INSTALLER_FILENAME_PATTERN = "TunnelForge-Setup-latest.exe"


# ============================================================
# Downloader (from downloader.py)
# ============================================================

class DownloadError(Exception):
    """다운로드 관련 오류"""
    pass


def format_size(size_bytes: int) -> str:
    """바이트 크기를 읽기 쉬운 형식으로 변환"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


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

            expected_name = f"TunnelForge-Setup-{self.latest_version}.exe"
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


# ============================================================
# GUI Application
# ============================================================

class BootstrapperApp:
    """온라인 설치 프로그램 GUI"""

    WINDOW_WIDTH = 450
    WINDOW_HEIGHT = 200
    PADDING = 20

    def __init__(self):
        self.root = tk.Tk()
        self.downloader = InstallerDownloader()
        self.download_thread: Optional[threading.Thread] = None
        self.downloaded_file: Optional[str] = None

        self._setup_window()
        self._create_widgets()

    def _setup_window(self):
        """윈도우 설정"""
        self.root.title(f"{__app_name__} 설치")
        self.root.resizable(False, False)

        # 화면 중앙 배치
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - self.WINDOW_WIDTH) // 2
        y = (screen_height - self.WINDOW_HEIGHT) // 2
        self.root.geometry(f"{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}+{x}+{y}")

        # 아이콘 설정 (있는 경우)
        try:
            if getattr(sys, 'frozen', False):
                # PyInstaller 빌드된 경우
                base_path = sys._MEIPASS
            else:
                # 개발 환경
                base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

            icon_path = os.path.join(base_path, 'assets', 'icon.ico')
            if os.path.exists(icon_path):
                self.root.iconbitmap(icon_path)
        except Exception:
            pass  # 아이콘 설정 실패 시 무시

        # 닫기 버튼 처리
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_widgets(self):
        """UI 위젯 생성"""
        # 메인 프레임
        main_frame = ttk.Frame(self.root, padding=self.PADDING)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 제목 라벨
        title_label = ttk.Label(
            main_frame,
            text=f"{__app_name__} 온라인 설치",
            font=('Segoe UI', 14, 'bold')
        )
        title_label.pack(pady=(0, 15))

        # 상태 라벨
        self.status_label = ttk.Label(
            main_frame,
            text="최신 버전 확인 중...",
            font=('Segoe UI', 10)
        )
        self.status_label.pack(pady=(0, 10))

        # 진행률 바
        self.progress_bar = ttk.Progressbar(
            main_frame,
            mode='indeterminate',
            length=400
        )
        self.progress_bar.pack(pady=(0, 5))
        self.progress_bar.start(10)

        # 상세 정보 라벨
        self.detail_label = ttk.Label(
            main_frame,
            text="",
            font=('Segoe UI', 9),
            foreground='gray'
        )
        self.detail_label.pack(pady=(0, 15))

        # 버튼 프레임
        button_frame = ttk.Frame(main_frame)
        button_frame.pack()

        # 취소 버튼
        self.cancel_button = ttk.Button(
            button_frame,
            text="취소",
            command=self._on_cancel,
            width=15
        )
        self.cancel_button.pack(side=tk.LEFT, padx=5)

        # GitHub 링크 버튼 (초기에는 숨김)
        self.github_button = ttk.Button(
            button_frame,
            text="GitHub에서 다운로드",
            command=self._open_github,
            width=20
        )

    def _update_status(self, text: str):
        """상태 라벨 업데이트 (스레드 안전)"""
        self.root.after(0, lambda: self.status_label.config(text=text))

    def _update_detail(self, text: str):
        """상세 정보 라벨 업데이트 (스레드 안전)"""
        self.root.after(0, lambda: self.detail_label.config(text=text))

    def _update_progress(self, downloaded: int, total: int):
        """진행률 업데이트 (스레드 안전)"""
        def update():
            if self.progress_bar['mode'] == 'indeterminate':
                self.progress_bar.stop()
                self.progress_bar.config(mode='determinate', maximum=100)

            percent = (downloaded / total) * 100 if total > 0 else 0
            self.progress_bar['value'] = percent

            detail_text = f"{format_size(downloaded)} / {format_size(total)} ({percent:.0f}%)"
            self.detail_label.config(text=detail_text)

        self.root.after(0, update)

    def _show_error(self, message: str, show_github: bool = True):
        """에러 표시"""
        def show():
            self.progress_bar.stop()
            self.progress_bar.config(mode='determinate', value=0)
            self.status_label.config(text="오류가 발생했습니다", foreground='red')
            self.detail_label.config(text="")
            self.cancel_button.config(text="닫기")

            if show_github:
                self.github_button.pack(side=tk.LEFT, padx=5)

            messagebox.showerror(
                "다운로드 오류",
                message + "\n\nGitHub에서 직접 다운로드할 수 있습니다."
            )

        self.root.after(0, show)

    def _download_worker(self):
        """백그라운드 다운로드 작업"""
        try:
            # 1. 최신 릴리스 정보 조회
            self.downloader.reset_cancellation()
            self._update_status("최신 버전 확인 중...")
            version, url, size = self.downloader.get_latest_release()

            # 2. 다운로드 시작
            self._update_status(f"v{version} 다운로드 중...")
            if size > 0:
                self._update_detail(f"파일 크기: {format_size(size)}")

            file_path = self.downloader.download_installer(
                progress_callback=self._update_progress
            )
            self.downloaded_file = file_path

            # 3. 다운로드 완료
            self.root.after(0, self._on_download_complete)

        except DownloadError as e:
            self._show_error(str(e))
        except Exception as e:
            self._show_error(f"예상치 못한 오류: {str(e)}")

    def _on_download_complete(self):
        """다운로드 완료 처리"""
        self.progress_bar.config(value=100)
        self.status_label.config(text="다운로드 완료! 설치 프로그램 실행 중...")
        self.detail_label.config(text="")
        self.cancel_button.config(state=tk.DISABLED)

        # 약간의 지연 후 설치 프로그램 실행
        self.root.after(500, self._launch_installer)

    def _launch_installer(self):
        """설치 프로그램 실행"""
        if not self.downloaded_file or not os.path.exists(self.downloaded_file):
            self._show_error("다운로드된 파일을 찾을 수 없습니다.")
            return

        try:
            expected_sha256 = getattr(self.downloader, "expected_sha256", None)
            expected_size = getattr(self.downloader, "file_size", 0)
            if not expected_sha256 or expected_size <= 0:
                raise IntegrityError("release asset integrity metadata is missing or invalid")
            verify_file_integrity(
                self.downloaded_file,
                expected_sha256,
                expected_size,
            )
        except (IntegrityError, OSError) as e:
            try:
                os.remove(self.downloaded_file)
            except OSError:
                pass
            self._show_error(f"다운로드된 설치 파일 검증 실패: {str(e)}")
            return

        try:
            # 설치 프로그램 실행 (부트스트래퍼와 별개 프로세스)
            subprocess.Popen(
                [self.downloaded_file],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )

            # 부트스트래퍼 종료
            self.root.after(500, self.root.destroy)
        except Exception as e:
            self._show_error(
                f"설치 프로그램 실행 실패: {str(e)}\n\n"
                f"파일 위치: {self.downloaded_file}"
            )

    def _on_cancel(self):
        """취소 버튼 클릭"""
        if self.cancel_button.cget('text') == '닫기':
            self.root.destroy()
            return

        if messagebox.askyesno(
            "다운로드 취소",
            "설치를 취소하시겠습니까?"
        ):
            self.downloader.cancel()
            self.root.destroy()

    def _on_close(self):
        """윈도우 닫기"""
        self._on_cancel()

    def _open_github(self):
        """GitHub 릴리스 페이지 열기"""
        webbrowser.open(RELEASES_PAGE_URL)

    def start(self):
        """다운로드 시작"""
        # 500ms 지연 후 다운로드 시작 (UI 표시 후)
        self.root.after(500, self._start_download)

    def _start_download(self):
        """백그라운드 다운로드 시작"""
        self.download_thread = threading.Thread(
            target=self._download_worker,
            daemon=True
        )
        self.download_thread.start()

    def run(self):
        """애플리케이션 실행"""
        self.start()
        self.root.mainloop()


def main():
    """엔트리 포인트"""
    app = BootstrapperApp()
    app.run()


if __name__ == '__main__':
    main()
