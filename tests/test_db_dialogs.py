from unittest.mock import MagicMock

from src.ui.dialogs.db_dialogs import RustDumpWizard, cap_incomplete_export_percent


def test_cap_incomplete_export_percent_prevents_early_100():
    assert cap_incomplete_export_percent(100, completed_tables=35, total_tables=208) == 17


def test_cap_incomplete_export_percent_allows_final_100():
    assert cap_incomplete_export_percent(100, completed_tables=208, total_tables=208) == 100


def test_preselected_export_tunnel_passes_mysql_default_database(monkeypatch):
    captured = {}

    class FakeMySQLConnector:
        def __init__(self, host, port, user, password, database=None):
            captured["host"] = host
            captured["port"] = port
            captured["user"] = user
            captured["password"] = password
            captured["database"] = database

        def connect(self):
            return True, "ok"

    monkeypatch.setattr("src.ui.dialogs.db_dialogs.MySQLConnector", FakeMySQLConnector)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("root", "tunnelpass")
    tunnel_engine = MagicMock()
    tunnel_engine.is_running.return_value = True
    tunnel_engine.get_connection_info.return_value = ("127.0.0.1", 3309)

    wizard = RustDumpWizard(
        tunnel_engine=tunnel_engine,
        config_manager=config_manager,
        preselected_tunnel={
            "id": "mysql-tunnel",
            "name": "MySQL 터널",
            "db_engine": "mysql",
            "default_database": "tf_source84",
        },
    )

    connector, connection_info = wizard._connect_preselected_tunnel()

    assert connector is not None
    assert connection_info == "MySQL 터널_root"
    assert captured == {
        "host": "127.0.0.1",
        "port": 3309,
        "user": "root",
        "password": "tunnelpass",
        "database": "tf_source84",
    }
