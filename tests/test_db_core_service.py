import io
import json

from src.core.db_core_service import (
    DbCoreFacade,
    DbCoreServiceError,
    DbCoreServiceClient,
    DbEndpoint,
    RustDbConnector,
    create_rust_db_connector,
    normalize_db_engine,
    parse_db_version_tuple,
)


class FakeProcess:
    def __init__(self, stdout_lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("\n".join(stdout_lines) + "\n")
        self.stderr = io.StringIO()
        self.terminated = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self):
        self.terminated = True


def test_client_sends_jsonl_and_returns_result():
    process = FakeProcess([
        '{"event":"phase","request_id":"req-1","phase":"schema","message":"started"}',
        '{"event":"result","request_id":"req-1","command":"service.hello","success":true}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    events = []

    result = client.request("service.hello", request_id="req-1", on_event=events.append)

    sent = json.loads(process.stdin.getvalue().strip())
    assert sent["command"] == "service.hello"
    assert result["success"] is True
    assert events[0]["event"] == "phase"


def test_client_reports_missing_core_executable_cleanly():
    def missing_executable(*args, **kwargs):
        raise FileNotFoundError("missing")

    client = DbCoreServiceClient(
        executable="missing-tunnelforge-core.exe",
        popen_factory=missing_executable,
    )

    try:
        client.request("service.hello")
    except DbCoreServiceError as exc:
        message = str(exc)
    else:
        raise AssertionError("DbCoreServiceError was not raised")

    assert "Rust DB Core 실행 파일을 찾을 수 없습니다" in message
    assert "missing-tunnelforge-core.exe" in message


def test_client_error_includes_database_error_details():
    process = FakeProcess([
        json.dumps({
            "event": "error",
            "request_id": "req-1",
            "message": "postgresql connection error: db error",
            "code": "3D000",
            "detail": "database public does not exist",
            "context": "connection.open",
        }),
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )

    try:
        client.request("connection.open", request_id="req-1")
    except DbCoreServiceError as exc:
        message = str(exc)
    else:
        raise AssertionError("DbCoreServiceError was not raised")

    assert "postgresql connection error: db error" in message
    assert "code=3D000" in message
    assert "database public does not exist" in message
    assert "connection.open" in message


def test_facade_uses_connection_test_protocol():
    process = FakeProcess([
        '{"event":"result","command":"connection.test","success":true,"message":"connection successful"}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "user", "secret", "app")

    success, message = facade.test_connection(endpoint)

    sent = json.loads(process.stdin.getvalue().strip())
    assert success is True
    assert message == "connection successful"
    assert sent["payload"]["connection"]["password"] == "secret"


def test_facade_uses_dump_protocols():
    process = FakeProcess([
        '{"event":"phase","phase":"dump","message":"started"}',
        '{"event":"result","command":"dump.run","success":true,"rows_dumped":3}',
        '{"event":"result","command":"dump.import","success":true,"rows_imported":3}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    events = []

    dump_result = facade.run_dump({"output_dir": "dump"}, on_event=events.append)
    import_result = facade.import_dump({"input_dir": "dump"})

    sent = [json.loads(line) for line in process.stdin.getvalue().strip().splitlines()]
    assert sent[0]["command"] == "dump.run"
    assert sent[1]["command"] == "dump.import"
    assert dump_result["rows_dumped"] == 3
    assert import_result["rows_imported"] == 3
    assert events[0]["event"] == "phase"


def test_rust_connector_masks_success_message_shape():
    class FakeFacade:
        def open_connection(self, endpoint):
            self.endpoint = endpoint
            return "conn-1"

    fake = FakeFacade()
    connector = RustDbConnector("postgresql", "db.local", 5432, "user", "pw", "postgres", facade=fake)

    success, message = connector.connect()

    assert success is True
    assert message == "연결 성공"
    assert fake.endpoint.engine == "postgresql"


def test_parse_db_version_tuple_handles_rust_core_version_strings():
    assert parse_db_version_tuple("8.4.7") == (8, 4, 7)
    assert parse_db_version_tuple("PostgreSQL 16.2 on x86_64") == (16, 2, 0)
    assert parse_db_version_tuple("") == (0, 0, 0)


def test_rust_connector_get_db_version_returns_legacy_tuple():
    class FakeFacade:
        def open_connection(self, endpoint):
            return "conn-1"

        def execute_on_connection(self, connection_id, query, params=None):
            assert connection_id == "conn-1"
            assert "VERSION()" in query
            return [{"version": "8.4.7"}]

    connector = RustDbConnector(
        "mysql",
        "127.0.0.1",
        3306,
        "root",
        "pw",
        "app",
        facade=FakeFacade(),
    )

    assert connector.get_db_version() == (8, 4, 7)
    assert connector.get_db_version_string() == "8.4.7"


def test_execute_on_connection_sends_params_to_core_protocol():
    process = FakeProcess([
        '{"event":"result","command":"query.execute","success":true,"rows":[{"id":1}]}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)

    rows = facade.execute_on_connection("conn-1", "SELECT * FROM users WHERE id = %s", params=[1])

    sent = json.loads(process.stdin.getvalue().strip())
    assert rows == [{"id": 1}]
    assert sent["payload"]["connection_id"] == "conn-1"
    assert sent["payload"]["params"] == [1]


def test_execute_on_connection_streaming_collects_row_batches():
    process = FakeProcess([
        '{"event":"row_batch","rows":[{"id":1}],"total":2}',
        '{"event":"row_batch","rows":[{"id":2}],"total":2}',
        '{"event":"result","command":"query.execute","success":true,"rows_streamed":2}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    batches = []

    result = facade.execute_on_connection_streaming(
        "conn-1",
        "SELECT * FROM users",
        row_batch_size=1,
        on_batch=batches.append,
    )

    sent = json.loads(process.stdin.getvalue().strip())
    assert sent["payload"]["stream_rows"] is True
    assert sent["payload"]["row_batch_size"] == 1
    assert batches == [[{"id": 1}], [{"id": 2}]]
    assert result["rows_streamed"] == 2


def test_create_rust_db_connector_resolves_postgresql_engine_from_config():
    connector = create_rust_db_connector(
        "postgres",
        "db.local",
        5432,
        "user",
        "pw",
        schema="analytics",
    )

    assert normalize_db_engine("pg") == "postgresql"
    assert connector.endpoint.engine == "postgresql"
    assert connector.endpoint.database == "postgres"
    assert connector.endpoint.schema == "analytics"
