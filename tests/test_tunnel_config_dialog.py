import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget

from src.ui.dialogs.tunnel_config import TunnelConfigDialog


app = QApplication.instance() or QApplication(sys.argv)


class ParentWithTunnels(QWidget):
    def __init__(self):
        super().__init__()
        self.tunnels = [
            {
                "id": "current",
                "name": "Current",
                "connection_mode": "ssh_tunnel",
                "bastion_host": "old-bastion",
                "bastion_port": 22,
                "bastion_user": "old-user",
                "bastion_key": "old.pem",
            },
            {
                "id": "template",
                "name": "Template",
                "connection_mode": "ssh_tunnel",
                "bastion_host": "template-bastion",
                "bastion_port": 2022,
                "bastion_user": "ec2-user",
                "bastion_key": "C:/keys/template.pem",
            },
            {
                "id": "direct",
                "name": "Direct",
                "connection_mode": "direct",
                "bastion_host": "ignore-me",
            },
        ]


def test_copy_bastion_from_another_connection_only_copies_bastion_fields():
    parent = ParentWithTunnels()
    dialog = TunnelConfigDialog(
        parent,
        tunnel_data={
            "id": "current",
            "name": "Current",
            "connection_mode": "ssh_tunnel",
            "remote_host": "db.example.com",
            "remote_port": 3306,
            "default_database": "postgres",
            "default_schema": "app",
        },
    )
    try:
        assert len(dialog.bastion_templates) == 1
        assert dialog.bastion_templates[0]["name"] == "Template"
        assert dialog.btn_copy_bastion.isEnabled()

        dialog._copy_bastion_from_tunnel(dialog.bastion_templates[0])

        assert dialog.input_bastion_host.text() == "template-bastion"
        assert dialog.input_bastion_port.value() == 2022
        assert dialog.input_bastion_user.text() == "ec2-user"
        assert dialog.input_bastion_key.text() == "C:/keys/template.pem"
        assert dialog.input_remote_host.text() == "db.example.com"
        assert dialog.input_remote_port.value() == 3306
        assert dialog.input_default_database.text() == "postgres"
        assert dialog.input_default_schema.text() == "app"
    finally:
        dialog.close()
        parent.close()


def test_db_engine_is_manual_select_field():
    parent = ParentWithTunnels()
    dialog = TunnelConfigDialog(
        parent,
        tunnel_data={
            "id": "current",
            "name": "Current",
            "connection_mode": "ssh_tunnel",
            "remote_host": "db.example.com",
            "remote_port": 5432,
            "db_engine": "postgresql",
        },
    )
    try:
        assert dialog.combo_db_engine.isEnabled()
        assert not hasattr(dialog, "btn_detect_engine")
        assert dialog.combo_db_engine.currentData() == "postgresql"

        mysql_index = dialog.combo_db_engine.findData("mysql")
        dialog.combo_db_engine.setCurrentIndex(mysql_index)
        assert dialog.get_data()["db_engine"] == "mysql"
    finally:
        dialog.close()
        parent.close()
