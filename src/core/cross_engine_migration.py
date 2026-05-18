"""Cross-engine migration protocol models.

The PyQt UI talks to the Rust helper through newline-delimited JSON.  This
module keeps that wire format isolated from widgets and worker orchestration.
"""
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


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
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    return base / "TunnelForge"


def db_core_executable() -> str:
    """Return the best available Rust DB core executable path."""
    exe_names = (
        ["tunnelforge-core.exe"]
        if os.name == "nt"
        else ["tunnelforge-core"]
    )
    candidate_dirs: List[Path] = []

    if hasattr(sys, "_MEIPASS"):
        candidate_dirs.append(Path(sys._MEIPASS))  # type: ignore[attr-defined]
    if getattr(sys, "frozen", False):
        candidate_dirs.append(Path(sys.executable).resolve().parent)
        candidate_dirs.append(Path.cwd())

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
    return {
        "engine": engine.value,
        "host": host,
        "port": int(port),
        "user": user,
        "password": password,
        "database": database,
        "schema": schema,
    }


def schema_from_inspect_result(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract normalized schema from an inspect result payload."""
    if payload.get("event") != "result" or payload.get("command") != "inspect":
        return None
    schema = payload.get("schema")
    if isinstance(schema, dict):
        return schema
    return None


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

    issues = payload.get("issues")
    if isinstance(issues, list):
        lines.append("")
        lines.append(f"Issues: {len(issues)}")
        for issue in issues:
            if isinstance(issue, dict):
                lines.append(
                    f"- [{issue.get('severity', 'info')}] "
                    f"{issue.get('location', '')}: {issue.get('message', '')}"
                )

    mismatches = payload.get("mismatches")
    if isinstance(mismatches, list):
        lines.append("")
        lines.append(f"Mismatches: {len(mismatches)}")
        for mismatch in mismatches[:50]:
            if isinstance(mismatch, dict):
                table = mismatch.get("table", "")
                kind = mismatch.get("kind", "")
                detail = mismatch.get("column") or mismatch.get("digest") or mismatch.get("message") or ""
                lines.append(f"- {table} {kind} {detail}".strip())
        if len(mismatches) > 50:
            lines.append(f"... {len(mismatches) - 50} more")

    plan = payload.get("plan")
    if isinstance(plan, dict):
        ddl = plan.get("ddl")
        if isinstance(ddl, list):
            lines.append("")
            lines.append("DDL:")
            lines.extend(str(item) for item in ddl)

    directions = payload.get("directions")
    if isinstance(directions, list):
        lines.append("")
        lines.append("Direction Readiness:")
        for direction in directions:
            if isinstance(direction, dict):
                status = "ready" if direction.get("success") else "blocked"
                issues = direction.get("issues")
                issue_count = len(issues) if isinstance(issues, list) else 0
                lines.append(
                    f"- {direction.get('direction', '')}: {status} "
                    f"tables={direction.get('table_count', 0)} issues={issue_count}"
                )
                guide = direction.get("guide")
                if isinstance(guide, dict):
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
                            if not isinstance(table, dict):
                                continue
                            lines.append(
                                f"  Table {table.get('table', '')}: rows={table.get('row_count', 0)}"
                            )
                            columns = table.get("columns")
                            if isinstance(columns, list):
                                for column in columns:
                                    if isinstance(column, dict):
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

    return "\n".join(lines) + "\n"
