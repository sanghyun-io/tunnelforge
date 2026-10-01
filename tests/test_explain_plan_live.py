"""Opt-in live check of `query.explain` against disposable MySQL 8.0/8.4 and PostgreSQL 13/18 (TF-STATUS-133).

Skipped unless TF_EXPLAIN_LIVE=1. Servers (names tf-test-a-plan-*):

    docker run -d --name tf-test-a-plan-my80 -p 127.0.0.1:23380:3306 -e MYSQL_ROOT_PASSWORD=tfpass -e MYSQL_DATABASE=tfdb --tmpfs /var/lib/mysql mysql:8.0
    docker run -d --name tf-test-a-plan-my84 -p 127.0.0.1:23384:3306 ... mysql:8.4
    docker run -d --name tf-test-a-plan-pg13 -p 127.0.0.1:25413:5432 -e POSTGRES_PASSWORD=tfpass --tmpfs /var/lib/postgresql/data postgres:13
    docker run -d --name tf-test-a-plan-pg18 -p 127.0.0.1:25418:5432 ... --tmpfs /var/lib/postgresql postgres:18.4

    TF_EXPLAIN_LIVE=1 TF_TLS_TEST_CORE=<tunnelforge-core exe> pytest tests/test_explain_plan_live.py -s
"""
import os

import pytest

from src.core.db_core_client import DbCoreServiceClient, DbCoreServiceError, db_core_executable
from src.core.db_core_facade import DbCoreFacade, DbEndpoint
from src.core.explain_plan import FLAG_FULL_SCAN, FLAG_SEQ_SCAN, explain_on_connection

pytestmark = pytest.mark.skipif(os.environ.get("TF_EXPLAIN_LIVE") != "1", reason="TF_EXPLAIN_LIVE not set")

SERVERS = [
    ("mysql", "mysql 8.0", 23380, "root", "tfdb"),
    ("mysql", "mysql 8.4", 23384, "root", "tfdb"),
    ("postgresql", "postgresql 13", 25413, "postgres", "postgres"),
    ("postgresql", "postgresql 18", 25418, "postgres", "postgres"),
]


def _facade():
    return DbCoreFacade(DbCoreServiceClient(executable=os.environ.get("TF_TLS_TEST_CORE") or db_core_executable()))


def _run(facade, connection_id, *statements):
    for sql in statements:
        result = facade.client.request("query.execute", {"connection_id": connection_id, "sql": sql})
        assert result.get("success"), (sql, result)


def _count(facade, connection_id):
    result = facade.client.request("query.execute", {"connection_id": connection_id, "sql": "SELECT COUNT(*) AS n FROM tf_plan"})
    return int(str(result["rows"][0]["n"]))


