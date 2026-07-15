import ast
import io
import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

import src.core.db_core_dbapi_shim as db_core_dbapi_shim
import src.core.db_core_service as db_core_service
from src.core.db_core_service import (
    DbCoreOutcome,
    DbCoreFacade,
    DbCoreRequestKind,
    DbCoreServiceError,
    DbCoreServiceClient,
    DbEndpoint,
    RustDbConnection,
    RustDbConnector,
    create_rust_db_connector,
    normalize_db_engine,
    parse_db_version_tuple,
)
from src.core.sql_query_classifier import (
    classify_sql_statement,
    is_mysql_implicit_commit_ddl,
    statement_returns_rows,
)


@dataclass(frozen=True)
class _ExpectedConnectionHandle:
    connection_id: str
    process_generation: int


def _connection_handle(connection_id="conn-1", process_generation=1):
    handle_type = getattr(
        db_core_service,
        "DbCoreConnectionHandle",
        _ExpectedConnectionHandle,
    )
    return handle_type(connection_id, process_generation)


def _service_error(
    message,
    *,
    code="db_core_business_failure",
    request_kind=DbCoreRequestKind.MUTATION,
    outcome=DbCoreOutcome.FAILED,
    request_id="test-request",
    process_generation=1,
    rust_code=None,
    payload=None,
):
    return DbCoreServiceError(
        message,
        code=code,
        request_kind=request_kind,
        outcome=outcome,
        request_id=request_id,
        process_generation=process_generation,
        rust_code=rust_code,
        payload=payload or {},
    )


class FakeProcess:
    def __init__(self, stdout_lines, stderr_text=""):
        self._stdout_lines = list(stdout_lines)
        self.stdout = _AsyncTextReader("")
        self.stdin = _AsyncTextWriter(self._handle_request)
        self.stderr = _AsyncTextReader(stderr_text)
        self.returncode = None

    def _handle_request(self, request):
        request_id = request["request_id"]
        if request_id.startswith("py-hello-"):
            self.stdout.append(json.dumps({
                "event": "result",
                "request_id": request_id,
                "command": "service.hello",
                "success": True,
                "service": "tunnelforge-core",
                "protocol_version": 1,
                "process_version": 1,
                "process_capabilities": [
                    "request.deadline",
                    "request.strict_id",
                    "process.generation",
                    "mutation.outcome_indeterminate",
                ],
                "max_jsonl_frame_bytes": 1_048_576,
                "max_assembled_event_bytes": 64 * 1024 * 1024,
                "max_assembled_event_chunks": 4_096,
                "max_assembled_event_nodes": 65_536,
                "max_assembled_event_depth": 128,
            }))
            return
        while self._stdout_lines:
            event = json.loads(self._stdout_lines.pop(0))
            event["request_id"] = request_id
            self.stdout.append(json.dumps(event))
            if event.get("event") in ("result", "error"):
                return

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


