import pytest

from src.ui.workers.test_worker import ConnectionTestWorker, TestType


def test_resolve_db_engine_uses_saved_config():
    worker = ConnectionTestWorker(
        TestType.DB_ONLY,
        {"db_engine": "postgresql"},
        tunnel_engine=None,
        config_manager=None,
    )

    assert worker._resolve_db_engine("127.0.0.1", 15432) == "postgresql"


def test_resolve_db_engine_requires_saved_config():
    worker = ConnectionTestWorker(
        TestType.DB_ONLY,
        {"remote_port": 5432},
        tunnel_engine=None,
        config_manager=None,
    )

    with pytest.raises(ValueError, match="DB Engine"):
        worker._resolve_db_engine("127.0.0.1", 55315)


def test_postgresql_core_connector_uses_default_database_not_schema(monkeypatch):
    captured = {}

    class FakeRustDbConnector:
        def __init__(self, engine, host, port, user, password, database, schema=""):
            captured["engine"] = engine
            captured["database"] = database
            captured["schema"] = schema

    import types
    import sys

    fake_module = types.SimpleNamespace(RustDbConnector=FakeRustDbConnector)
    monkeypatch.setitem(sys.modules, "src.core.db_core_service", fake_module)
    worker = ConnectionTestWorker(
        TestType.DB_ONLY,
        {
            "db_engine": "postgresql",
            "default_database": "appdb",
            "default_schema": "analytics",
        },
        tunnel_engine=None,
        config_manager=None,
    )

    worker._create_connector("postgresql", "127.0.0.1", 5432, "user", "pw")

    assert captured["engine"] == "postgresql"
    assert captured["database"] == "appdb"
    assert captured["schema"] == "analytics"


def test_mysql_core_connector_uses_default_database(monkeypatch):
    captured = {}

    class FakeRustDbConnector:
        def __init__(self, engine, host, port, user, password, database, schema=""):
            captured["engine"] = engine
            captured["database"] = database
            captured["schema"] = schema

    import types
    import sys

    fake_module = types.SimpleNamespace(RustDbConnector=FakeRustDbConnector)
    monkeypatch.setitem(sys.modules, "src.core.db_core_service", fake_module)
    worker = ConnectionTestWorker(
        TestType.DB_ONLY,
        {
            "db_engine": "mysql",
            "default_database": "tf_source84",
            "default_schema": "",
        },
        tunnel_engine=None,
        config_manager=None,
    )

    worker._create_connector("mysql", "127.0.0.1", 33406, "root", "pw")

    assert captured["engine"] == "mysql"
    assert captured["database"] == "tf_source84"
    assert captured["schema"] == ""
