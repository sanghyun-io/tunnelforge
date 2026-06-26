from src.core.sql_statement_parser import read_dollar_quote
from src.ui.workers.test_worker import SQLExecutionWorker


class FakeCursor:
    def __init__(self, executed):
        self.executed = executed
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def execute(self, statement):
        self.executed.append(statement)
        if statement.lower().startswith("select"):
            self._rows = [{"value": 1}]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, executed):
        self.executed = executed

    def cursor(self):
        return FakeCursor(self.executed)


class FakeConnector:
    def __init__(self, executed):
        self.connection = FakeConnection(executed)
        self.disconnected = False

    def connect(self):
        return True, "ok"

    def disconnect(self):
        self.disconnected = True


def test_sql_execution_worker_uses_rust_core_connector(monkeypatch, tmp_path):
    sql_file = tmp_path / "script.sql"
    sql_file.write_text("SELECT 1; INSERT INTO logs VALUES ('a;b');", encoding="utf-8")
    executed = []
    created = {}
    connector = FakeConnector(executed)

    def fake_create(engine, host, port, user, password, database=None, schema=""):
        created.update(
            {
                "engine": engine,
                "host": host,
                "port": port,
                "user": user,
                "database": database,
                "schema": schema,
            }
        )
        return connector

    monkeypatch.setattr("src.ui.workers.test_worker.create_rust_db_connector", fake_create)

    outputs = []
    finished = []
    worker = SQLExecutionWorker(
        str(sql_file),
        "127.0.0.1",
        15432,
        "pg_user",
        "pw",
        "analytics",
        db_engine="postgresql",
        schema="public",
    )
    worker.output.connect(outputs.append)
    worker.finished.connect(lambda success, message: finished.append((success, message)))

    worker.run()

    assert created == {
        "engine": "postgresql",
        "host": "127.0.0.1",
        "port": 15432,
        "user": "pg_user",
        "database": "analytics",
        "schema": "public",
    }
    assert executed == ["SELECT 1", "INSERT INTO logs VALUES ('a;b')"]
    assert outputs == ["value\n1"]
    assert finished and finished[0][0] is True
    assert connector.disconnected is True


def test_sql_statement_parser_preserves_semicolons_in_literals_and_comments():
    sql = """
    -- comment; ignored
    SELECT 'a;b';
    /* block; comment */
    UPDATE logs SET message = "x;y";
    """

    assert SQLExecutionWorker._parse_sql_statements(sql) == [
        "-- comment; ignored\n    SELECT 'a;b'",
        '/* block; comment */\n    UPDATE logs SET message = "x;y"',
    ]


def test_sql_statement_parser_supports_client_delimiters():
    sql = """
    DELIMITER //
    CREATE PROCEDURE p()
    BEGIN
        SELECT 'a;b';
    END//
    DELIMITER ;
    SELECT 1;
    """

    assert SQLExecutionWorker._parse_sql_statements(sql) == [
        "CREATE PROCEDURE p()\n    BEGIN\n        SELECT 'a;b';\n    END",
        "SELECT 1",
    ]


def test_sql_statement_parser_supports_postgresql_dollar_quotes():
    sql = """
    CREATE FUNCTION f() RETURNS void AS $body$
    BEGIN
        RAISE NOTICE 'a;b';
    END
    $body$ LANGUAGE plpgsql;
    SELECT 1;
    """

    assert SQLExecutionWorker._parse_sql_statements(sql) == [
        "CREATE FUNCTION f() RETURNS void AS $body$\n    BEGIN\n        RAISE NOTICE 'a;b';\n    END\n    $body$ LANGUAGE plpgsql",
        "SELECT 1",
    ]


def test_dollar_quote_reader_fails_closed_for_out_of_range_starts():
    sql = "$body$"

    assert read_dollar_quote("", 0) == ""
    assert read_dollar_quote(sql, -1) == ""
    assert read_dollar_quote(sql, len(sql)) == ""
    assert SQLExecutionWorker._read_dollar_quote("", 0) == ""
    assert SQLExecutionWorker._read_dollar_quote(sql, -1) == ""
    assert SQLExecutionWorker._read_dollar_quote(sql, len(sql)) == ""


def test_dollar_quote_reader_fails_closed_for_none_sql_text():
    assert read_dollar_quote(None, 0) == ""
    assert SQLExecutionWorker._read_dollar_quote(None, 0) == ""
