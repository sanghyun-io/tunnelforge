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


def test_resolve_connection_uses_running_tunnel_without_temp_tunnel():
    class FakeEngine:
        def __init__(self):
            self.temp_created = False

        def is_running(self, tunnel_id):
            return tunnel_id == "t1"

        def get_connection_info(self, tunnel_id):
            return "127.0.0.1", 3307

        def create_temp_tunnel(self, config):
            self.temp_created = True
            return True, object(), None

    engine = FakeEngine()
    worker = ConnectionTestWorker(
        TestType.DB_ONLY,
        {"id": "t1", "db_engine": "mysql", "connection_mode": "ssh_tunnel"},
        tunnel_engine=engine,
        config_manager=None,
    )
    progress = []
    worker.progress.connect(progress.append)

    resolved, failure = worker._resolve_connection(announce_connection=True)

    assert failure is None
    assert resolved.host == "127.0.0.1"
    assert resolved.port == 3307
    assert resolved.temp_server is None
    assert engine.temp_created is False
    assert progress == ["🔗 활성 터널 사용: localhost:3307"]


def test_resolve_connection_creates_temp_tunnel_after_bastion_probe():
    class FakeEngine:
        def __init__(self):
            self.created_with = None

        def is_running(self, tunnel_id):
            return False

        def test_target_reachable_from_bastion(self, config):
            return True, "reachable"

        def create_temp_tunnel(self, config):
            self.created_with = config
            return True, "temp-server", None

        def get_temp_tunnel_port(self, temp_server):
            return 45432

    config = {"id": "t1", "db_engine": "postgresql", "connection_mode": "ssh_tunnel"}
    engine = FakeEngine()
    worker = ConnectionTestWorker(
        TestType.DB_ONLY,
        config,
        tunnel_engine=engine,
        config_manager=None,
    )
    progress = []
    worker.progress.connect(progress.append)

    resolved, failure = worker._resolve_connection(announce_connection=True)

    assert failure is None
    assert resolved.host == "127.0.0.1"
    assert resolved.port == 45432
    assert resolved.temp_server == "temp-server"
    assert engine.created_with is config
    assert progress == [
        "🔎 Bastion → Target DB 포트 도달성 확인 중...",
        "✅ reachable",
        "🔗 임시 SSH 터널 생성 중...",
        "✅ 임시 터널 생성됨: localhost:45432",
    ]


def test_resolve_connection_reports_bastion_reachability_failure():
    class FakeEngine:
        def is_running(self, tunnel_id):
            return False

        def test_target_reachable_from_bastion(self, config):
            return False, "blocked"

    worker = ConnectionTestWorker(
        TestType.DB_ONLY,
        {"id": "t1", "db_engine": "mysql", "connection_mode": "ssh_tunnel"},
        tunnel_engine=FakeEngine(),
        config_manager=None,
    )

    resolved, failure = worker._resolve_connection(announce_connection=False)

    assert resolved is None
    assert failure.kind == "target_unreachable"
    assert failure.message == "blocked"
