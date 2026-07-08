"""
스키마 비교(Schema Diff) - 데이터 모델 (Enum/dataclass) + 공유 순수 헬퍼
"""
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from enum import Enum


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
    """MySQL 버전 컨텍스트"""
    source_version: Tuple[int, int, int] = (0, 0, 0)
    target_version: Tuple[int, int, int] = (0, 0, 0)
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


class DiffType(Enum):
    """차이 유형"""
    ADDED = "added"       # 타겟에 추가 필요
    REMOVED = "removed"   # 타겟에서 삭제 필요
    MODIFIED = "modified"
    RENAMED = "renamed"   # 이름만 변경 (내용 동일)
    UNCHANGED = "unchanged"


def _normalize_column_extra(extra: Optional[str]) -> str:
    """MySQL INFORMATION_SCHEMA EXTRA 값을 비교/SQL 출력용으로 정규화.

    MySQL 8.0은 DEFAULT CURRENT_TIMESTAMP 등 컬럼의 EXTRA에
    DEFAULT_GENERATED를 자동으로 붙이지만 MySQL 5.7은 붙이지 않아,
    정규화하지 않으면 버전 차이만으로 잘못된 SQL/거짓 diff가 발생한다.
    """
    if not extra:
        return ""
    cleaned = re.sub(r"\bDEFAULT_GENERATED\b", "", extra, flags=re.IGNORECASE)
    return " ".join(cleaned.split())


@dataclass
class ColumnInfo:
    """컬럼 정보"""
    name: str
    data_type: str
    nullable: bool
    default: Optional[str]
    extra: str = ""      # AUTO_INCREMENT 등
    key: str = ""        # PRI, UNI, MUL
    charset: str = ""
    collation: str = ""

    def to_sql_definition(self) -> str:
        """SQL 컬럼 정의 생성"""
        parts = [f"`{self.name}`", self.data_type]

        if self.charset and self.charset not in self.data_type:
            parts.append(f"CHARACTER SET {self.charset}")

        if not self.nullable:
            parts.append("NOT NULL")
        else:
            parts.append("NULL")

        if self.default is not None:
            if self.default.upper() in ('CURRENT_TIMESTAMP', 'NULL'):
                parts.append(f"DEFAULT {self.default}")
            else:
                parts.append(f"DEFAULT '{self.default}'")

        extra = _normalize_column_extra(self.extra)
        if extra:
            parts.append(extra)

        return " ".join(parts)


@dataclass
class IndexInfo:
    """인덱스 정보"""
    name: str
    columns: List[str]
    unique: bool = False
    type: str = "BTREE"   # BTREE, FULLTEXT, HASH

    def to_sql_definition(self, table_name: str) -> str:
        """인덱스 생성 SQL"""
        cols = ", ".join(f"`{c}`" for c in self.columns)
        if self.name == "PRIMARY":
            return f"PRIMARY KEY ({cols})"
        elif self.unique:
            return f"UNIQUE INDEX `{self.name}` ({cols}) USING {self.type}"
        else:
            return f"INDEX `{self.name}` ({cols}) USING {self.type}"


@dataclass
class ForeignKeyInfo:
    """외래 키 정보"""
    name: str
    columns: List[str]
    ref_table: str
    ref_columns: List[str]
    on_delete: str = "RESTRICT"
    on_update: str = "RESTRICT"

    def to_sql_definition(self) -> str:
        """FK 정의 SQL"""
        cols = ", ".join(f"`{c}`" for c in self.columns)
        ref_cols = ", ".join(f"`{c}`" for c in self.ref_columns)
        return (
            f"CONSTRAINT `{self.name}` FOREIGN KEY ({cols}) "
            f"REFERENCES `{self.ref_table}` ({ref_cols}) "
            f"ON DELETE {self.on_delete} ON UPDATE {self.on_update}"
        )


@dataclass
class TableSchema:
    """테이블 스키마 정보"""
    name: str
    columns: List[ColumnInfo] = field(default_factory=list)
    indexes: List[IndexInfo] = field(default_factory=list)
    foreign_keys: List[ForeignKeyInfo] = field(default_factory=list)
    engine: str = "InnoDB"
    charset: str = "utf8mb4"
    collation: str = "utf8mb4_general_ci"
    row_count: int = 0

    def get_column(self, name: str) -> Optional[ColumnInfo]:
        """이름으로 컬럼 조회"""
        for col in self.columns:
            if col.name.lower() == name.lower():
                return col
        return None

    def get_index(self, name: str) -> Optional[IndexInfo]:
        """이름으로 인덱스 조회"""
        for idx in self.indexes:
            if idx.name.lower() == name.lower():
                return idx
        return None

    def get_foreign_key(self, name: str) -> Optional[ForeignKeyInfo]:
        """이름으로 FK 조회"""
        for fk in self.foreign_keys:
            if fk.name.lower() == name.lower():
                return fk
        return None


@dataclass
class ColumnDiff:
    """컬럼 차이"""
    column_name: str
    diff_type: DiffType
    source_info: Optional[ColumnInfo] = None
    target_info: Optional[ColumnInfo] = None
    differences: List[str] = field(default_factory=list)
    severity: Optional[DiffSeverity] = None


@dataclass
class IndexDiff:
    """인덱스 차이"""
    index_name: str
    diff_type: DiffType
    source_info: Optional[IndexInfo] = None
    target_info: Optional[IndexInfo] = None
    differences: List[str] = field(default_factory=list)
    severity: Optional[DiffSeverity] = None
    old_name: Optional[str] = None  # RENAMED 시 타겟 측 이전 이름


@dataclass
class ForeignKeyDiff:
    """FK 차이"""
    fk_name: str
    diff_type: DiffType
    source_info: Optional[ForeignKeyInfo] = None
    target_info: Optional[ForeignKeyInfo] = None
    differences: List[str] = field(default_factory=list)
    severity: Optional[DiffSeverity] = None
    old_name: Optional[str] = None  # RENAMED 시 타겟 측 이전 이름


@dataclass
class TableDiff:
    """테이블 차이"""
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

    def has_differences(self) -> bool:
        """차이가 있는지 확인"""
        if self.diff_type in [DiffType.ADDED, DiffType.REMOVED]:
            return True
        return any(d.diff_type != DiffType.UNCHANGED
                  for d in self.column_diffs + self.index_diffs + self.fk_diffs)
