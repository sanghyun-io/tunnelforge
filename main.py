import sys
import ctypes
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon

from src.core import ConfigManager, TunnelEngine
from src.ui.main_window import TunnelManagerUI


def main():
    # Windows 작업표시줄 아이콘을 위한 AppUserModelID 설정
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('dataflare.tunnelmanager.1.0')

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon('assets/icon.ico'))  # 또는 'assets/icon.png'
    
    # 애플리케이션가 닫혀도 마지막 창이 닫힐 때까지 종료되지 않도록 설정 (트레이 아이콘 때문)
    app.setQuitOnLastWindowClosed(False)

    # 1. 매니저 초기화
    config_mgr = ConfigManager()
    tunnel_engine = TunnelEngine()

    # 2. 설정 파일 경로 안내 (첫 실행 사용자를 위해)
    config_path = config_mgr.get_config_path()
    print(f"ℹ️ 설정 파일 위치: {config_path}")

    # 3. UI 실행
    window = TunnelManagerUI(config_mgr, tunnel_engine)
    window.show()

    # 4. 앱 루프 시작
    sys.exit(app.exec())

if __name__ == "__main__":
    main()