"""
MySQL 8.0 → 8.4 마이그레이션 State Tracker

마이그레이션 진행 상태를 추적하고 저장하여, 중단된 마이그레이션을 재시작할 수 있도록 지원합니다.
- 상태 저장/로드
- 중단 후 재시작 지원
- 롤백 SQL 경로 관리
"""
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from src.core.logger import get_logger

logger = get_logger('migration_state')


class MigrationPhase:
    """마이그레이션 단계"""
    PREFLIGHT = "preflight"       # 사전 검사
    ANALYSIS = "analysis"         # 분석
    RECOMMENDATION = "recommendation"  # 권장 옵션 선택
    EXECUTION = "execution"       # 실행
    VALIDATION = "validation"     # 검증
    COMPLETED = "completed"       # 완료


@dataclass
class MigrationState:
    """마이그레이션 상태"""
    schema: str
    started_at: str  # ISO format datetime string
    current_phase: str = MigrationPhase.PREFLIGHT
    completed_steps: List[int] = field(default_factory=list)
    pending_steps: List[int] = field(default_factory=list)
    rollback_sql_path: Optional[str] = None
    last_updated: str = ""  # ISO format datetime string
    error_message: Optional[str] = None
    total_issues: int = 0
    fixed_issues: int = 0

    def __post_init__(self):
        if not self.last_updated:
            self.last_updated = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환 (JSON 직렬화용)"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MigrationState':
        """딕셔너리에서 생성"""
        return cls(**data)


