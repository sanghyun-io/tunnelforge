"""
migration_state_tracker.py 단위 테스트

MigrationPhase 상수만 검증한다. 상태 저장/재시작/롤백 등
MigrationStateTracker의 영속화 기능은 Rust DB Core로 이관되어 삭제되었고,
프로덕션(oneclick_migration_dialog.py)에서는 MigrationPhase 상수만 계속 사용한다.
"""
from src.core.migration_state_tracker import MigrationPhase


class TestMigrationPhase:
    def test_phase_values(self):
        assert MigrationPhase.PREFLIGHT == "preflight"
        assert MigrationPhase.ANALYSIS == "analysis"
        assert MigrationPhase.RECOMMENDATION == "recommendation"
        assert MigrationPhase.EXECUTION == "execution"
        assert MigrationPhase.VALIDATION == "validation"
        assert MigrationPhase.COMPLETED == "completed"

    def test_all_phases_distinct(self):
        phases = [
            MigrationPhase.PREFLIGHT,
            MigrationPhase.ANALYSIS,
            MigrationPhase.RECOMMENDATION,
            MigrationPhase.EXECUTION,
            MigrationPhase.VALIDATION,
            MigrationPhase.COMPLETED,
        ]
        assert len(set(phases)) == len(phases)
