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


def test_connection_test_worker_does_not_shadow_builtin_finished_signal():
    """WP-3.9 Finding 1 회귀: 결과 전달용 시그널이 QThread 내장 finished()를
    shadow하면 worker 참조 해제 타이밍을 "스레드가 실제로 정지한 뒤"로 안전하게
    잡을 방법이 없어진다. test_finished(bool, str)와 내장 finished()가 서로
    독립적으로 동작해야 한다. 실제 QThread는 절대 start()하지 않고 시그널만
    직접 emit해서 검증한다.
    """
    worker = ConnectionTestWorker(
        TestType.TUNNEL_ONLY, {}, tunnel_engine=None, config_manager=None,
    )

    test_finished_calls = []
    thread_finished_calls = []
    worker.test_finished.connect(lambda success, msg: test_finished_calls.append((success, msg)))
    worker.finished.connect(lambda: thread_finished_calls.append(True))

    worker.test_finished.emit(True, "ok")
    assert test_finished_calls == [(True, "ok")]
    assert thread_finished_calls == []  # 결과 시그널만으로는 스레드 종료 시그널이 울리면 안 된다

    worker.finished.emit()
    assert thread_finished_calls == [True]
    # 내장 finished()는 인자를 받지 않는다 (test_finished와 시그니처가 다름을 재확인)
    assert test_finished_calls == [(True, "ok")]
