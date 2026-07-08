"""
스키마 비교 (Schema Diff)
- 두 DB 스키마 구조 비교
- 테이블/컬럼/인덱스/FK 차이 분석
- 동기화 SQL 스크립트 생성

owner 참고: 현재 UI 노출 스키마 비교/동기화 스크립트 생성은 이 모듈이
재수입하는 SchemaComparator / SyncScriptGenerator가 담당한다. Rust facade의
schema.diff 정리/라우팅은 이후 Rust-contract WP에서 다룬다. 이 모듈이
DB 작업 전체를 소유한다는 의미는 아니다.
"""
from src.core.schema_diff_models import (
    DiffSeverity,
    CompareLevel,
    VersionContext,
    SeveritySummary,
    DiffType,
    ColumnInfo,
    IndexInfo,
    ForeignKeyInfo,
    TableSchema,
    ColumnDiff,
    IndexDiff,
    ForeignKeyDiff,
    TableDiff,
    _normalize_column_extra,
)
from src.core.schema_extractor import SchemaExtractor
from src.core.schema_comparator import SchemaComparator
from src.core.schema_severity_classifier import SeverityClassifier
from src.core.schema_sync_script_generator import SyncScriptGenerator
