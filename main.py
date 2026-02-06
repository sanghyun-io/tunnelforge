# -*- coding: utf-8 -*-
import sys
import os
import io
import ctypes
import subprocess
import traceback

# Windows 콘솔 UTF-8 출력 지원 (이모지 출력을 위해)
# GUI 모드(pythonw.exe 또는 PyInstaller --noconsole)에서는 stdout/stderr가 None일 수 있음
if sys.platform == 'win32':
    if sys.stdout is not None and hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    if sys.stderr is not None and hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)


def get_app_dir() -> str:
    """애플리케이션 설치 경로 반환"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 빌드된 경우
        return os.path.dirname(sys.executable)
    else:
        # 개발 환경
        return os.path.dirname(os.path.abspath(__file__))


def show_error_and_offer_recovery(error_message: str):
    """오류 메시지 표시 및 복구 프로그램 실행 제안 (Windows MessageBox 사용)"""
    # Windows API 상수
    MB_YESNO = 0x04
    MB_ICONERROR = 0x10
    IDYES = 6

    app_dir = get_app_dir()
    updater_path = os.path.join(app_dir, "TunnelForge-WebSetup.exe")
    updater_exists = os.path.exists(updater_path)

    if updater_exists:
        message = (
            f"프로그램 실행 중 오류가 발생했습니다.\n\n"
            f"오류 내용:\n{error_message}\n\n"
            f"복구/업데이트 프로그램을 실행하여 최신 버전으로 재설치하시겠습니까?"
        )

        result = ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "TunnelForge - 오류",
            MB_YESNO | MB_ICONERROR
        )

        if result == IDYES:
            try:
                subprocess.Popen(
                    [updater_path],
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            except Exception as e:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"복구 프로그램 실행 실패:\n{str(e)}\n\n"
                    f"수동으로 실행해 주세요:\n{updater_path}",
                    "TunnelForge - 오류",
                    0x10  # MB_ICONERROR
                )
    else:
        # 복구 프로그램이 없는 경우 (개발 환경 등)
        message = (
            f"프로그램 실행 중 오류가 발생했습니다.\n\n"
            f"오류 내용:\n{error_message}\n\n"
            f"GitHub에서 최신 버전을 다운로드해 주세요:\n"
            f"https://github.com/sanghyun-io/tunnelforge/releases"
        )
        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "TunnelForge - 오류",
            0x10  # MB_ICONERROR
        )


def main():
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon

    from src.core import ConfigManager, TunnelEngine
    from src.core.logger import get_logger
    from src.ui.main_window import TunnelManagerUI

    # 루트 로거 초기화
    logger = get_logger('main')

    # Windows 작업표시줄 아이콘을 위한 AppUserModelID 설정
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('tunnelforge.1.0')

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon('assets/icon.ico'))  # 또는 'assets/icon.png'

    # 애플리케이션가 닫혀도 마지막 창이 닫힐 때까지 종료되지 않도록 설정 (트레이 아이콘 때문)
    app.setQuitOnLastWindowClosed(False)

    # 1. 매니저 초기화
    config_mgr = ConfigManager()
    tunnel_engine = TunnelEngine()

    # 2. 설정 파일 경로 안내 (첫 실행 사용자를 위해)
    config_path = config_mgr.get_config_path()
    logger.info(f"설정 파일 위치: {config_path}")

    # 3. UI 실행
    start_minimized = '--minimized' in sys.argv
    window = TunnelManagerUI(config_mgr, tunnel_engine)
    if not start_minimized:
        window.show()
    else:
        logger.info("--minimized 모드: 시스템 트레이에서 시작")

    # 4. 앱 루프 시작
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 전역 예외 핸들러: 시작 시 오류 발생하면 복구 프로그램 안내
        error_msg = f"{type(e).__name__}: {str(e)}"

        # 상세 에러 로그 (디버깅용)
        try:
            error_details = traceback.format_exc()
            # 로그 파일에 기록 시도
            app_dir = get_app_dir()
            log_path = os.path.join(app_dir, "crash.log")
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(f"TunnelForge Crash Log\n")
                f.write(f"=" * 50 + "\n")
                f.write(error_details)
        except:
            pass  # 로그 기록 실패해도 계속 진행

        show_error_and_offer_recovery(error_msg)
        sys.exit(1)