"""
SQL 스키마 메타데이터
- 테이블/컬럼 존재 여부 조회, 유사 이름 제안
- 스키마별 인메모리 캐시 제공자
"""
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from difflib import get_close_matches


# get_close_matches 유사 이름 제안 시 사용하는 최소 유사도 기준값 (0~1)
FUZZY_MATCH_CUTOFF = 0.5


def _schema_key(schema: Optional[str]) -> Optional[str]:
    """스키마명을 캐시 키로 정규화 (None/빈 문자열/공백만 있으면 None)"""
    if schema is None:
        return None
    stripped = schema.strip()
    return stripped or None


@dataclass
class SchemaMetadata:
    """스키마 메타데이터"""
    tables: Set[str] = field(default_factory=set)
    columns: Dict[str, Set[str]] = field(default_factory=dict)  # table -> columns
    db_version: Tuple[int, int, int] = (0, 0, 0)

    def has_table(self, table: str) -> bool:
        """테이블 존재 여부 (대소문자 무시)"""
        return table.lower() in {t.lower() for t in self.tables}

    def get_table_name(self, table: str) -> Optional[str]:
        """실제 테이블명 반환 (대소문자 매칭)"""
        table_lower = table.lower()
        for t in self.tables:
            if t.lower() == table_lower:
                return t
        return None

    def has_column(self, table: str, column: str) -> bool:
        """컬럼 존재 여부 (대소문자 무시)"""
        real_table = self.get_table_name(table)
        if not real_table or real_table not in self.columns:
            return False
        return column.lower() in {c.lower() for c in self.columns[real_table]}

    def get_column_name(self, table: str, column: str) -> Optional[str]:
        """실제 컬럼명 반환"""
        real_table = self.get_table_name(table)
        if not real_table or real_table not in self.columns:
            return None
        col_lower = column.lower()
        for c in self.columns[real_table]:
            if c.lower() == col_lower:
                return c
        return None

    def get_similar_tables(self, table: str, n: int = 3) -> List[str]:
        """유사한 테이블명 제안"""
        return get_close_matches(table.lower(), [t.lower() for t in self.tables], n=n, cutoff=FUZZY_MATCH_CUTOFF)

    def get_similar_columns(self, table: str, column: str, n: int = 3) -> List[str]:
        """유사한 컬럼명 제안"""
        real_table = self.get_table_name(table)
        if not real_table or real_table not in self.columns:
            return []
        return get_close_matches(column.lower(), [c.lower() for c in self.columns[real_table]], n=n, cutoff=FUZZY_MATCH_CUTOFF)


class SchemaMetadataProvider:
    """스키마 메타데이터 제공자 (스키마별 인메모리 캐시)

    Python 쪽에서는 동기 DB 조회를 하지 않는다. 메타데이터는 반드시
    `set_metadata()` (또는 호환용 `_metadata` 직접 대입)로 채워져야 하며,
    캐시 미스 시에는 커넥터를 호출하지 않고 빈 SchemaMetadata를 반환한다.
    이는 ValidationWorker가 MetadataLoadWorker와 같은 커넥터를 두고
    경쟁(race)하는 것을 방지하기 위함이다.
    """

    def __init__(self):
        self._metadata_by_schema: Dict[Optional[str], SchemaMetadata] = {}
        self._active_schema_key: Optional[str] = None
        self._connector = None
        self._lock = threading.RLock()

    @property
    def _metadata(self) -> Optional[SchemaMetadata]:
        """호환용 속성 (UI 등 외부에서 `_metadata`를 직접 읽는 경우 대응)"""
        with self._lock:
            if self._active_schema_key in self._metadata_by_schema:
                return self._metadata_by_schema[self._active_schema_key]
            return self._metadata_by_schema.get(None)

    @_metadata.setter
    def _metadata(self, value: Optional[SchemaMetadata]):
        """호환용 속성 (UI 등 외부에서 `_metadata`를 직접 대입하는 경우 대응)

        `set_connector(connector)` 직후 활성 스키마(`connector.database`)에
        매핑해 저장한다. 신규 코드는 `set_metadata(schema, metadata)`를 사용할 것.
        """
        with self._lock:
            if value is None:
                if self._active_schema_key is not None:
                    self._metadata_by_schema.pop(self._active_schema_key, None)
                else:
                    self._metadata_by_schema.clear()
                return
            self._metadata_by_schema[self._active_schema_key] = value

    def set_connector(self, connector):
        """DB 커넥터 설정

        커넥터가 바뀌면 이전 캐시가 다른 연결의 것일 수 있으므로 무효화한다.
        여기서는 DB에 동기 조회를 하지 않는다.
        """
        with self._lock:
            self._connector = connector
            self._active_schema_key = _schema_key(getattr(connector, "database", None))
            self._metadata_by_schema.clear()

    def set_metadata(self, schema: str, metadata: SchemaMetadata):
        """스키마에 대한 메타데이터를 캐시에 저장 (백그라운드 로드 완료 후 호출)"""
        if metadata is None:
            raise ValueError("metadata는 None일 수 없습니다")

        key = _schema_key(schema)
        with self._lock:
            self._metadata_by_schema[key] = metadata
            self._active_schema_key = key

    def get_metadata(self, schema: str = None) -> SchemaMetadata:
        """메타데이터 조회 (캐시 히트만 반환, 캐시 미스 시 커넥터 조회하지 않음)"""
        key = _schema_key(schema)
        with self._lock:
            if key in self._metadata_by_schema:
                return self._metadata_by_schema[key]
            if key is None and None in self._metadata_by_schema:
                return self._metadata_by_schema[None]
            return SchemaMetadata()

    def invalidate(self, schema: str = None):
        """캐시 무효화

        Args:
            schema: 지정하면 해당 스키마만 무효화, None이면 전체 무효화
        """
        with self._lock:
            if schema is None:
                self._metadata_by_schema.clear()
            else:
                self._metadata_by_schema.pop(_schema_key(schema), None)
