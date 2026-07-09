"""
마이그레이션 분석 데이터 모델

MigrationAnalyzer 협력 모듈이 공유하는 데이터클래스/Enum 정의.
순환 import 방지를 위해 협력 모듈은 데이터클래스를 오직 이 모듈에서만 import한다.
"""
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

from src.core.migration_constants import IssueType, CompatibilityIssue


class ActionType(Enum):
    """조치 유형"""
    DELETE = "delete"  # 삭제
    UPDATE = "update"  # 업데이트
    SET_NULL = "set_null"  # NULL로 설정
    MANUAL = "manual"  # 수동 처리 필요


@dataclass
class OrphanRecord:
    """고아 레코드 정보"""
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    orphan_count: int
    sample_values: List[Any] = field(default_factory=list)


@dataclass
class ForeignKeyInfo:
    """FK 관계 정보"""
    constraint_name: str
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    on_delete: str
    on_update: str


@dataclass
class CleanupAction:
    """정리 작업"""
    action_type: ActionType
    table: str
    description: str
    sql: str
    affected_rows: int
    dry_run: bool = True
    # dry-run 시 COUNT 쿼리를 만들 때 쓰는 실행 메타데이터.
    # sql 텍스트를 문자열 분해(split)로 재파싱하지 않기 위해 생성 시점에
    # 직접 저장해둔다 (테이블명에 FROM/WHERE/SET 같은 키워드가 포함돼도 안전).
    target_schema: Optional[str] = None
    target_table: Optional[str] = None


@dataclass
class SchemaCheckOptions:
    """스키마 분석 검사 활성화 옵션

    analyze_schema의 15개 check_* 불리언 인자를 하나로 묶어
    analyze_schema → _analyze_schema_impl 사이의 15-인자 verbatim
    재선언/일대일 pass-through 중복을 제거하기 위한 값 객체.
    (기본값은 모두 True — 특히 check_int_display_width도 True 유지)
    """
    check_orphans: bool = True
    check_charset: bool = True
    check_keywords: bool = True
    check_routines: bool = True
    check_sql_mode: bool = True
    check_auth_plugins: bool = True
    check_zerofill: bool = True
    check_float_precision: bool = True
    check_fk_name_length: bool = True
    check_invalid_dates: bool = True
    check_year2: bool = True
    check_deprecated_engines: bool = True
    check_enum_empty: bool = True
    check_timestamp_range: bool = True
    check_int_display_width: bool = True


@dataclass
class AnalysisResult:
    """분석 결과"""
    schema: str
    analyzed_at: str
    total_tables: int
    total_fk_relations: int
    orphan_records: List[OrphanRecord] = field(default_factory=list)
    compatibility_issues: List[CompatibilityIssue] = field(default_factory=list)
    cleanup_actions: List[CleanupAction] = field(default_factory=list)
    fk_tree: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON 직렬화용 딕셔너리 변환"""
        import dataclasses
        return {
            'schema': self.schema,
            'analyzed_at': self.analyzed_at,
            'total_tables': self.total_tables,
            'total_fk_relations': self.total_fk_relations,
            'orphan_records': [dataclasses.asdict(o) for o in self.orphan_records],
            'compatibility_issues': [
                {**dataclasses.asdict(i), 'issue_type': i.issue_type.value}
                for i in self.compatibility_issues
            ],
            'cleanup_actions': [
                {**dataclasses.asdict(a), 'action_type': a.action_type.value}
                for a in self.cleanup_actions
            ],
            'fk_tree': self.fk_tree
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'AnalysisResult':
        """딕셔너리에서 AnalysisResult 복원"""
        orphan_records = [OrphanRecord(**o) for o in data.get('orphan_records', [])]
        compatibility_issues = [
            CompatibilityIssue(
                issue_type=IssueType(i['issue_type']),
                severity=i['severity'],
                location=i['location'],
                description=i['description'],
                suggestion=i['suggestion'],
                fix_query=i.get('fix_query'),
                doc_link=i.get('doc_link'),
                upgrade_check_id=i.get('upgrade_check_id'),
                code_snippet=i.get('code_snippet'),
                table_name=i.get('table_name'),
                column_name=i.get('column_name')
            )
            for i in data.get('compatibility_issues', [])
        ]
        cleanup_actions = [
            CleanupAction(
                action_type=ActionType(a['action_type']),
                table=a['table'],
                description=a['description'],
                sql=a['sql'],
                affected_rows=a['affected_rows'],
                dry_run=a.get('dry_run', True),
                target_schema=a.get('target_schema'),
                target_table=a.get('target_table')
            )
            for a in data.get('cleanup_actions', [])
        ]

        return cls(
            schema=data['schema'],
            analyzed_at=data['analyzed_at'],
            total_tables=data['total_tables'],
            total_fk_relations=data['total_fk_relations'],
            orphan_records=orphan_records,
            compatibility_issues=compatibility_issues,
            cleanup_actions=cleanup_actions,
            fk_tree=data.get('fk_tree', {})
        )
