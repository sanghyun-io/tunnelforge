"""Cross-engine migration protocol support for the tunnelforge-core helper.

The PyQt UI talks to the Rust helper through newline-delimited JSON, and this
module keeps four responsibilities isolated from widgets and worker
orchestration:

1. JSONL wire-format models, parsing, and request building for the
   tunnelforge-core helper protocol.
2. Locating the tunnelforge-core executable across dev/frozen/PATH layouts.
3. Persisting and loading resume-state to/from disk between migration runs.
4. Rendering human-readable text reports from migration result payloads.
"""
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.platform_paths import data_dir


class DatabaseEngine(str, Enum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"


class MigrationDirection(str, Enum):
    MYSQL_TO_POSTGRESQL = "mysql_to_postgresql"
    POSTGRESQL_TO_MYSQL = "postgresql_to_mysql"

    @classmethod
    def from_engines(cls, source: DatabaseEngine, target: DatabaseEngine) -> "MigrationDirection":
        if source == DatabaseEngine.MYSQL and target == DatabaseEngine.POSTGRESQL:
            return cls.MYSQL_TO_POSTGRESQL
        if source == DatabaseEngine.POSTGRESQL and target == DatabaseEngine.MYSQL:
            return cls.POSTGRESQL_TO_MYSQL
        raise ValueError(f"Unsupported migration direction: {source.value} -> {target.value}")


DEFAULT_MYSQL_PORT = 3306
DEFAULT_POSTGRESQL_PORT = 5432
DEFAULT_POSTGRESQL_SCHEMA = "public"
DEFAULT_POSTGRESQL_DATABASE = "postgres"


@dataclass
class ConnectionEndpointInput:
    engine: DatabaseEngine
    host: str
    port: int
    user: str
    password: str
    database: str
    schema: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "engine": self.engine.value,
            "host": self.host,
            "port": int(self.port),
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "schema": self.schema,
        }


@dataclass
class MigrationIssue:
    severity: str
    location: str
    message: str
    suggestion: str = ""
    blocking: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MigrationIssue":
        return cls(
            severity=str(data.get("severity", "info")),
            location=str(data.get("location", "")),
            message=str(data.get("message", "")),
            suggestion=str(data.get("suggestion", "")),
            blocking=bool(data.get("blocking", False)),
        )


@dataclass
class HelperEvent:
    event: str
    raw_line: str = ""
    request_id: Optional[str] = None
    phase: Optional[str] = None
    message: str = ""
    table: Optional[str] = None
    status: Optional[str] = None
    rows: Optional[int] = None
    total: Optional[int] = None
    issue: Optional[MigrationIssue] = None
    success: Optional[bool] = None
    command: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)


class HelperProtocolError(ValueError):
    """Raised when the Rust helper emits malformed JSONL."""


FULL_MIGRATION_WORKFLOW = ("inspect", "preflight", "plan", "migrate", "verify")

MAX_MISMATCHES_DISPLAYED = 50


def next_workflow_command(completed_command: str, success: bool) -> Optional[str]:
    """Return the next command in the full migration workflow."""
    if not success:
        return None
    try:
        index = FULL_MIGRATION_WORKFLOW.index(completed_command)
    except ValueError:
        return None
    next_index = index + 1
    if next_index >= len(FULL_MIGRATION_WORKFLOW):
        return None
    return FULL_MIGRATION_WORKFLOW[next_index]


def build_helper_request(command: str, payload: Dict[str, Any], request_id: Optional[str] = None) -> str:
    """Build one JSONL request line for tunnelforge-core."""
    body = {
        "command": command,
        "payload": payload,
    }
    if request_id:
        body["request_id"] = request_id
    return json.dumps(body, ensure_ascii=False) + "\n"


