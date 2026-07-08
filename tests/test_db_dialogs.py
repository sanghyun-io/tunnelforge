from unittest.mock import MagicMock

from PyQt6.QtWidgets import QApplication

from src.ui.dialogs.db_dialogs import RustDumpWizard
from src.exporters.rust_dump_exporter import RustDumpConfig
from src.ui.workers.rust_dump_worker import RustDumpWorker

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

def test_preselected_export_tunnel_uses_postgres_connector_for_postgresql(monkeypatch):
    captured = {}

    class FailingMySQLConnector:
        def __init__(self, *args, **kwargs):
            raise AssertionError("PostgreSQL tunnel must not create MySQLConnector")

    class FakePostgresConnector:
        engine = "postgresql"

        def __init__(self, host, port, user, password, database=None):
            captured["host"] = host
            captured["port"] = port
            captured["user"] = user
            captured["password"] = password
            captured["database"] = database

        def connect(self):
            return True, "ok"

    monkeypatch.setattr("src.ui.dialogs.db_dialogs.MySQLConnector", FailingMySQLConnector)
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.PostgresConnector", FakePostgresConnector)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("postgres", "tunnelpass")
    tunnel_engine = MagicMock()
    tunnel_engine.is_running.return_value = True
    tunnel_engine.get_connection_info.return_value = ("127.0.0.1", 55432)

    wizard = RustDumpWizard(
        tunnel_engine=tunnel_engine,
        config_manager=config_manager,
        preselected_tunnel={
            "id": "pg-tunnel",
            "name": "PostgreSQL 터널",
            "db_engine": "postgresql",
            "default_database": "postgres",
            "default_schema": "public",
        },
    )

    connector, connection_info = wizard._connect_preselected_tunnel()

    assert connector is not None
    assert connector.engine == "postgresql"
    assert connection_info == "PostgreSQL 터널_postgres"
    assert captured == {
        "host": "127.0.0.1",
        "port": 55432,
        "user": "postgres",
        "password": "tunnelpass",
        "database": "postgres",
    }

def _make_worker_with_fake_runner(owns_facade: bool):
    config = RustDumpConfig(host="127.0.0.1", port=3306, user="root", password="pw", engine="mysql")
    worker = RustDumpWorker("export_schema", config)

    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    class FakeClient:
        def __init__(self, process):
            self._process = process

    class FakeFacade:
        def __init__(self, client):
            self.client = client

    class FakeRunner:
        def __init__(self, facade, owns):
            self.facade = facade
            self._owns_facade = owns

    process = FakeProcess()
    runner = FakeRunner(FakeFacade(FakeClient(process)), owns_facade)
    worker._active_runner = runner
    return worker, process


def test_rust_dump_worker_cancel_terminates_owned_dedicated_process():
    worker, process = _make_worker_with_fake_runner(owns_facade=True)

    result = worker.cancel()

    assert result is True
    assert worker._cancel_requested is True
    assert process.terminated is True

def test_rust_dump_worker_cancel_does_not_touch_shared_facade_process():
    worker, process = _make_worker_with_fake_runner(owns_facade=False)

    result = worker.cancel()

    assert result is True
    assert worker._cancel_requested is True
    assert process.terminated is False

def test_start_orphan_check_disconnects_connector_after_dialog_exec(monkeypatch):
    app = QApplication.instance() or QApplication([])

    class FakeConnector:
        def __init__(self):
            self.disconnect_calls = 0

        def get_schemas(self):
            return []

        def disconnect(self):
            self.disconnect_calls += 1

    connector = FakeConnector()

    wizard = RustDumpWizard(preselected_tunnel={"id": "t1"})
    monkeypatch.setattr(
        wizard, "_connect_preselected_tunnel", lambda: (connector, "info")
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.OrphanRecordDialog.exec", lambda self: None
    )

    result = wizard.start_orphan_check()

    assert result is True
    assert connector.disconnect_calls == 1
