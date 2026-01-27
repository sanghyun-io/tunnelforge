"""TunnelDB Manager 온라인 설치 프로그램 (부트스트래퍼)

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

# ============================================================
# Version Info (from version_info.py)
# ============================================================

__bootstrapper_version__ = "1.0.0"
__app_name__ = "TunnelDB Manager"

# GitHub 저장소 정보
GITHUB_OWNER = "sanghyun-io"
GITHUB_REPO = "db-connector"

# GitHub API URL
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

# 다운로드 대상 파일 패턴
INSTALLER_FILENAME_PATTERN = "TunnelDBManager-Setup-latest.exe"


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
                    if 'TunnelDBManager-Setup' in asset_name and asset_name.endswith('.exe'):
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
        filename = os.path.basename(self.download_url) or "TunnelDBManager-Setup.exe"
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