def parse_helper_event(line: str) -> HelperEvent:
    """Parse one JSONL event emitted by tunnelforge-core."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise HelperProtocolError(f"Invalid helper JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise HelperProtocolError("Helper event must be a JSON object")

    event_type = str(data.get("event", ""))
    if not event_type:
        raise HelperProtocolError("Helper event is missing 'event'")

    issue = None
    if isinstance(data.get("issue"), dict):
        issue = MigrationIssue.from_dict(data["issue"])

    return HelperEvent(
        event=event_type,
        raw_line=line.rstrip(),
        request_id=data.get("request_id"),
        phase=data.get("phase"),
        message=str(data.get("message", "")),
        table=data.get("table"),
        status=data.get("status"),
        rows=data.get("rows"),
        total=data.get("total"),
        issue=issue,
        success=data.get("success"),
        command=data.get("command"),
        payload=data,
    )


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def app_data_dir() -> Path:
    return data_dir()


def _db_core_executable_names(os_name: Optional[str] = None) -> List[str]:
    return ["tunnelforge-core.exe"] if (os_name or os.name) == "nt" else ["tunnelforge-core"]


def _db_core_frozen_candidate_dirs(executable_path: Path) -> List[Path]:
    executable_dir = executable_path.parent
    candidate_dirs = [executable_dir]

    contents_dir = executable_dir.parent
    if executable_dir.name == "MacOS" and contents_dir.name == "Contents":
        candidate_dirs.extend([
            contents_dir / "Frameworks",
            contents_dir / "Resources",
        ])

    candidate_dirs.append(Path.cwd())
    return candidate_dirs


def db_core_executable() -> str:
    """Return the best available Rust DB core executable path."""
    exe_names = _db_core_executable_names()
    candidate_dirs: List[Path] = []

    if hasattr(sys, "_MEIPASS"):
        candidate_dirs.append(Path(sys._MEIPASS))  # type: ignore[attr-defined]
    if getattr(sys, "frozen", False):
        candidate_dirs.extend(_db_core_frozen_candidate_dirs(Path(sys.executable)))

    root = project_root()
    candidate_dirs.extend([
        root,
        root / "migration_core" / "target" / "release",
        root / "migration_core" / "target" / "debug",
    ])

    for directory in candidate_dirs:
        for exe_name in exe_names:
            candidate = directory / exe_name
            if candidate.exists():
                return str(candidate)

    for exe_name in exe_names:
        from_path = shutil.which(exe_name)
        if from_path:
            return from_path

    return str(root / "migration_core" / "target" / "release" / exe_names[0])


def state_key_from_payload(payload: Dict[str, Any]) -> str:
    """Build a stable filename-safe key for a cross-engine migration payload."""
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    schema = payload.get("schema") if isinstance(payload.get("schema"), dict) else {}
    tables = schema.get("tables") if isinstance(schema.get("tables"), list) else []
    table_part = "_".join(
        str(table.get("name", "")) for table in tables[:3] if isinstance(table, dict)
    ) or "all"
    raw = "_".join([
        str(source.get("engine", "source")),
        str(source.get("database", "")),
        str(source.get("schema", "")),
        str(target.get("engine", "target")),
        str(target.get("database", "")),
        str(target.get("schema", "")),
        table_part,
    ])
    return "".join(char if char.isalnum() or char in "_-" else "_" for char in raw)


def cross_engine_state_dir(base_dir: Optional[Path] = None) -> Path:
    state_dir = (base_dir or app_data_dir()) / "cross_engine_migration_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def save_resume_state(key: str, state: Dict[str, Any], base_dir: Optional[Path] = None) -> Path:
    path = cross_engine_state_dir(base_dir) / f"{key}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return path


def load_resume_state(key: str, base_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = cross_engine_state_dir(base_dir) / f"{key}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def make_connection_payload(
    engine: DatabaseEngine,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    schema: str = "",
) -> Dict[str, Any]:
    """Build a serializable endpoint payload for the helper."""
    return ConnectionEndpointInput(
        engine=engine,
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        schema=schema,
    ).to_payload()


def format_schema_summary(schema: Dict, unsupported_objects: List[str]) -> str:
    schema_data: Dict[str, Any] = schema if isinstance(schema, dict) else {}
    raw_tables = schema_data.get("tables")
    tables: List[Any] = raw_tables if isinstance(raw_tables, list) else []
    valid_tables: List[Dict[str, Any]] = [table for table in tables if isinstance(table, dict)]
    table_count = len(valid_tables)
    column_count = 0
    index_count = 0
    foreign_key_count = 0
    for table in valid_tables:
        columns = table.get("columns")
        indexes = table.get("indexes")
        foreign_keys = table.get("foreign_keys")
        column_count += len(columns) if isinstance(columns, list) else 0
        index_count += len(indexes) if isinstance(indexes, list) else 0
        foreign_key_count += len(foreign_keys) if isinstance(foreign_keys, list) else 0
    return (
        f"테이블 {table_count}개, 컬럼 {column_count}개, "
        f"인덱스 {index_count}개, FK {foreign_key_count}개, "
        f"지원 제외 {len(unsupported_objects)}개"
    )


def _plan_tables(payload: Dict) -> List[Dict]:
    raw_plan = payload.get("plan")
    plan: Dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    raw_tables = plan.get("tables")
    tables: List[Any] = raw_tables if isinstance(raw_tables, list) else []
    return [table for table in tables if isinstance(table, dict)]


def _plan_type_mappings(payload: Dict) -> List[Dict]:
    raw_plan = payload.get("plan")
    plan: Dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    raw_mappings = plan.get("type_mappings")
    mappings: List[Any] = raw_mappings if isinstance(raw_mappings, list) else []
    return [mapping for mapping in mappings if isinstance(mapping, dict)]


def format_plan_summary(payload: Dict) -> str:
    tables = _plan_tables(payload)
    mappings = _plan_type_mappings(payload)
    estimated_rows = 0
    for table in tables:
        raw_rows = table.get("estimated_rows")
        if not isinstance(raw_rows, int) or isinstance(raw_rows, bool):
            raw_rows = table.get("rows")
        if isinstance(raw_rows, int) and not isinstance(raw_rows, bool):
            estimated_rows += raw_rows

    lines = [
        f"전환 대상 테이블 {len(tables)}개",
        f"예상 rows {estimated_rows:,}",
    ]
    mapping_summaries: List[str] = []
    for mapping in mappings:
        source_type = str(mapping.get("source_type", "")).strip()
        target_type = str(mapping.get("target_type", "")).strip()
        if source_type and target_type:
            mapping_summaries.append(f"{source_type} -> {target_type}")
    if mapping_summaries:
        lines.append("타입 변환: " + ", ".join(mapping_summaries[:8]))

    raw_plan = payload.get("plan")
    plan: Dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    raw_ddl_order = plan.get("ddl_order")
    ddl_order: List[Any] = raw_ddl_order if isinstance(raw_ddl_order, list) else []
    ddl_order_text = " ".join(str(item).lower() for item in ddl_order)
    if "foreign" in ddl_order_text or "fk" in ddl_order_text:
        lines.append("FK/index는 데이터 적재 후 생성")
    return "\n".join(lines)


def _display_value(value: Any, limit: int = 160) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def format_verification_result(payload: Dict) -> str:
    mismatch_lines: List[str] = []
    raw_mismatches = payload.get("mismatches")
    if isinstance(raw_mismatches, list):
        for mismatch in raw_mismatches[:20]:
            if not isinstance(mismatch, dict):
                continue
            table = mismatch.get("table", "")
            key = mismatch.get("key", "")
            column = mismatch.get("column", "")
            source_value = mismatch.get("source_value", "")
            target_value = mismatch.get("target_value", "")
            difference = mismatch.get("difference", "")
            lines = [
                f"- 테이블: {table}",
                f"  Key: {key}",
                f"  Column: {column}",
                f"  Source: {_display_value(source_value)}",
                f"  Target: {_display_value(target_value)}",
            ]
            if difference:
                lines.append(f"  차이 유형: {difference}")
            mismatch_lines.append("\n".join(lines))

    row_diff_lines: List[str] = []
    raw_row_diffs = payload.get("row_count_differences")
    if isinstance(raw_row_diffs, list):
        for diff in raw_row_diffs:
            if not isinstance(diff, dict):
                continue
            source_rows = int(diff.get("source_rows", 0) or 0)
            target_rows = int(diff.get("target_rows", 0) or 0)
            delta = source_rows - target_rows
            row_diff_lines.append(
                f"- {diff.get('table', '')}: Source {source_rows:,} rows / "
                f"Target {target_rows:,} rows / 차이 {delta:+,}"
            )

    lines: List[str] = []
    if mismatch_lines:
        lines.append("Mismatch 예시")
        lines.extend(mismatch_lines)
    if row_diff_lines:
        if lines:
            lines.append("")
        lines.append("Row count 차이")
        lines.extend(row_diff_lines)
    if not lines and payload.get("success") is True:
        lines.append("검증 통과: Source와 Target 데이터가 일치합니다.")
    elif not lines:
        lines.append("검증 실패: Rust Core가 비교 차이 상세를 반환하지 않았습니다.")
    return "\n".join(lines)


def schema_from_inspect_result(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract normalized schema from an inspect result payload."""
    if payload.get("event") != "result" or payload.get("command") != "inspect":
        return None
    schema = payload.get("schema")
    if isinstance(schema, dict):
        return schema
    return None


