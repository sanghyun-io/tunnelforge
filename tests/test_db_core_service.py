import io
import json

from src.core.db_core_service import DbCoreFacade, DbCoreServiceClient, DbEndpoint, RustDbConnector


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
