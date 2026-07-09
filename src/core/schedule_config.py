"""
스케줄 설정 데이터 모델
- ScheduleTaskType: 스케줄 작업 유형
- ScheduleConfig: 스케줄 백업/SQL 실행 설정
- _ExecutionJob: 실행 큐에 올라가는 작업 단위
- _ResolvedConnection: 백업/SQL 실행이 공유하는 해석된 연결 정보
"""
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class ScheduleTaskType(str, Enum):
    """스케줄 작업 유형"""
    BACKUP = "backup"
    SQL_QUERY = "sql_query"


@dataclass
class ScheduleConfig:
    """스케줄 백업/SQL 실행 설정"""
    id: str
    name: str
    tunnel_id: str              # 사용할 터널 ID
    schema: str                 # Export 대상 스키마
    tables: List[str] = field(default_factory=list)  # 빈 리스트 = 전체
    output_dir: str = ""        # 출력 디렉토리
    cron_expression: str = "0 3 * * *"  # 기본: 매일 03:00
    enabled: bool = True
    retention_count: int = 5    # 보관할 백업 수
    retention_days: int = 30    # 보관 기간 (일)
    last_run: Optional[str] = None  # ISO format
    next_run: Optional[str] = None  # ISO format

    # === SQL 쿼리 실행 전용 필드 ===
    task_type: str = "backup"           # 작업 유형: backup, sql_query
    sql_query: str = ""                 # 실행할 SQL (;로 멀티 쿼리 구분)
    result_format: str = "csv"          # 결과 저장 형식: csv, json, none
    result_output_dir: str = ""         # 결과 저장 경로 (없으면 output_dir 사용)
    result_filename_pattern: str = "{name}_{timestamp}"  # 파일명 패턴
    query_timeout: int = 300            # 타임아웃 (초)
    result_retention_count: int = 10    # 결과 파일 보관 개수
    result_retention_days: int = 30     # 결과 파일 보관 기간

    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScheduleConfig':
        """딕셔너리에서 생성 (하위 호환성 지원)"""
        # 기존 설정에 새 필드가 없으면 기본값 적용
        defaults = {
            'task_type': 'backup',
            'sql_query': '',
            'result_format': 'csv',
            'result_output_dir': '',
            'result_filename_pattern': '{name}_{timestamp}',
            'query_timeout': 300,
            'result_retention_count': 10,
            'result_retention_days': 30,
        }
        for key, default_value in defaults.items():
            if key not in data:
                data[key] = default_value
        return cls(**data)

    def is_sql_query_task(self) -> bool:
        """SQL 쿼리 작업 여부"""
        return self.task_type == ScheduleTaskType.SQL_QUERY.value

    def get_result_output_path(self) -> str:
        """결과 저장 경로 반환 (result_output_dir 우선, 없으면 output_dir)"""
        return self.result_output_dir or self.output_dir


@dataclass
class _ExecutionJob:
    """실행 큐에 올라가는 작업 단위 (스케줄 스냅샷 + 실행 후 처리 방식)"""
    schedule: ScheduleConfig
    update_next_run: bool


@dataclass(frozen=True)
class _ResolvedConnection:
    """백업/SQL 실행이 공유하는 해석된 연결 정보"""
    host: str
    port: int
    user: str
    password: str
    engine: str