@pytest.mark.parametrize("engine,label,port,user,database", SERVERS, ids=[s[1] for s in SERVERS])
def test_explain_plan_analyze_and_write_refusal(engine, label, port, user, database):
    facade = _facade()
    try:
        connection_id = facade.open_connection(DbEndpoint(engine, "127.0.0.1", port, user, "tfpass", database))
        _run(facade, connection_id, "DROP TABLE IF EXISTS tf_plan", "CREATE TABLE tf_plan (id INT PRIMARY KEY, grp INT, name VARCHAR(20))")
        _run(facade, connection_id, "INSERT INTO tf_plan VALUES " + ",".join(f"({i},{i % 5},'n{i}')" for i in range(1, 201)))
        if engine == "postgresql":
            _run(facade, connection_id, "ANALYZE tf_plan")

        # 1. plain EXPLAIN: parsed tree, raw document preserved, nothing executed
        plan = explain_on_connection(facade, connection_id, "SELECT * FROM tf_plan WHERE name = 'n7' ORDER BY grp")
        assert plan.root is not None and not plan.analyze and plan.raw
        flags = {flag for node in plan.root.walk() for flag in node.flags}
        assert FLAG_FULL_SCAN in flags or FLAG_SEQ_SCAN in flags, (label, plan.raw[:300])
        print(f"PASS {label}: plain EXPLAIN parsed, flags={sorted(flags)}")

        # 2. a data-changing statement can be explained but is not executed
        before = _count(facade, connection_id)
        dml = explain_on_connection(facade, connection_id, "DELETE FROM tf_plan WHERE id > 10")
        assert dml.root is not None and _count(facade, connection_id) == before == 200
        print(f"PASS {label}: EXPLAIN DELETE did not delete (rows still 200)")

        # 3. ANALYZE of a read-only query really executes and reports actuals
        analyzed = explain_on_connection(facade, connection_id, "SELECT grp, COUNT(*) FROM tf_plan GROUP BY grp")
        assert not analyzed.analyze
        actual = explain_on_connection(facade, connection_id, "SELECT grp, COUNT(*) FROM tf_plan GROUP BY grp", analyze=True)
        assert actual.analyze and actual.warnings
        assert any(node.rows_actual is not None for node in actual.root.walk()), actual.raw[:300]
        print(f"PASS {label}: ANALYZE returned actual rows/time (format={actual.format})")

        # 4. ANALYZE of writes / locking reads is refused before reaching the server
        for bad in ("DELETE FROM tf_plan", "UPDATE tf_plan SET grp = 0", "INSERT INTO tf_plan VALUES (999,1,'x')",
                    "SELECT * FROM tf_plan FOR UPDATE", "CREATE TABLE tf_plan_x (a INT)"):
            with pytest.raises(DbCoreServiceError) as info:
                explain_on_connection(facade, connection_id, bad, analyze=True)
            assert info.value.error_code == "explain_refused", (bad, info.value)
        assert _count(facade, connection_id) == 200
        print(f"PASS {label}: ANALYZE refused for DELETE/UPDATE/INSERT/FOR UPDATE/DDL, data unchanged")

        # 5. read-only session keeps working for EXPLAIN and ANALYZE of SELECT
        ro_endpoint = DbEndpoint(engine, "127.0.0.1", port, user, "tfpass", database)
        payload = ro_endpoint.to_payload()
        payload["read_only"] = True
        ro = facade.client.request("connection.open", {"connection": payload})
        assert ro.get("success"), ro
        ro_id = ro["connection_id"]
        assert explain_on_connection(facade, ro_id, "SELECT * FROM tf_plan", analyze=True).root is not None
        print(f"PASS {label}: read-only session explains and analyzes SELECT")
        _run(facade, connection_id, "DROP TABLE IF EXISTS tf_plan")
    finally:
        facade.client.shutdown()


@pytest.mark.parametrize("engine,label,port,user,database", SERVERS[1::2], ids=[s[1] for s in SERVERS[1::2]])
def test_running_analyze_can_be_cancelled_on_the_server(engine, label, port, user, database):
    import threading
    import time

    facade = _facade()
    try:
        connection_id = facade.open_connection(DbEndpoint(engine, "127.0.0.1", port, user, "tfpass", database))
        sleeper = "SELECT SLEEP(30)" if engine == "mysql" else "SELECT pg_sleep(30)"
        outcome = {}

        def run():
            try:
                explain_on_connection(facade, connection_id, sleeper, analyze=True, job_id="plan-cancel-1")
                outcome["ok"] = True
            except DbCoreServiceError as exc:
                outcome["code"] = exc.error_code

        thread = threading.Thread(target=run)
        started = time.monotonic()
        thread.start()
        time.sleep(1.5)
        facade.cancel_query("plan-cancel-1")
        thread.join(10)
        assert not thread.is_alive() and outcome.get("code") == "query_cancelled", outcome
        assert time.monotonic() - started < 15
        # the session is still usable afterwards
        assert facade.client.request("query.execute", {"connection_id": connection_id, "sql": "SELECT 1 AS one"}).get("success")
        print(f"PASS {label}: running EXPLAIN ANALYZE cancelled on the server (query_cancelled), session still usable")
    finally:
        facade.client.shutdown()
