"""Tunnel CRUD actions for the main window."""

import copy
import uuid

from PyQt6.QtWidgets import QMessageBox

from src.ui.dialogs.tunnel_config import TunnelConfigDialog


class TunnelActionsController:
    """Owns tunnel configuration create/update/delete actions."""

    def __init__(self, window):
        self._window = window

    def add_tunnel_dialog(self):
        """연결 추가 팝업"""
        window = self._window
        dialog = TunnelConfigDialog(window, tunnel_engine=window.engine)
        if dialog.exec():
            new_data = dialog.get_data()
            new_data = self._process_credentials(new_data)
            window.tunnels.append(new_data)
            self.save_and_refresh()

    def edit_tunnel_dialog(self, tunnel):
        """연결 수정 팝업"""
        window = self._window
        if window.engine.is_running(tunnel["id"]):
            QMessageBox.warning(window, "수정 불가", "실행 중인 터널은 수정할 수 없습니다.\n먼저 연결을 중지해주세요.")
            return

        dialog = TunnelConfigDialog(window, tunnel_data=tunnel, tunnel_engine=window.engine)
        if dialog.exec():
            updated_data = dialog.get_data()
            updated_data = self._process_credentials(updated_data)
            for i, existing in enumerate(window.tunnels):
                if existing["id"] == updated_data["id"]:
                    window.tunnels[i] = updated_data
                    break
            self.save_and_refresh()

    def duplicate_tunnel(self, tunnel):
        """연결 설정 복사하여 새로 만들기"""
        window = self._window
        new_data = copy.deepcopy(tunnel)
        new_data["id"] = str(uuid.uuid4())
        original_name = tunnel.get("name", "Unknown")
        new_data["name"] = f"{original_name} (복사)"

        dialog = TunnelConfigDialog(window, tunnel_data=new_data, tunnel_engine=window.engine)
        dialog.setWindowTitle("연결 복사 - 새 연결 만들기")

        if dialog.exec():
            copied_data = dialog.get_data()
            copied_data["id"] = new_data["id"]
            copied_data = self._process_credentials(copied_data)
            window.tunnels.append(copied_data)
            self.save_and_refresh()
            window.statusBar().showMessage(f"✅ '{copied_data['name']}' 연결이 생성되었습니다.", 3000)

    def delete_tunnel(self, tunnel):
        """연결 삭제"""
        window = self._window
        if window.engine.is_running(tunnel["id"]):
            QMessageBox.warning(window, "삭제 불가", "실행 중인 터널은 삭제할 수 없습니다.")
            return

        confirm = QMessageBox.question(
            window,
            "삭제 확인",
            f"'{tunnel['name']}' 연결 설정을 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if confirm == QMessageBox.StandardButton.Yes:
            window.tunnels = [existing for existing in window.tunnels if existing["id"] != tunnel["id"]]
            self.save_and_refresh()

    def _process_credentials(self, tunnel_data: dict) -> dict:
        """비밀번호 암호화 처리"""
        result = tunnel_data.copy()
        window = self._window

        if "_db_password_plain" in result:
            plain_password = result.pop("_db_password_plain")
            if plain_password:
                result["db_password_encrypted"] = window.config_mgr.encryptor.encrypt(plain_password)

        if not result.get("db_user"):
            result.pop("db_user", None)
            result.pop("db_password_encrypted", None)

        return result

    def save_and_refresh(self):
        """변경사항을 JSON 파일에 저장하고 테이블 새로고침 (기존 설정 보존)"""
        window = self._window
        config = window.config_mgr.load_config()
        config["tunnels"] = window.tunnels
        window.config_mgr.save_config(config)
        window.refresh_table()
        window.statusBar().showMessage("설정이 저장되었습니다.", 2000)
