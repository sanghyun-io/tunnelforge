"""
업데이트 다운로드 워커
- QThread 기반 비동기 다운로드
- 취소 지원
"""
import threading

from PyQt6.QtCore import QThread, pyqtSignal

from src.core.update_downloader import UpdateDownloader, DownloadError


class UpdateDownloadWorker(QThread):
    """업데이트 다운로드 비동기 워커

    Signals:
        info_fetched: 설치 프로그램 정보 조회 완료 (version, file_size)
        verification_ready: 무결성 메타데이터 준비 완료 (sha256, file_size)
        progress: 다운로드 진행률 (downloaded_bytes, total_bytes)
        finished: 다운로드 완료 (success, file_path_or_error_message)
    """
    info_fetched = pyqtSignal(str, int)   # version, file_size
    verification_ready = pyqtSignal(str, int)  # sha256, file_size
    progress = pyqtSignal(int, int)        # downloaded, total
    finished = pyqtSignal(bool, str)       # success, file_path or error

    def __init__(self, config_manager=None):
        super().__init__()
        self.downloader = UpdateDownloader(config_manager=config_manager)
        self._cancelled = False
        self._state_lock = threading.RLock()

    def run(self):
        """다운로드 실행 (비동기)"""
        if self._is_cancelled():
            return

        try:
            # 1. 설치 프로그램 정보 조회
            version, url, file_size = self.downloader.get_installer_info()

            if self._is_cancelled():
                return

            self.verification_ready.emit(
                self.downloader.expected_sha256, file_size
            )
            self.info_fetched.emit(version, file_size)

            # 2. 다운로드 실행
            file_path = self.downloader.download_installer(
                progress_callback=self._on_progress
            )

            with self._state_lock:
                if self._cancelled:
                    self.downloader.discard_downloaded_installer(file_path)
                else:
                    self.finished.emit(True, file_path)

        except DownloadError as e:
            if not self._is_cancelled():
                self.finished.emit(False, str(e))
        except Exception as e:
            if not self._is_cancelled():
                self.finished.emit(False, f"예기치 않은 오류: {str(e)}")

    def _on_progress(self, downloaded: int, total: int):
        """진행률 콜백"""
        if not self._is_cancelled():
            self.progress.emit(downloaded, total)

    def _is_cancelled(self) -> bool:
        with self._state_lock:
            return self._cancelled

    def cancel(self):
        """다운로드 취소"""
        with self._state_lock:
            self._cancelled = True
            self.downloader.cancel()
