import io
import json
import threading
import time

import src.core.db_core_service as db_core_service
from src.core.db_core_service import (
    DbCoreFacade,
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


class FakeProcess:
    def __init__(self, stdout_lines, stderr_text=""):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("\n".join(stdout_lines) + "\n")
        self.stderr = io.StringIO(stderr_text)
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


def test_rust_db_cursor_rowcount_uses_core_rows_affected_for_dml():
    process = FakeProcess([
        '{"event":"result","command":"query.execute","success":true,"rows":[],"rows_affected":7}',
    ])
    client = DbCoreServiceClient(
        executable="fake-core",
        popen_factory=lambda *args, **kwargs: process,
    )
    facade = DbCoreFacade(client)
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, facade, "conn-1")

    with connection.cursor() as cursor:
        rowcount = cursor.execute("UPDATE users SET active = 0")

    assert rowcount == 7
    assert cursor.rowcount == 7
    assert cursor.fetchall() == []


def test_rust_db_cursor_rowcount_uses_call_local_rows_affected():
    class FakeFacade:
        last_rows_affected = 999

        def execute_on_connection(self, connection_id, query, params=None):
            assert connection_id == "conn-1"
            return []

        def execute_on_connection_result(self, connection_id, query, params=None):
            assert connection_id == "conn-1"
            return {"rows": [], "rows_affected": 7}

    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, FakeFacade(), "conn-1")

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
    connection = RustDbConnection(endpoint, facade, "conn-1")

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

    result = facade.execute_on_connection_result("conn-1", "SELECT id FROM users WHERE 1=0")

    assert result["columns"] == ["id"]
    assert result["rows"] == []


def test_rust_db_cursor_empty_select_reports_column_metadata():
    class FakeFacade:
        def execute_on_connection_result(self, connection_id, query, params=None):
            return {"rows": [], "columns": ["id", "name"], "rows_affected": 0}

    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, FakeFacade(), "conn-1")

    with connection.cursor() as cursor:
        cursor.execute("-- check\nSELECT id, name FROM users WHERE 1=0")
        assert cursor.description == [("id",), ("name",)]
        assert cursor.rowcount == 0
        assert cursor.fetchall() == []


def test_rust_db_cursor_comment_prefixed_select_keeps_empty_description_not_none():
    class FakeFacade:
        def execute_on_connection_result(self, connection_id, query, params=None):
            return {"rows": [], "columns": [], "rows_affected": 0}

    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, FakeFacade(), "conn-1")

    with connection.cursor() as cursor:
        cursor.execute("-- comment\nSELECT * FROM users WHERE 1=0")
        assert cursor.description == []
        assert cursor.description is not None


def test_rust_db_cursor_dml_has_no_description():
    class FakeFacade:
        def execute_on_connection_result(self, connection_id, query, params=None):
            return {"rows": [], "columns": [], "rows_affected": 7}

    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, FakeFacade(), "conn-1")

    with connection.cursor() as cursor:
        rowcount = cursor.execute("UPDATE users SET active = 0")

    assert cursor.description is None
    assert rowcount == 7
    assert cursor.rowcount == 7


def test_rust_db_connection_ping_issues_exactly_select_1():
    class FakeFacade:
        def __init__(self):
            self.calls = []

        def execute_on_connection(self, connection_id, query, params=None):
            self.calls.append((connection_id, query))
            return []

    facade = FakeFacade()
    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, facade, "conn-1")

    connection.ping()

    assert facade.calls == [("conn-1", "SELECT 1")]


def test_rust_db_connection_ping_closes_on_failure_and_closed_ping_raises():
    class FailingFacade:
        def execute_on_connection(self, connection_id, query, params=None):
            raise DbCoreServiceError("connection lost")

    endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "root", "pw", "app")
    connection = RustDbConnection(endpoint, FailingFacade(), "conn-1")

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


def test_bind_sql_params_and_sql_literal_helpers_are_removed():
    assert not hasattr(db_core_service, "bind_sql_params")
    assert not hasattr(db_core_service, "sql_literal")


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
        def readline(self):
            return ""

        def read(self):
            raise AssertionError("stderr.read() must not be called; the drained tail must be used instead")

    class EOFProcess:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("")
            self.stderr = ExplodingStderr()
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

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


class _BlockingStdout:
    """Stdout stub whose readline() blocks until `release_event` is set."""

    def __init__(self, lines, release_event):
        self._lines = list(lines)
        self._release_event = release_event

    def readline(self):
        self._release_event.wait(timeout=5)
        if self._lines:
            return self._lines.pop(0)
        return ""


class _BlockingProcess:
    def __init__(self, lines, release_event):
        self.stdin = io.StringIO()
        self.stdout = _BlockingStdout(lines, release_event)
        self.stderr = io.StringIO()
        self.terminated = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self):
        self.terminated = True


def test_shutdown_and_concurrent_request_are_serialized_by_lock():
    release_shutdown_response = threading.Event()
    shutdown_process = _BlockingProcess(
        ['{"event":"result","command":"service.shutdown","success":true}\n'],
        release_shutdown_response,
    )
    second_process = FakeProcess([
        '{"event":"result","command":"service.hello","success":true}',
    ])
    popen_calls = []

    def popen_factory(*args, **kwargs):
        popen_calls.append(args)
        return shutdown_process if len(popen_calls) == 1 else second_process

    client = DbCoreServiceClient(executable="fake-core", popen_factory=popen_factory)
    client.start()

    shutdown_thread = threading.Thread(target=client.shutdown)
    shutdown_thread.start()
    time.sleep(0.05)  # let shutdown() acquire `_lock` and block on the fake stdout

    results = {}
    errors = []

    def do_request():
        try:
            results["value"] = client.request("service.hello")
        except Exception as exc:
            errors.append(exc)

    request_thread = threading.Thread(target=do_request)
    request_thread.start()
    time.sleep(0.05)
    assert request_thread.is_alive(), "request() should be waiting behind `_lock`"

    release_shutdown_response.set()
    shutdown_thread.join(timeout=5)
    request_thread.join(timeout=5)

    assert not errors
    assert results["value"]["success"] is True
    assert client._process is second_process
    assert len(popen_calls) == 2
