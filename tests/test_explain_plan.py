"""TF-STATUS-133: execution plan parsing (real outputs captured from MySQL 8.0 and PostgreSQL 18)."""
import json
from pathlib import Path

import pytest

from src.core import explain_plan as ep

FIXTURES = Path(__file__).parent / "fixtures" / "explain"


def _load(name):
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    meta = data["meta"]
    return ep.parse_explain_rows(meta["engine"], meta["analyze"], meta["format"], data["rows"])


def _flags(result):
    return {flag for node in result.root.walk() for flag in node.flags}


def test_mysql_json_plan_is_a_tree_with_facts_only():
    result = _load("mysql80_plain")
    assert result.engine == "mysql" and not result.analyze and result.format == "json"
    titles = [node.title for node in result.root.walk()]
    assert any("테이블 a" in t for t in titles) and any("테이블 b" in t for t in titles)
    assert {ep.FLAG_FILESORT, ep.FLAG_TEMPORARY, ep.FLAG_FULL_SCAN} <= _flags(result)
    scan = next(n for n in result.root.walk() if n.title == "테이블 a")
    assert scan.rows_estimate == 300 and scan.cost == pytest.approx(30.25)
    assert result.warnings == []  # plain EXPLAIN executes nothing
    assert json.loads(result.raw)["query_block"]  # raw document preserved


def test_mysql_explain_analyze_tree_text():
    result = _load("mysql80_analyze")
    assert result.analyze and result.format == "tree" and result.raw.startswith("-> Sort")
    assert ep.ANALYZE_WARNING in result.warnings
    root = result.root
    assert root.title == "Sort: n DESC" and ep.FLAG_FILESORT in root.flags
    assert root.rows_actual == 7 and root.time_ms == pytest.approx(0.374) and root.loops == 1
    nodes = {n.title: n for n in root.walk()}
    scan = nodes["Table scan on a"]
    assert ep.FLAG_FULL_SCAN in scan.flags and scan.rows_estimate == 300 and scan.rows_actual == 300
    lookup = next(n for n in root.walk() if n.title.startswith("Covering index lookup"))
    assert lookup.loops == 111 and lookup.rows_actual == 3
    # a scan of the temporary table is not a full table scan of user data
    temp_scan = nodes["Table scan on <temporary>"]
    assert ep.FLAG_FULL_SCAN not in temp_scan.flags and ep.FLAG_TEMPORARY in temp_scan.flags
    depth = {n.title: d for n, d in _with_depth(root)}
    assert depth["Table scan on a"] == 5 and depth["Sort: n DESC"] == 0


def _with_depth(node, depth=0):
    yield node, depth
    for child in node.children:
        yield from _with_depth(child, depth + 1)


def test_postgres_plain_and_analyze():
    plain, analyzed = _load("pg18_plain"), _load("pg18_analyze")
    assert plain.root.title == "Sort" and not plain.analyze and plain.root.rows_actual is None
    assert ep.FLAG_SEQ_SCAN in _flags(plain)
    assert any(n.title == "Seq Scan on tf_a" and n.facts["Relation Name"] == "tf_a" for n in plain.root.walk())

    assert analyzed.analyze and ep.ANALYZE_WARNING in analyzed.warnings
    assert analyzed.summary["Execution Time"] == pytest.approx(0.22)
    join = next(n for n in analyzed.root.walk() if n.title == "Hash Join")
    assert join.rows_estimate == 336 and join.rows_actual == 333 and join.loops == 1
    assert isinstance(json.loads(analyzed.raw), list)  # raw JSON document kept