class MigrationStateTracker:
    """마이그레이션 상태 추적기"""

    STATE_FILE_PREFIX = "migration_state_"
    STATE_FILE_EXT = ".json"

    def __init__(self):
        """초기화 - 상태 저장 디렉토리 설정"""
        self._state_dir = self._get_state_dir()
        self._current_states: Dict[str, MigrationState] = {}

    def _get_state_dir(self) -> Path:
        """상태 저장 디렉토리 경로 반환"""
        if os.name == 'nt':  # Windows
            base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
        else:  # Unix/Linux/Mac
            base = Path.home() / '.local' / 'share'

        state_dir = base / 'TunnelForge' / 'migration_state'
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir

    def _get_state_file(self, schema: str) -> Path:
        """상태 파일 경로 반환"""
        # 스키마명에서 안전하지 않은 문자 제거
        safe_schema = "".join(c if c.isalnum() or c in '_-' else '_' for c in schema)
        return self._state_dir / f"{self.STATE_FILE_PREFIX}{safe_schema}{self.STATE_FILE_EXT}"

    def _get_all_state_files(self) -> List[Path]:
        """모든 상태 파일 목록 반환"""
        return list(self._state_dir.glob(f"{self.STATE_FILE_PREFIX}*{self.STATE_FILE_EXT}"))

    def save_state(self, state: MigrationState) -> bool:
        """
        상태 저장

        Args:
            state: 저장할 MigrationState

        Returns:
            성공 여부
        """
        try:
            state.last_updated = datetime.now().isoformat()
            state_file = self._get_state_file(state.schema)

            with open(state_file, 'w', encoding='utf-8') as f:
                json.dump(state.to_dict(), f, indent=2, ensure_ascii=False)

            # 메모리 캐시 업데이트
            self._current_states[state.schema] = state

            logger.info(f"마이그레이션 상태 저장: {state.schema} (phase: {state.current_phase})")
            return True

        except Exception as e:
            logger.error(f"상태 저장 실패: {e}")
            return False

    def load_state(self, schema: str) -> Optional[MigrationState]:
        """
        상태 로드

        Args:
            schema: 스키마명

        Returns:
            MigrationState 또는 None
        """
        # 메모리 캐시 확인
        if schema in self._current_states:
            return self._current_states[schema]

        state_file = self._get_state_file(schema)

        if not state_file.exists():
            return None

        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            state = MigrationState.from_dict(data)
            self._current_states[schema] = state
            return state

        except Exception as e:
            logger.error(f"상태 로드 실패: {e}")
            return None

    def can_resume(self, schema: str) -> bool:
        """
        재시작 가능 여부 확인

        Args:
            schema: 스키마명

        Returns:
            재시작 가능 여부
        """
        state = self.load_state(schema)

        if not state:
            return False

        # 완료된 경우 재시작 불필요
        if state.current_phase == MigrationPhase.COMPLETED:
            return False

        # 에러가 있는 경우 재시작 가능
        # (에러 해결 후 재시작 시도)
        return True

    def clear_state(self, schema: str) -> bool:
        """
        상태 삭제

        Args:
            schema: 스키마명

        Returns:
            성공 여부
        """
        try:
            state_file = self._get_state_file(schema)

            if state_file.exists():
                state_file.unlink()

            # 메모리 캐시에서도 제거
            self._current_states.pop(schema, None)

            logger.info(f"마이그레이션 상태 삭제: {schema}")
            return True

        except Exception as e:
            logger.error(f"상태 삭제 실패: {e}")
            return False

    def update_phase(self, schema: str, phase: str) -> bool:
        """
        단계 업데이트

        Args:
            schema: 스키마명
            phase: 새 단계 (MigrationPhase 상수)

        Returns:
            성공 여부
        """
        state = self.load_state(schema)
        if not state:
            return False

        state.current_phase = phase
        return self.save_state(state)

    def mark_step_completed(self, schema: str, step_index: int) -> bool:
        """
        스텝 완료 표시

        Args:
            schema: 스키마명
            step_index: 완료된 스텝 인덱스

        Returns:
            성공 여부
        """
        state = self.load_state(schema)
        if not state:
            return False

        if step_index not in state.completed_steps:
            state.completed_steps.append(step_index)

        if step_index in state.pending_steps:
            state.pending_steps.remove(step_index)

        state.fixed_issues = len(state.completed_steps)

        return self.save_state(state)

    def set_error(self, schema: str, error_message: str) -> bool:
        """
        에러 기록

        Args:
            schema: 스키마명
            error_message: 에러 메시지

        Returns:
            성공 여부
        """
        state = self.load_state(schema)
        if not state:
            return False

        state.error_message = error_message
        return self.save_state(state)

    def clear_error(self, schema: str) -> bool:
        """
        에러 클리어

        Args:
            schema: 스키마명

        Returns:
            성공 여부
        """
        state = self.load_state(schema)
        if not state:
            return False

        state.error_message = None
        return self.save_state(state)

    def create_state(
        self,
        schema: str,
        total_issues: int,
        pending_steps: List[int]
    ) -> MigrationState:
        """
        새 마이그레이션 상태 생성

        Args:
            schema: 스키마명
            total_issues: 총 이슈 수
            pending_steps: 대기 중인 스텝 인덱스 목록

        Returns:
            생성된 MigrationState
        """
        state = MigrationState(
            schema=schema,
            started_at=datetime.now().isoformat(),
            current_phase=MigrationPhase.PREFLIGHT,
            completed_steps=[],
            pending_steps=pending_steps,
            total_issues=total_issues,
            fixed_issues=0
        )

        self.save_state(state)
        return state

    def list_incomplete_migrations(self) -> List[MigrationState]:
        """
        미완료 마이그레이션 목록 반환

        Returns:
            미완료 MigrationState 목록
        """
        incomplete = []

        for state_file in self._get_all_state_files():
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                state = MigrationState.from_dict(data)

                # 완료되지 않은 것만 포함
                if state.current_phase != MigrationPhase.COMPLETED:
                    incomplete.append(state)

            except Exception as e:
                logger.warning(f"상태 파일 읽기 실패: {state_file} - {e}")
                continue

        # 시작 시간 기준 정렬 (최신 먼저)
        incomplete.sort(key=lambda s: s.started_at, reverse=True)

        return incomplete

    def get_rollback_sql(self, schema: str) -> Optional[str]:
        """
        롤백 SQL 파일 경로 반환

        Args:
            schema: 스키마명

        Returns:
            롤백 SQL 파일 경로 또는 None
        """
        state = self.load_state(schema)
        if state and state.rollback_sql_path:
            if Path(state.rollback_sql_path).exists():
                return state.rollback_sql_path
        return None

    def set_rollback_sql(self, schema: str, sql_path: str) -> bool:
        """
        롤백 SQL 파일 경로 설정

        Args:
            schema: 스키마명
            sql_path: 롤백 SQL 파일 경로

        Returns:
            성공 여부
        """
        state = self.load_state(schema)
        if not state:
            return False

        state.rollback_sql_path = sql_path
        return self.save_state(state)

    def get_progress(self, schema: str) -> Dict[str, Any]:
        """
        마이그레이션 진행 상황 반환

        Args:
            schema: 스키마명

        Returns:
            진행 상황 딕셔너리
        """
        state = self.load_state(schema)
        if not state:
            return {
                'exists': False,
                'progress_percent': 0,
                'phase': None,
                'message': '상태 정보 없음'
            }

        total = state.total_issues
        fixed = state.fixed_issues
        progress = (fixed / total * 100) if total > 0 else 0

        phase_names = {
            MigrationPhase.PREFLIGHT: "사전 검사",
            MigrationPhase.ANALYSIS: "분석",
            MigrationPhase.RECOMMENDATION: "권장 옵션 선택",
            MigrationPhase.EXECUTION: "실행",
            MigrationPhase.VALIDATION: "검증",
            MigrationPhase.COMPLETED: "완료"
        }

        return {
            'exists': True,
            'progress_percent': round(progress, 1),
            'phase': state.current_phase,
            'phase_name': phase_names.get(state.current_phase, state.current_phase),
            'total_issues': total,
            'fixed_issues': fixed,
            'pending_steps': len(state.pending_steps),
            'has_error': state.error_message is not None,
            'error_message': state.error_message,
            'started_at': state.started_at,
            'last_updated': state.last_updated
        }


# 전역 싱글톤 인스턴스
_tracker_instance: Optional[MigrationStateTracker] = None


def get_state_tracker() -> MigrationStateTracker:
    """전역 StateTracker 인스턴스 반환"""
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = MigrationStateTracker()
    return _tracker_instance