def _render_issues(issues: Any) -> List[str]:
    """Render the top-level payload issue list section."""
    if not isinstance(issues, list):
        return []
    lines = ["", f"Issues: {len(issues)}"]
    for issue in issues:
        if isinstance(issue, dict):
            lines.append(
                f"- [{issue.get('severity', 'info')}] "
                f"{issue.get('location', '')}: {issue.get('message', '')}"
            )
    return lines


def _render_mismatches(mismatches: Any) -> List[str]:
    """Render the verify-result mismatch list section."""
    if not isinstance(mismatches, list):
        return []
    lines = ["", f"Mismatches: {len(mismatches)}"]
    for mismatch in mismatches[:MAX_MISMATCHES_DISPLAYED]:
        if isinstance(mismatch, dict):
            table = mismatch.get("table", "")
            kind = mismatch.get("kind", "")
            detail = mismatch.get("column") or mismatch.get("digest") or mismatch.get("message") or ""
            lines.append(f"- {table} {kind} {detail}".strip())
    if len(mismatches) > MAX_MISMATCHES_DISPLAYED:
        lines.append(f"... {len(mismatches) - MAX_MISMATCHES_DISPLAYED} more")
    return lines


def _render_plan(plan: Any) -> List[str]:
    """Render the plan DDL section."""
    if not isinstance(plan, dict):
        return []
    ddl = plan.get("ddl")
    if not isinstance(ddl, list):
        return []
    lines = ["", "DDL:"]
    lines.extend(str(item) for item in ddl)
    return lines