def test_postgres_disk_sort_hash_batches_and_filter_facts():
    plan = {"Plan": {
        "Node Type": "Sort", "Total Cost": 10, "Plan Rows": 5, "Sort Method": "external merge",
        "Sort Space Type": "Disk", "Sort Space Used": 4096,
        "Plans": [{"Node Type": "Hash", "Hash Batches": 4, "Total Cost": 1, "Plan Rows": 1,
                   "Plans": [{"Node Type": "Seq Scan", "Relation Name": "t", "Rows Removed by Filter": 9000,
                              "Filter": "(a > 1)", "Total Cost": 1, "Plan Rows": 1}]}],
    }}
    result = ep.parse_explain_rows("postgresql", False, "json", [{"QUERY PLAN": json.dumps([plan])}])
    nodes = {n.title: n for n in result.root.walk()}
    assert ep.FLAG_DISK_SORT in nodes["Sort"].flags and ep.FLAG_DISK_HASH in nodes["Hash"].flags
    scan = nodes["Seq Scan on t"]
    assert scan.facts["Filter"] == "(a > 1)" and "9000" in scan.detail


def test_mysql_json_union_subquery_and_index_access():
    document = {"query_block": {"union_result": {"using_temporary_table": True, "query_specifications": [
        {"query_block": {"select_id": 1, "table": {"table_name": "a", "access_type": "index", "key": "ix",
                                                    "rows_examined_per_scan": 10}}},
        {"query_block": {"select_id": 2, "table": {"table_name": "b", "access_type": "ref", "key": "ix_b"}}},
    ]}}}
    result = ep.parse_explain_rows("mysql", False, "json", [{"EXPLAIN": json.dumps(document)}])
    tables = [n for n in result.root.walk() if n.title.startswith("테이블")]
    assert [t.title for t in tables] == ["테이블 a", "테이블 b"]
    assert ep.FLAG_INDEX_SCAN in tables[0].flags and not tables[1].flags
    assert ep.FLAG_TEMPORARY in _flags(result)


def test_unrecognised_documents_raise_clear_errors():
    with pytest.raises(ValueError):
        ep.parse_explain_rows("mysql", False, "json", [{"EXPLAIN": "{}"}])
    with pytest.raises(ValueError):
        ep.parse_explain_rows("postgresql", False, "json", [{"QUERY PLAN": "[]"}])
    with pytest.raises(ValueError):
        ep.parse_explain_rows("mysql", True, "tree", [{"EXPLAIN": "no arrows here"}])
    with pytest.raises(ValueError):
        ep.parse_explain_rows("mysql", False, "json", [])


def test_never_executed_tree_nodes_are_reported_as_facts():
    text = "-> Limit: 1 row(s)  (cost=1 rows=1) (actual time=0.1..0.1 rows=1 loops=1)\n    -> Table scan on t  (never executed)\n"
    result = ep.parse_explain_rows("mysql", True, "tree", [{"EXPLAIN": text}])
    child = result.root.children[0]
    assert child.detail == "실행되지 않음" and child.rows_actual is None


class _Client:
    def __init__(self, result):
        self.result, self.sent = result, []

    def request(self, command, payload=None):
        self.sent.append((command, payload))
        return self.result


def test_explain_on_connection_sends_explicit_analyze_flag_and_maps_refusals():
    rows = [{"EXPLAIN": json.dumps({"query_block": {"select_id": 1}})}]
    client = _Client({"success": True, "rows": rows, "explain": {"engine": "mysql", "analyze": False, "format": "json"}})
    facade = type("F", (), {"client": client})()
    result = ep.explain_on_connection(facade, "c1", "SELECT 1", job_id="j1", timeout_ms=500)
    assert client.sent == [("query.explain", {"connection_id": "c1", "sql": "SELECT 1", "analyze": False,
                                              "job_id": "j1", "timeout_ms": 500})]
    assert result.engine == "mysql" and not result.analyze

    refused = _Client({"success": False, "error_code": "explain_refused", "message": "no"})
    with pytest.raises(ep.DbCoreServiceError) as info:
        ep.explain_on_connection(type("F", (), {"client": refused})(), "c1", "DELETE FROM t", analyze=True)
    assert info.value.error_code == "explain_refused"
    assert refused.sent[0][1]["analyze"] is True
