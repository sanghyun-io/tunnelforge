"""실행 계획(EXPLAIN) 조회와 파싱 (TF-STATUS-133).

Core 의 `query.explain` 결과(행)를 엔진 공통 트리(`PlanNode`)로 바꾼다. 원문(JSON/텍스트)은
`ExplainResult.raw` 에 그대로 보존한다. 이 모듈은 사실만 표시한다: 인덱스 추가 같은
자동 권고는 하지 않는다(잘못된 권고 방지).
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.db_core_client import DbCoreServiceError

FLAG_SEQ_SCAN = "순차 스캔"
FLAG_FULL_SCAN = "풀 테이블 스캔"
FLAG_INDEX_SCAN = "인덱스 풀 스캔"
FLAG_FILESORT = "filesort"
FLAG_TEMPORARY = "임시 테이블"
FLAG_DISK_SORT = "디스크 정렬"
FLAG_DISK_HASH = "디스크 해시"

ANALYZE_WARNING = "ANALYZE 는 쿼리를 실제로 실행합니다. 실행 시간과 서버 부하가 발생하고, 함수 등의 부수 효과가 있을 수 있습니다."


@dataclass
class PlanNode:
    title: str
    detail: str = ""
    cost: Optional[float] = None
    rows_estimate: Optional[float] = None
    rows_actual: Optional[float] = None
    time_ms: Optional[float] = None
    loops: Optional[int] = None
    flags: List[str] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)
    children: List["PlanNode"] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class ExplainResult:
    engine: str
    analyze: bool
    format: str  # "json" | "tree"
    raw: str
    root: Optional[PlanNode]
    summary: Dict[str, Any] = field(default_factory=dict)  # 예: PostgreSQL Planning/Execution Time
    warnings: List[str] = field(default_factory=list)

    def flagged_nodes(self) -> List[PlanNode]:
        return [node for node in self.root.walk() if node.flags] if self.root else []


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- PostgreSQL (FORMAT JSON) -------------------------------------------------------------

_PG_FACT_KEYS = (
    "Relation Name", "Alias", "Index Name", "Join Type", "Filter", "Index Cond", "Hash Cond", "Join Filter",
    "Rows Removed by Filter", "Sort Key", "Sort Method", "Sort Space Used", "Sort Space Type",
    "Hash Batches", "Group Key", "Parent Relationship", "Workers Planned", "Workers Launched",
    "Heap Fetches", "Shared Hit Blocks", "Shared Read Blocks", "Shared Dirtied Blocks",
    "Shared Written Blocks", "Temp Read Blocks", "Temp Written Blocks", "Plan Width",
)


def _pg_node(plan: Dict[str, Any]) -> PlanNode:
    node_type = str(plan.get("Node Type", "?"))
    relation = plan.get("Relation Name")
    index = plan.get("Index Name")
    title = node_type
    if relation:
        title += f" on {relation}"
    if index:
        title += f" using {index}"
    node = PlanNode(
        title=title,
        cost=_num(plan.get("Total Cost")),
        rows_estimate=_num(plan.get("Plan Rows")),
        rows_actual=_num(plan.get("Actual Rows")),
        time_ms=_num(plan.get("Actual Total Time")),
        loops=int(plan["Actual Loops"]) if plan.get("Actual Loops") is not None else None,
        facts={key: plan[key] for key in _PG_FACT_KEYS if key in plan},
    )
    if node_type == "Seq Scan" or node_type.endswith("Seq Scan"):
        node.flags.append(FLAG_SEQ_SCAN)
    if plan.get("Sort Space Type") == "Disk":
        node.flags.append(FLAG_DISK_SORT)
    batches = plan.get("Hash Batches")
    if isinstance(batches, (int, float)) and batches > 1:
        node.flags.append(FLAG_DISK_HASH)
    removed = plan.get("Rows Removed by Filter")
    if removed:
        node.detail = f"필터로 제외된 행 {removed}"
    for child in plan.get("Plans") or []:
        if isinstance(child, dict):
            node.children.append(_pg_node(child))
    return node


def _parse_pg(analyze: bool, document: Any, raw: str) -> ExplainResult:
    entry = document[0] if isinstance(document, list) and document else document
    if not isinstance(entry, dict) or not isinstance(entry.get("Plan"), dict):
        raise ValueError("PostgreSQL 실행 계획 JSON 형식을 인식할 수 없습니다")
    summary = {key: entry[key] for key in ("Planning Time", "Execution Time") if key in entry}
    return ExplainResult("postgresql", analyze, "json", raw, _pg_node(entry["Plan"]), summary)


# --- MySQL (FORMAT=JSON) ------------------------------------------------------------------

_MYSQL_BLOCK_KEYS = (
    "ordering_operation", "grouping_operation", "duplicates_removal", "windowing", "buffer_result",
    "union_result", "materialized_from_subquery", "query_block",
)
_MYSQL_LIST_KEYS = ("nested_loop", "query_specifications", "attached_subqueries", "optimized_away_subqueries",
                    "select_list_subqueries")
_MYSQL_TABLE_FACTS = ("access_type", "possible_keys", "key", "key_length", "ref", "attached_condition",
                      "used_columns", "using_index", "index_condition", "using_join_buffer", "filtered")


def _mysql_table(table: Dict[str, Any]) -> PlanNode:
    access = str(table.get("access_type", ""))
    name = table.get("table_name") or table.get("message") or "?"
    node = PlanNode(
        title=f"테이블 {name}",
        detail=f"access: {access}" + (f", key: {table['key']}" if table.get("key") else ""),
        rows_estimate=_num(table.get("rows_examined_per_scan", table.get("rows_produced_per_join"))),
        facts={key: table[key] for key in _MYSQL_TABLE_FACTS if key in table},
    )
    cost = table.get("cost_info") or {}
    node.cost = _num(cost.get("prefix_cost", cost.get("read_cost")))
    if access == "ALL":
        node.flags.append(FLAG_FULL_SCAN)
    elif access == "index":
        node.flags.append(FLAG_INDEX_SCAN)
    if table.get("using_filesort"):
        node.flags.append(FLAG_FILESORT)
    for key in _MYSQL_BLOCK_KEYS + _MYSQL_LIST_KEYS:
        if key in table:
            _mysql_attach(node, key, table[key])
    return node


def _mysql_block(name: str, block: Dict[str, Any]) -> PlanNode:
    node = PlanNode(title=name.replace("_", " "))
    cost = block.get("cost_info") or {}
    node.cost = _num(cost.get("query_cost"))
    if block.get("using_filesort"):
        node.flags.append(FLAG_FILESORT)
    if block.get("using_temporary_table"):
        node.flags.append(FLAG_TEMPORARY)
    node.facts = {key: value for key, value in block.items()
                  if isinstance(value, (str, int, float, bool)) and key not in ("using_filesort", "using_temporary_table")}
    for key, value in block.items():
        if key == "table" and isinstance(value, dict):
            node.children.append(_mysql_table(value))
        elif key in _MYSQL_BLOCK_KEYS + _MYSQL_LIST_KEYS:
            _mysql_attach(node, key, value)
    return node


def _mysql_attach(parent: PlanNode, key: str, value: Any) -> None:
    if isinstance(value, dict):
        parent.children.append(_mysql_block(key, value))
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("table"), dict):
                parent.children.append(_mysql_table(item["table"]))
            elif isinstance(item.get("query_block"), dict):
                parent.children.append(_mysql_block("query_block", item["query_block"]))
            else:
                parent.children.append(_mysql_block(key, item))


def _parse_mysql_json(document: Any, raw: str) -> ExplainResult:
    if not isinstance(document, dict) or not isinstance(document.get("query_block"), dict):
        raise ValueError("MySQL 실행 계획 JSON 형식을 인식할 수 없습니다")
    return ExplainResult("mysql", False, "json", raw, _mysql_block("query_block", document["query_block"]))


# --- MySQL EXPLAIN ANALYZE (tree text) -----------------------------------------------------

_TREE_COST = re.compile(r"\(cost=([\d.eE+-]+) rows=([\d.eE+-]+)\)")
_TREE_ACTUAL = re.compile(r"\(actual time=([\d.eE+-]+)\.\.([\d.eE+-]+) rows=([\d.eE+-]+) loops=(\d+)\)")
_TREE_NEVER = "(never executed)"


def _tree_node(text: str) -> PlanNode:
    cost = _TREE_COST.search(text)
    actual = _TREE_ACTUAL.search(text)
    title = text
    for pattern in (_TREE_COST, _TREE_ACTUAL):
        title = pattern.sub("", title)
    title = title.replace(_TREE_NEVER, "").strip()
    node = PlanNode(title=title)
    if cost:
        node.cost, node.rows_estimate = _num(cost.group(1)), _num(cost.group(2))
    if actual:
        node.time_ms, node.rows_actual, node.loops = _num(actual.group(2)), _num(actual.group(3)), int(actual.group(4))
    if _TREE_NEVER in text:
        node.detail = "실행되지 않음"
    lowered = title.lower()
    if lowered.startswith("table scan") and "<temporary>" not in lowered:
        node.flags.append(FLAG_FULL_SCAN)
    elif lowered.startswith("index scan"):
        node.flags.append(FLAG_INDEX_SCAN)
    if lowered.startswith("sort"):
        node.flags.append(FLAG_FILESORT)
    if "temporary table" in lowered or "<temporary>" in lowered:
        node.flags.append(FLAG_TEMPORARY)
    return node


def _parse_mysql_tree(text: str) -> ExplainResult:
    root: Optional[PlanNode] = None
    stack: List[tuple] = []  # (indent, node)
    for line in text.splitlines():
        marker = line.find("-> ")
        if marker < 0:
            continue
        node = _tree_node(line[marker + 3:])
        while stack and stack[-1][0] >= marker:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        elif root is None:
            root = node
        else:  # more than one top-level line: keep them under a synthetic parent
            wrapper = PlanNode(title="plan", children=[root, node])
            root = wrapper
        stack.append((marker, node))
    if root is None:
        raise ValueError("MySQL EXPLAIN ANALYZE 출력을 인식할 수 없습니다")
    return ExplainResult("mysql", True, "tree", text, root)


# --- entry points -------------------------------------------------------------------------

def parse_explain_rows(engine: str, analyze: bool, fmt: str, rows: List[Dict[str, Any]]) -> ExplainResult:
    """`query.explain` 결과 행 -> ExplainResult. 원문은 그대로 보존한다."""
    if not rows:
        raise ValueError("실행 계획 결과가 비어 있습니다")
    value = next(iter(rows[0].values()))
    if engine == "postgresql":
        document = json.loads(value) if isinstance(value, str) else value
        raw = value if isinstance(value, str) else json.dumps(document, ensure_ascii=False, indent=2)
        result = _parse_pg(analyze, document, raw)
    elif fmt == "tree":
        result = _parse_mysql_tree("\n".join(str(next(iter(row.values()))) for row in rows))
    else:
        raw = str(value)
        result = _parse_mysql_json(json.loads(raw), raw)
    if analyze:
        result.warnings.append(ANALYZE_WARNING)
    return result


def explain_on_connection(facade, connection_id: str, sql: str, analyze: bool = False,
                          job_id: Optional[str] = None, timeout_ms: Optional[int] = None) -> ExplainResult:
    """세션(connection_id)에서 실행 계획을 조회한다. `analyze=True` 는 쿼리를 실제로 실행한다."""
    payload: Dict[str, Any] = {"connection_id": connection_id, "sql": sql, "analyze": bool(analyze)}
    if job_id:
        payload["job_id"] = job_id
    if timeout_ms:
        payload["timeout_ms"] = int(timeout_ms)
    result = facade.client.request("query.explain", payload)
    if result.get("success") is False:
        raise DbCoreServiceError(str(result.get("message") or "explain failed"),
                                 error_code=result.get("error_code"), payload=result)
    meta = result.get("explain") or {}
    rows = [row for row in (result.get("rows") or []) if isinstance(row, dict)]
    return parse_explain_rows(str(meta.get("engine", "")), bool(meta.get("analyze", analyze)),
                              str(meta.get("format", "json")), rows)