def _render_table_guide(table: Any) -> List[str]:
    """Render one table's columns, row samples, and insert example."""
    if not isinstance(table, dict):
        return []
    lines = [f"  Table {table.get('table', '')}: rows={table.get('row_count', 0)}"]

    columns = table.get("columns")
    if isinstance(columns, list):
        for column in columns:
            if not isinstance(column, dict):
                continue
            lines.append(
                "    Column "
                f"{column.get('name', '')}: "
                f"{column.get('source_type', '')} -> "
                f"{column.get('target_type', '')}"
            )

    rows = table.get("row_samples")
    if isinstance(rows, list):
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"    Row sample {index}: "
                f"{json.dumps(row, ensure_ascii=False, sort_keys=True)}"
            )

    insert_sql = table.get("insert_example_sql")
    if insert_sql:
        lines.append(f"    Insert example: {insert_sql}")

    return lines


def _render_direction_guide(guide: Dict[str, Any]) -> List[str]:
    """Render a direction's create-table SQL, follow-up SQL, and table guides."""
    lines: List[str] = []

    create_sql = guide.get("create_table_sql")
    if isinstance(create_sql, list) and create_sql:
        lines.append("  Create table SQL:")
        lines.extend(f"  {item}" for item in create_sql)

    sequence_sql = guide.get("sequence_reset_sql")
    post_data_sql = guide.get("post_data_sql")
    followup_sql = []
    if isinstance(sequence_sql, list):
        followup_sql.extend(sequence_sql)
    if isinstance(post_data_sql, list):
        followup_sql.extend(post_data_sql)
    if followup_sql:
        lines.append("  Follow-up SQL:")
        lines.extend(f"  {item}" for item in followup_sql)

    tables = guide.get("tables")
    if isinstance(tables, list):
        for table in tables:
            lines.extend(_render_table_guide(table))

    return lines


def _render_directions(directions: Any) -> List[str]:
    """Render the direction readiness section."""
    if not isinstance(directions, list):
        return []
    lines = ["", "Direction Readiness:"]
    for direction in directions:
        if not isinstance(direction, dict):
            continue
        status = "ready" if direction.get("success") else "blocked"
        direction_issues = direction.get("issues")
        issue_count = len(direction_issues) if isinstance(direction_issues, list) else 0
        lines.append(
            f"- {direction.get('direction', '')}: {status} "
            f"tables={direction.get('table_count', 0)} issues={issue_count}"
        )
        guide = direction.get("guide")
        if isinstance(guide, dict):
            lines.extend(_render_direction_guide(guide))
    return lines


def render_result_report(payload: Dict[str, Any]) -> str:
    """Render a concise human-readable migration helper result."""
    command = payload.get("command", "unknown")
    success = payload.get("success")
    lines = [
        "TunnelForge DB Migration Report",
        "=" * 34,
        f"Command: {command}",
        f"Success: {success}",
    ]

    if "rows_copied" in payload:
        lines.append(f"Rows copied: {payload.get('rows_copied')}")
    if "chunks_copied" in payload:
        lines.append(f"Chunks copied: {payload.get('chunks_copied')}")

    lines.extend(_render_issues(payload.get("issues")))
    lines.extend(_render_mismatches(payload.get("mismatches")))
    lines.extend(_render_plan(payload.get("plan")))
    lines.extend(_render_directions(payload.get("directions")))

    return "\n".join(lines) + "\n"
