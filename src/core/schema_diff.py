"""
스키마 비교(Schema Diff) 결과 모델

비교·심각도 분류·동기화 SQL 생성은 Rust core 의 `schema.compare` 가 한다.
이 모듈은 그 JSON 결과를 UI 가 쓰는 객체로 바꾸기만 한다.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DiffType(Enum):
    """차이 유형"""
    ADDED = "added"       # 타겟에 추가 필요
    REMOVED = "removed"   # 타겟에서 삭제 필요
    MODIFIED = "modified"
    RENAMED = "renamed"   # 이름만 변경 (내용 동일)
    UNCHANGED = "unchanged"


class DiffSeverity(Enum):
    """차이 심각도"""
    CRITICAL = "critical"   # Import 실패 위험
    WARNING = "warning"     # 성능/무결성 영향
    INFO = "info"           # 무시 가능


class CompareLevel(Enum):
    """비교 수준"""
    QUICK = "quick"         # 테이블/컬럼 존재성, 타입만
    STANDARD = "standard"   # + 인덱스, FK, 기본값
    STRICT = "strict"       # + charset, collation


@dataclass
class VersionContext:
    """양쪽 MySQL 버전 (표시용)"""
    source_version_str: str = ""
    target_version_str: str = ""


@dataclass
class SeveritySummary:
    """심각도 요약"""
    critical: int = 0
    warning: int = 0
    info: int = 0

    @property
    def has_critical(self) -> bool:
        return self.critical > 0


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool = True
    default: Optional[str] = None
    extra: str = ""
    charset: str = ""
    collation: str = ""
    comment: str = ""

    def __str__(self) -> str:
        return f"{self.name} {self.data_type}"


@dataclass
class IndexInfo:
    name: str
    columns: List[str] = field(default_factory=list)
    unique: bool = False
    type: str = "BTREE"

    def __str__(self) -> str:
        cols = ", ".join(self.columns)
        if self.name.upper() == "PRIMARY":
            return f"PRIMARY KEY ({cols})"
        prefix = "UNIQUE INDEX" if self.unique else "INDEX"
        return f"{prefix} {self.name} ({cols}) USING {self.type}"


@dataclass
class ForeignKeyInfo:
    name: str
    columns: List[str] = field(default_factory=list)
    ref_table: str = ""
    ref_columns: List[str] = field(default_factory=list)
    on_delete: str = "RESTRICT"
    on_update: str = "RESTRICT"

    def __str__(self) -> str:
        return (
            f"CONSTRAINT {self.name} FOREIGN KEY ({', '.join(self.columns)}) "
            f"REFERENCES {self.ref_table} ({', '.join(self.ref_columns)}) "
            f"ON DELETE {self.on_delete} ON UPDATE {self.on_update}"
        )


@dataclass
class TableSchema:
    name: str
    columns: List[ColumnInfo] = field(default_factory=list)
    indexes: List[IndexInfo] = field(default_factory=list)
    foreign_keys: List[ForeignKeyInfo] = field(default_factory=list)
    engine: str = ""
    collation: str = ""
    row_count: int = 0


@dataclass
class ColumnDiff:
    column_name: str
    diff_type: DiffType
    source_info: Optional[ColumnInfo] = None
    target_info: Optional[ColumnInfo] = None
    differences: List[str] = field(default_factory=list)
    severity: Optional[DiffSeverity] = None


@dataclass
class IndexDiff:
    index_name: str
    diff_type: DiffType
    source_info: Optional[IndexInfo] = None
    target_info: Optional[IndexInfo] = None
    differences: List[str] = field(default_factory=list)
    severity: Optional[DiffSeverity] = None
    old_name: Optional[str] = None  # RENAMED 시 타겟 측 이전 이름


@dataclass
class ForeignKeyDiff:
    fk_name: str
    diff_type: DiffType
    source_info: Optional[ForeignKeyInfo] = None
    target_info: Optional[ForeignKeyInfo] = None
    differences: List[str] = field(default_factory=list)
    severity: Optional[DiffSeverity] = None
    old_name: Optional[str] = None


@dataclass
class TableDiff:
    table_name: str
    diff_type: DiffType
    source_schema: Optional[TableSchema] = None
    target_schema: Optional[TableSchema] = None
    column_diffs: List[ColumnDiff] = field(default_factory=list)
    index_diffs: List[IndexDiff] = field(default_factory=list)
    fk_diffs: List[ForeignKeyDiff] = field(default_factory=list)
    row_count_source: int = 0
    row_count_target: int = 0
    severity: Optional[DiffSeverity] = None


@dataclass
class CompareResult:
    diffs: List[TableDiff]
    summary: SeveritySummary
    version_ctx: VersionContext
    sync_sql: str = ""
    row_counts_exact: bool = False


def _severity(value: Any) -> Optional[DiffSeverity]:
    try:
        return DiffSeverity(value) if value else None
    except ValueError:
        return None


def _texts(entry: Dict[str, Any]) -> List[str]:
    return [str(change.get("text", "")) for change in entry.get("changes") or [] if isinstance(change, dict)]


def _column(data: Optional[Dict[str, Any]]) -> Optional[ColumnInfo]:
    if not isinstance(data, dict):
        return None
    return ColumnInfo(
        name=str(data.get("name", "")),
        data_type=str(data.get("column_type", "")),
        nullable=bool(data.get("nullable", True)),
        default=data.get("default"),
        extra=str(data.get("extra", "")),
        charset=str(data.get("charset", "")),
        collation=str(data.get("collation", "")),
        comment=str(data.get("comment", "")),
    )


def _index(data: Optional[Dict[str, Any]]) -> Optional[IndexInfo]:
    if not isinstance(data, dict):
        return None
    columns = []
    for part in data.get("parts") or []:
        name = part.get("column") or "<expression>"
        columns.append(f"{name}({part['sub_part']})" if part.get("sub_part") else name)
    return IndexInfo(
        name=str(data.get("name", "")),
        columns=columns,
        unique=bool(data.get("unique", False)),
        type=str(data.get("index_type", "")),
    )


def _foreign_key(data: Optional[Dict[str, Any]]) -> Optional[ForeignKeyInfo]:
    if not isinstance(data, dict):
        return None
    return ForeignKeyInfo(
        name=str(data.get("name", "")),
        columns=[str(c) for c in data.get("columns") or []],
        ref_table=str(data.get("ref_table", "")),
        ref_columns=[str(c) for c in data.get("ref_columns") or []],
        on_delete=str(data.get("on_delete", "")),
        on_update=str(data.get("on_update", "")),
    )


def _table(data: Optional[Dict[str, Any]]) -> Optional[TableSchema]:
    if not isinstance(data, dict):
        return None
    return TableSchema(
        name=str(data.get("name", "")),
        columns=[c for c in map(_column, data.get("columns") or []) if c],
        indexes=[i for i in map(_index, data.get("indexes") or []) if i],
        foreign_keys=[f for f in map(_foreign_key, data.get("foreign_keys") or []) if f],
        engine=str(data.get("engine", "")),
        collation=str(data.get("collation", "")),
        row_count=int(data.get("row_count", 0) or 0),
    )


def parse_compare_result(result: Dict[str, Any]) -> CompareResult:
    """Rust `schema.compare` 결과를 UI 모델로 변환한다."""
    diffs = []
    for table in result.get("tables") or []:
        diffs.append(TableDiff(
            table_name=str(table.get("name", "")),
            diff_type=DiffType(table.get("diff_type", "unchanged")),
            source_schema=_table(table.get("source")),
            target_schema=_table(table.get("target")),
            column_diffs=[
                ColumnDiff(
                    column_name=str(c.get("name", "")),
                    diff_type=DiffType(c.get("diff_type", "unchanged")),
                    source_info=_column(c.get("source")),
                    target_info=_column(c.get("target")),
                    differences=_texts(c),
                    severity=_severity(c.get("severity")),
                )
                for c in table.get("columns") or []
            ],
            index_diffs=[
                IndexDiff(
                    index_name=str(i.get("name", "")),
                    diff_type=DiffType(i.get("diff_type", "unchanged")),
                    source_info=_index(i.get("source")),
                    target_info=_index(i.get("target")),
                    differences=_texts(i),
                    severity=_severity(i.get("severity")),
                    old_name=i.get("old_name"),
                )
                for i in table.get("indexes") or []
            ],
            fk_diffs=[
                ForeignKeyDiff(
                    fk_name=str(f.get("name", "")),
                    diff_type=DiffType(f.get("diff_type", "unchanged")),
                    source_info=_foreign_key(f.get("source")),
                    target_info=_foreign_key(f.get("target")),
                    differences=_texts(f),
                    severity=_severity(f.get("severity")),
                    old_name=f.get("old_name"),
                )
                for f in table.get("foreign_keys") or []
            ],
            row_count_source=int(table.get("row_count_source", 0) or 0),
            row_count_target=int(table.get("row_count_target", 0) or 0),
            severity=_severity(table.get("severity")),
        ))
    summary_data = result.get("summary") or {}
    return CompareResult(
        diffs=diffs,
        summary=SeveritySummary(
            critical=int(summary_data.get("critical", 0) or 0),
            warning=int(summary_data.get("warning", 0) or 0),
            info=int(summary_data.get("info", 0) or 0),
        ),
        version_ctx=VersionContext(
            source_version_str=str(result.get("source_version", "")),
            target_version_str=str(result.get("target_version", "")),
        ),
        sync_sql=str(result.get("sync_sql", "")),
        row_counts_exact=bool(result.get("row_counts_exact", False)),
    )