class _AsyncTextWriter:
    def __init__(self, on_request=None):
        self._buffer = io.StringIO()
        self._on_request = on_request

    def write(self, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        written = len(data)
        for line in data.splitlines():
            if not line.strip():
                continue
            request = json.loads(line)
            if not str(request.get("request_id", "")).startswith("py-hello-"):
                self._buffer.write(line + "\n")
            if self._on_request is not None:
                self._on_request(request)
        return written

    async def drain(self):
        return None

    def getvalue(self):
        return self._buffer.getvalue()


class _AsyncTextReader:
    def __init__(self, text):
        self._lines = text.splitlines(keepends=True)

    def append(self, line):
        self._lines.append(line.rstrip("\n") + "\n")

    async def readline(self):
        if not self._lines:
            return ""
        return self._lines.pop(0)


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
            "command": "connection.open",
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


def test_facade_open_business_failure_keeps_definite_outcome():
    process = FakeProcess([
        '{"event":"result","command":"connection.open","success":false,"message":"refused"}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "user", "secret", "app")

    with pytest.raises(DbCoreServiceError) as raised:
        facade.open_connection(endpoint)

    assert raised.value.code == "db_core_business_failure"
    assert raised.value.outcome is DbCoreOutcome.DEFINITE
    assert raised.value.request_kind is DbCoreRequestKind.MUTATION


def test_open_connection_returns_generation_handle():
    process = FakeProcess([
        '{"event":"result","command":"connection.open","success":true,"connection_id":"conn-1"}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "user", "secret", "app")

    handle = facade.open_connection(endpoint)

    assert hasattr(db_core_service, "DbCoreConnectionHandle")
    assert isinstance(handle, db_core_service.DbCoreConnectionHandle)
    assert handle.connection_id == "conn-1"
    assert handle.process_generation == client.process_generation == 1


@pytest.mark.parametrize(
    "operation",
    [
        lambda facade: facade.close_connection("conn-1"),
        lambda facade: facade.execute_on_connection("conn-1", "SELECT 1"),
        lambda facade: facade.execute_on_connection_result("conn-1", "SELECT 1"),
        lambda facade: facade.execute_on_connection_streaming("conn-1", "SELECT 1"),
    ],
)
def test_raw_connection_ids_are_rejected_before_wire(operation):
    process = FakeProcess([])
    process_starts = []
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process_starts.append(True) or process,
    )
    facade = DbCoreFacade(client)

    with pytest.raises(DbCoreServiceError) as raised:
        operation(facade)

    assert raised.value.code == "db_core_stale_connection"
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert raised.value.request_kind is DbCoreRequestKind.MUTATION
    assert process_starts == []
    assert process.stdin.getvalue() == ""


def test_stale_handle_rejected_before_wire():
    process = FakeProcess([
        '{"event":"result","command":"service.hello","success":true}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    facade.hello()
    stale_handle = _connection_handle(process_generation=client.process_generation + 1)
    writes_before = process.stdin.getvalue()

    with pytest.raises(DbCoreServiceError) as raised:
        facade.execute_on_connection(stale_handle, "SELECT 1")

    assert raised.value.code == "db_core_stale_connection"
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert raised.value.request_kind is DbCoreRequestKind.MUTATION
    assert process.stdin.getvalue() == writes_before


def _reused_connection_id_fixture():
    first_process = FakeProcess([
        '{"event":"result","command":"connection.open","success":true,"connection_id":"reused"}',
    ])
    second_process = FakeProcess([
        '{"event":"result","command":"connection.open","success":true,"connection_id":"reused"}',
        '{"event":"result","command":"query.execute","success":true,"rows":[{"value":2}],"columns":["value"]}',
    ])
    processes = iter([first_process, second_process])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: next(processes),
    )
    facade = DbCoreFacade(client)
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "user", "secret", "app")
    old_handle = facade.open_connection(endpoint)
    old_cursor = RustDbConnection(endpoint, facade, old_handle).cursor()
    first_process.returncode = 1
    new_handle = facade.open_connection(endpoint)
    new_cursor = RustDbConnection(endpoint, facade, new_handle).cursor()
    return second_process, old_cursor, new_cursor, old_handle, new_handle


def test_old_cursor_rejected_when_new_generation_reuses_same_connection_id():
    process, old_cursor, _new_cursor, old_handle, new_handle = _reused_connection_id_fixture()
    writes_before = process.stdin.getvalue()

    with pytest.raises(DbCoreServiceError) as raised:
        old_cursor.execute("SELECT 1 AS value")

    assert old_handle.connection_id == new_handle.connection_id == "reused"
    assert old_handle.process_generation != new_handle.process_generation
    assert raised.value.code == "db_core_stale_connection"
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert process.stdin.getvalue() == writes_before


def test_new_cursor_with_reused_id_succeeds():
    _process, _old_cursor, new_cursor, _old_handle, new_handle = _reused_connection_id_fixture()

    rowcount = new_cursor.execute("SELECT 2 AS value")

    assert new_handle.connection_id == "reused"
    assert rowcount == 1
    assert new_cursor.fetchone() == {"value": 2}


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


def test_facade_uses_oneclick_protocol():
    process = FakeProcess([
        '{"event":"phase","phase":"preflight","message":"started"}',
        '{"event":"result","command":"oneclick.run","success":true,"report":{"schema":"app","success":true}}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    events = []

    result = facade.run_oneclick({"schema": "app"}, on_event=events.append)

    sent = json.loads(process.stdin.getvalue().strip())
    assert sent["command"] == "oneclick.run"
    assert result["report"]["schema"] == "app"
    assert events[0]["phase"] == "preflight"


def test_facade_uses_oneclick_apply_fixes_protocol():
    process = FakeProcess([
        '{"event":"phase","phase":"execution","message":"started"}',
        '{"event":"result","command":"oneclick.apply_fixes","success":true,"success_count":1}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    events = []

    result = facade.apply_oneclick_fixes({"schema": "app"}, on_event=events.append)

    sent = json.loads(process.stdin.getvalue().strip())
    assert sent["command"] == "oneclick.apply_fixes"
    assert result["success_count"] == 1
    assert events[0]["phase"] == "execution"


def test_facade_uses_oneclick_derive_charset_contracts_protocol():
    process = FakeProcess([
        '{"event":"phase","phase":"recommendation","message":"started"}',
        '{"event":"result","command":"oneclick.derive_charset_contracts","success":true,"contracts":[{"issue_index":0}]}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    events = []

    result = facade.derive_oneclick_charset_contracts(
        {"schema": "tf_oneclick_app"},
        on_event=events.append,
    )

    sent = json.loads(process.stdin.getvalue().strip())
    assert sent["command"] == "oneclick.derive_charset_contracts"
    assert result["contracts"] == [{"issue_index": 0}]
    assert events[0]["phase"] == "recommendation"


def test_facade_explicitly_classifies_every_request_kind_and_preserves_shapes():
    calls = []

    class SpyClient:
        process_generation = 7

        def request_result(self, command, payload=None, **kwargs):
            calls.append((command, kwargs["request_kind"], kwargs.get("required_generation")))
            return db_core_service.DbCoreRequestResult(
                request_kind=kwargs["request_kind"],
                outcome=DbCoreOutcome.DEFINITE,
                request_id="open-request",
                process_generation=self.process_generation,
                message="",
                rust_code=None,
                payload={"success": True, "connection_id": "conn-7"},
            )

        def request_payload(self, command, payload=None, **kwargs):
            calls.append((command, kwargs["request_kind"], kwargs.get("required_generation")))
            if kwargs.get("on_event") is not None:
                kwargs["on_event"]({"event": "row_batch", "rows": [{"id": 1}]})
            return {
                "success": True,
                "message": "ok",
                "service": "tunnelforge-core",
                "schema": {"tables": []},
                "tables": ["users"],
                "differences": [{"kind": "added"}],
                "rows": [{"id": 1}],
                "columns": ["id"],
                "rows_affected": 1,
            }

    facade = DbCoreFacade(SpyClient())
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "user", "secret", "app")

    assert facade.hello().get("service") == "tunnelforge-core"
    assert facade.test_connection(endpoint) == (True, "ok")
    handle = facade.open_connection(endpoint)
    assert facade.close_connection(handle) is True
    assert facade.inspect_schema(endpoint)["tables"] == []
    assert facade.list_tables(endpoint)[0] == "users"
    assert facade.schema_diff({}, {})[0]["kind"] == "added"
    assert facade.execute_query(endpoint, "SELECT 1")[0]["id"] == 1
    assert facade.execute_on_connection(handle, "SELECT 1")[0]["id"] == 1
    batches = []
    assert facade.execute_on_connection_streaming(
        handle,
        "SELECT 1",
        on_batch=batches.append,
    ).get("success") is True
    assert batches == [[{"id": 1}]]
    assert facade.run_migration({}).get("success") is True
    assert facade.verify_migration({}).get("success") is True
    assert facade.run_dump({}).get("success") is True
    assert facade.import_dump({}).get("success") is True
    assert facade.run_oneclick({}).get("success") is True
    assert facade.derive_oneclick_charset_contracts({}).get("success") is True
    assert facade.apply_oneclick_fixes({}).get("success") is True

    assert calls == [
        ("service.hello", DbCoreRequestKind.READ_ONLY, None),
        ("connection.test", DbCoreRequestKind.READ_ONLY, None),
        ("connection.open", DbCoreRequestKind.MUTATION, None),
        ("connection.close", DbCoreRequestKind.MUTATION, 7),
        ("schema.inspect", DbCoreRequestKind.READ_ONLY, None),
        ("schema.list", DbCoreRequestKind.READ_ONLY, None),
        ("schema.diff", DbCoreRequestKind.READ_ONLY, None),
        ("query.execute", DbCoreRequestKind.MUTATION, None),
        ("query.execute", DbCoreRequestKind.MUTATION, 7),
        ("query.execute", DbCoreRequestKind.MUTATION, 7),
        ("migration.run", DbCoreRequestKind.MUTATION, None),
        ("migration.verify", DbCoreRequestKind.READ_ONLY, None),
        ("dump.run", DbCoreRequestKind.MUTATION, None),
        ("dump.import", DbCoreRequestKind.MUTATION, None),
        ("oneclick.run", DbCoreRequestKind.MUTATION, None),
        ("oneclick.derive_charset_contracts", DbCoreRequestKind.READ_ONLY, None),
        ("oneclick.apply_fixes", DbCoreRequestKind.MUTATION, None),
    ]


def test_owned_request_consumers_explicitly_classify_request_kind():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "src/core/db_core_facade.py",
        root / "scripts/capture-oneclick-real-execution-evidence.py",
    ]
    missing = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"request", "request_payload", "request_result"}:
                continue
            if not any(keyword.arg == "request_kind" for keyword in node.keywords):
                missing.append(f"{path.relative_to(root)}:{node.lineno}")

    assert missing == []


def test_rust_connector_masks_success_message_shape():
    class FakeFacade:
        def open_connection(self, endpoint):
            self.endpoint = endpoint
            return _connection_handle()

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
            return _connection_handle()

        def execute_on_connection_result(self, connection_handle, query, params=None):
            assert connection_handle == _connection_handle()
            assert "VERSION()" in query
            return {"rows": [{"version": "8.4.7"}], "columns": ["version"], "rows_affected": 0}

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
        '{"event":"result","command":"connection.open","success":true,"connection_id":"conn-1"}',
        '{"event":"result","command":"query.execute","success":true,"rows":[{"id":1}]}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    handle = facade.open_connection(endpoint)

    rows = facade.execute_on_connection(handle, "SELECT * FROM users WHERE id = %s", params=[1])

    sent = json.loads(process.stdin.getvalue().strip().splitlines()[-1])
    assert rows == [{"id": 1}]
    assert sent["payload"]["connection_id"] == "conn-1"
    assert sent["payload"]["params"] == [1]


def test_execute_on_connection_streaming_collects_row_batches():
    process = FakeProcess([
        '{"event":"result","command":"connection.open","success":true,"connection_id":"conn-1"}',
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
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    handle = facade.open_connection(endpoint)

    result = facade.execute_on_connection_streaming(
        handle,
        "SELECT * FROM users",
        row_batch_size=1,
        on_batch=batches.append,
    )

    sent = json.loads(process.stdin.getvalue().strip().splitlines()[-1])
    assert sent["payload"]["stream_rows"] is True
    assert sent["payload"]["row_batch_size"] == 1
    assert batches == [[{"id": 1}], [{"id": 2}]]
    assert result["rows_streamed"] == 2


def test_rust_db_cursor_rowcount_uses_core_rows_affected_for_dml():
    process = FakeProcess([
        '{"event":"result","command":"connection.open","success":true,"connection_id":"conn-1"}',
        '{"event":"result","command":"query.execute","success":true,"rows":[],"rows_affected":7}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, facade, facade.open_connection(endpoint))

    with connection.cursor() as cursor:
        rowcount = cursor.execute("UPDATE users SET active = 0")

    assert rowcount == 7
    assert cursor.rowcount == 7
    assert cursor.fetchall() == []


def test_rust_db_cursor_calls_execute_on_connection_result_unconditionally():
    class FakeFacade:
        def execute_on_connection_result(self, connection_handle, query, params=None):
            assert connection_handle == _connection_handle()
            return {"rows": [], "rows_affected": 7}

    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, FakeFacade(), _connection_handle())

    with connection.cursor() as cursor:
        rowcount = cursor.execute("UPDATE users SET active = 0")

    assert rowcount == 7
    assert cursor.rowcount == 7


def test_rust_db_cursor_executemany_rejects_python_batch_helper():
    class FakeFacade:
        def __init__(self):
            self.calls = []

        def execute_on_connection(self, connection_id, query, params=None):
            self.calls.append((connection_id, query, params))
            return []

    facade = FakeFacade()
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, facade, _connection_handle())

    with connection.cursor() as cursor:
        try:
            cursor.executemany(
                "INSERT INTO users (id, name) VALUES (%s, %s)",
                [(1, "Alice")],
            )
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError("RustDbCursor.executemany should fail closed")

    assert "Rust Core" in message
    assert facade.calls == []


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


def test_statement_returns_rows_skips_comments_parens_and_recognizes_keywords():
    row_returning = [
        "-- comment\nSELECT 1",
        "# comment\nSELECT 1",
        "/*x*/ SELECT 1",
        "(SELECT 1)",
        "WITH c AS (SELECT 1) SELECT * FROM c",
        "CALL p()",
        "VALUES (1)",
        "TABLE users",
    ]
    for sql in row_returning:
        assert statement_returns_rows(sql) is True, sql

    assert statement_returns_rows("UPDATE users SET name='x'") is False


def test_is_mysql_implicit_commit_ddl_recognizes_single_and_paired_keywords():
    classification = classify_sql_statement("CREATE TABLE t(id int)")
    assert classification.returns_rows is False
    assert classification.mysql_implicit_commit_ddl is True

    assert is_mysql_implicit_commit_ddl("ANALYZE TABLE users") is True
    assert is_mysql_implicit_commit_ddl("LOCK TABLES users READ") is True
    assert is_mysql_implicit_commit_ddl("DROP INDEX idx ON users") is True
    assert is_mysql_implicit_commit_ddl("SELECT 1") is False


def test_execute_on_connection_result_preserves_columns():
    process = FakeProcess([
        '{"event":"result","command":"connection.open","success":true,"connection_id":"conn-1"}',
        json.dumps({
            "event": "result",
            "command": "query.execute",
            "success": True,
            "rows": [],
            "columns": ["id"],
            "rows_affected": 0,
        }),
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    handle = facade.open_connection(endpoint)

    result = facade.execute_on_connection_result(handle, "SELECT id FROM users WHERE 1=0")

    assert result["columns"] == ["id"]
    assert result["rows"] == []


def test_rust_db_cursor_empty_select_reports_column_metadata():
    class FakeFacade:
        def execute_on_connection_result(self, connection_handle, query, params=None):
            return {"rows": [], "columns": ["id", "name"], "rows_affected": 0}

    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, FakeFacade(), _connection_handle())

    with connection.cursor() as cursor:
        cursor.execute("-- check\nSELECT id, name FROM users WHERE 1=0")
        assert cursor.description == [("id",), ("name",)]
        assert cursor.rowcount == 0
        assert cursor.fetchall() == []


def test_rust_db_cursor_comment_prefixed_select_keeps_empty_description_not_none():
    class FakeFacade:
        def execute_on_connection_result(self, connection_handle, query, params=None):
            return {"rows": [], "columns": [], "rows_affected": 0}

    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, FakeFacade(), _connection_handle())

    with connection.cursor() as cursor:
        cursor.execute("-- comment\nSELECT * FROM users WHERE 1=0")
        assert cursor.description == []
        assert cursor.description is not None


def test_rust_db_cursor_dml_has_no_description():
    class FakeFacade:
        def execute_on_connection_result(self, connection_handle, query, params=None):
            return {"rows": [], "columns": [], "rows_affected": 7}

    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, FakeFacade(), _connection_handle())

    with connection.cursor() as cursor:
        rowcount = cursor.execute("UPDATE users SET active = 0")

    assert cursor.description is None
    assert rowcount == 7
    assert cursor.rowcount == 7


def test_rust_db_connection_ping_issues_exactly_select_1():
    class FakeFacade:
        def __init__(self):
            self.calls = []

        def execute_on_connection(self, connection_handle, query, params=None):
            self.calls.append((connection_handle, query))
            return []

    facade = FakeFacade()
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    handle = _connection_handle()
    connection = RustDbConnection(endpoint, facade, handle)

    connection.ping()

    assert facade.calls == [(handle, "SELECT 1")]


def test_rust_db_connection_ping_closes_on_failure_and_closed_ping_raises():
    class FailingFacade:
        def execute_on_connection(self, connection_handle, query, params=None):
            raise _service_error(
                "connection lost",
                code="db_core_process_died",
                outcome=DbCoreOutcome.OUTCOME_INDETERMINATE,
            )

    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, FailingFacade(), _connection_handle())

    try:
        connection.ping()
    except DbCoreServiceError:
        pass
    else:
        raise AssertionError("ping should propagate the facade error")

    assert connection.open is False

    try:
        connection.ping()
    except DbCoreServiceError as exc:
        assert "connection is closed" in str(exc)
    else:
        raise AssertionError("ping on a closed connection should raise")


def test_transport_failure_does_not_retry_or_reopen_connection():
    class FailingFacade:
        def __init__(self):
            self.open_calls = 0
            self.execute_calls = 0

        def open_connection(self, endpoint):
            self.open_calls += 1
            return _connection_handle()

        def execute_on_connection(self, connection_handle, query, params=None):
            self.execute_calls += 1
            raise _service_error(
                "transport lost after write",
                code="db_core_process_died",
                outcome=DbCoreOutcome.OUTCOME_INDETERMINATE,
            )

    facade = FailingFacade()
    connector = RustDbConnector(
        "mysql",
        "127.0.0.1",
        3306,
        "root",
        "pw",
        "app",
        facade=facade,
    )
    assert connector.connect()[0] is True

    with pytest.raises(DbCoreServiceError) as raised:
        connector.connection.ping(reconnect=True)

    assert raised.value.outcome is DbCoreOutcome.OUTCOME_INDETERMINATE
    assert facade.open_calls == 1
    assert facade.execute_calls == 1
    assert connector.connection.open is False


def test_bind_sql_params_and_sql_literal_helpers_are_removed():
    assert not hasattr(db_core_service, "bind_sql_params")
    assert not hasattr(db_core_service, "sql_literal")


def test_db_core_service_reexports_all_public_names_after_module_split():
    """db_core_service는 db_core_client/db_core_facade/db_core_dbapi_shim 분할 후에도
    순수 재수출 모듈로서 기존 공개 이름을 전부 제공해야 한다 (import 경로 호환성 보장)."""
    expected_names = [
        "DbCoreServiceError",
        "DbCoreCallbackError",
        "DbCoreRequestKind",
        "DbCoreOutcome",
        "DbCoreGenerationState",
        "DbCoreRequestResult",
        "DbCoreConnectionHandle",
        "MAX_JSONL_FRAME_BYTES",
        "DB_CORE_STDIN_HIGH_WATER_BYTES",
        "REQUIRED_PROCESS_CAPABILITIES",
        "DEFAULT_REQUEST_TIMEOUT_SECONDS",
        "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
        "_format_error_event",
        "SUPPORTED_DB_ENGINES",
        "parse_db_version_tuple",
        "normalize_db_engine",
        "default_database_for_engine",
        "DbEndpoint",
        "DbCoreServiceClient",
        "DbCoreFacade",
        "get_shared_db_core_facade",
        "shutdown_shared_db_core_facade",
        "RustDbConnector",
        "create_rust_db_connector",
        "RustDbConnection",
        "RustDbCursor",
        "quote_mysql_ident",
    ]
    for name in expected_names:
        assert hasattr(db_core_service, name), f"db_core_service.{name} must remain re-exported"


def test_owned_service_error_construction_requires_full_metadata():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "src/core/db_core_facade.py",
        root / "src/core/db_core_dbapi_shim.py",
        root / "src/core/postgres_connector.py",
        root / "src/exporters/rust_dump_exporter.py",
        root / "src/ui/workers/rust_dump_worker.py",
        root / "scripts/capture-oneclick-real-execution-evidence.py",
        root / "tests/test_db_core_service.py",
        root / "tests/test_db_connector.py",
        root / "tests/test_db_dialogs.py",
        root / "tests/test_rust_dump_exporter.py",
    ]
    required_keywords = {
        "code",
        "request_kind",
        "outcome",
        "request_id",
        "process_generation",
        "rust_code",
        "payload",
    }
    incomplete = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if function_name != "DbCoreServiceError":
                continue
            keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
            missing = sorted(required_keywords - keywords)
            if missing:
                incomplete.append(
                    f"{path.relative_to(root)}:{node.lineno} missing {','.join(missing)}"
                )

    assert incomplete == []


def test_stderr_is_drained_in_background_and_available_as_tail():
    process = FakeProcess(
        ['{"event":"result","command":"service.hello","success":true}'],
        stderr_text="warning: slow query\nwarning: retrying\n",
    )
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )

    result = client.request("service.hello")
    assert result["success"] is True

    deadline = time.time() + 2.0
    tail = ""
    while time.time() < deadline:
        tail = client._stderr_tail_text()
        if "retrying" in tail:
            break
        time.sleep(0.02)

    assert "warning: slow query" in tail
    assert "warning: retrying" in tail


def test_request_eof_error_does_not_call_stderr_read():
    class ExplodingStderr:
        async def readline(self):
            return ""

        def read(self):
            raise AssertionError("stderr.read() must not be called; the drained tail must be used instead")

    class EOFProcess:
        def __init__(self):
            self.stdin = _AsyncTextWriter()
            self.stdout = _AsyncTextReader("")
            self.stderr = ExplodingStderr()
            self.returncode = None

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    process = EOFProcess()
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )

    try:
        client.request("service.hello")
    except DbCoreServiceError:
        pass
    else:
        raise AssertionError("expected DbCoreServiceError on stdout EOF")


def test_request_after_shutdown_rejects_stopped_owner_without_restart():
    process = FakeProcess([])
    popen_calls = []

    def popen_factory(*args, **kwargs):
        popen_calls.append(args)
        return process

    client = DbCoreServiceClient(executable="fake-core", popen_factory=popen_factory)
    client.start()
    client.shutdown(timeout_seconds=0.5)

    with pytest.raises(DbCoreServiceError) as raised:
        client.request("service.hello")

    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert len(popen_calls) == 1


# =====================================================================
# RustDbConnector: 공유 facade / SYSTEM_SCHEMAS / 메타데이터 예외 처리
# (WP-2.10: connector-facade 통합)
# =====================================================================

def test_rust_connector_uses_shared_facade_by_default(monkeypatch):
    """facade 미지정 시 앱 공유 facade를 사용해야 한다 (커넥터별 전용 프로세스 금지)."""
    sentinel = object()
    monkeypatch.setattr(db_core_dbapi_shim, "get_shared_db_core_facade", lambda: sentinel)

    connector = RustDbConnector("mysql", "127.0.0.1", 3306, "root", "pw", "app")

    assert connector.facade is sentinel


def test_rust_connector_uses_injected_facade_without_shared_lookup(monkeypatch):
    """facade가 주입되면 공유 facade 조회를 건너뛴다."""
    called = []
    monkeypatch.setattr(
        db_core_dbapi_shim, "get_shared_db_core_facade",
        lambda: called.append(True) or object(),
    )
    sentinel = object()

    connector = RustDbConnector("mysql", "127.0.0.1", 3306, "root", "pw", "app", facade=sentinel)

    assert connector.facade is sentinel
    assert called == []


def test_rust_connector_uses_constants_for_system_schema_filtering(monkeypatch):
    """MySQL 스키마 필터링은 하드코딩이 아닌 SYSTEM_SCHEMAS 상수를 사용해야 한다."""
    monkeypatch.setattr(db_core_dbapi_shim, "SYSTEM_SCHEMAS", frozenset({"mysql", "ndbinfo"}))

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return [{"Database": "app"}, {"Database": "mysql"}, {"Database": "ndbinfo"}]

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    connector = RustDbConnector("mysql", "127.0.0.1", 3306, "root", "pw", "app", facade=object())
    connector.connection = FakeConnection()

    assert connector.get_schemas() == ["app"]


class _FailingCursor:
    """execute 호출 시 지정된 예외를 던지는 cursor 스텁."""

    def __init__(self, exc):
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        raise self._exc


class _FailingConnection:
    def __init__(self, exc):
        self._exc = exc

    def cursor(self):
        return _FailingCursor(self._exc)


def test_get_schemas_propagates_and_logs_db_core_service_error(caplog):
    connector = RustDbConnector("mysql", "127.0.0.1", 3306, "root", "pw", "app", facade=object())
    connector.connection = _FailingConnection(_service_error("Access denied"))

    caplog.set_level("ERROR")
    with pytest.raises(DbCoreServiceError):
        connector.get_schemas()

    assert "get_schemas" in caplog.text
    assert "Access denied" in caplog.text


def test_schema_exists_propagates_and_logs_db_core_service_error(caplog):
    connector = RustDbConnector("mysql", "127.0.0.1", 3306, "root", "pw", "app", facade=object())
    connector.connection = _FailingConnection(_service_error("Access denied"))

    caplog.set_level("ERROR")
    with pytest.raises(DbCoreServiceError):
        connector.schema_exists("app")

    assert "schema_exists" in caplog.text
    assert "Access denied" in caplog.text


def test_get_tables_propagates_and_logs_db_core_service_error(caplog):
    class FailingFacade:
        def list_tables(self, endpoint):
            raise _service_error("Access denied")

    connector = RustDbConnector("mysql", "127.0.0.1", 3306, "root", "pw", "app", facade=FailingFacade())

    caplog.set_level("ERROR")
    with pytest.raises(DbCoreServiceError):
        connector.get_tables()

    assert "get_tables" in caplog.text
    assert "Access denied" in caplog.text


def test_get_db_version_string_propagates_and_logs_db_core_service_error(caplog):
    connector = RustDbConnector("mysql", "127.0.0.1", 3306, "root", "pw", "app", facade=object())
    connector.connection = _FailingConnection(_service_error("Access denied"))

    caplog.set_level("ERROR")
    with pytest.raises(DbCoreServiceError):
        connector.get_db_version_string()

    assert "get_db_version_string" in caplog.text
    assert "Access denied" in caplog.text


def test_get_column_names_propagates_and_logs_db_core_service_error(caplog):
    connector = RustDbConnector("mysql", "127.0.0.1", 3306, "root", "pw", "app", facade=object())
    connector.connection = _FailingConnection(_service_error("Access denied"))

    caplog.set_level("ERROR")
    with pytest.raises(DbCoreServiceError):
        connector.get_column_names("users")

    assert "get_column_names" in caplog.text
    assert "Access denied" in caplog.text


def test_get_schemas_generic_exception_is_logged_and_returns_empty_default():
    """facade 예외가 아닌 일반 예외는 기존 계약대로 빈 기본값을 반환한다 (호환성 유지)."""
    connector = RustDbConnector("mysql", "127.0.0.1", 3306, "root", "pw", "app", facade=object())
    connector.connection = _FailingConnection(RuntimeError("boom"))

    result = connector.get_schemas()

    assert result == []
