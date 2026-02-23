"""
migration_state_tracker.py 단위 테스트

MigrationPhase, MigrationState, MigrationStateTracker 검증.
tmp_path로 파일 I/O를 격리하여 실제 AppData에 쓰지 않음.
"""
import json
from datetime import datetime
from pathlib import Path

import pytest

from src.core.migration_state_tracker import (
    MigrationPhase,
    MigrationState,
    MigrationStateTracker,
    get_state_tracker,
)


# ============================================================
# 헬퍼 픽스처
# ============================================================
@pytest.fixture
def tracker(tmp_path, monkeypatch):
    """tmp_path를 상태 디렉토리로 사용하는 MigrationStateTracker"""
    state_dir = tmp_path / "migration_state"
    state_dir.mkdir()

    t = MigrationStateTracker.__new__(MigrationStateTracker)
    t._state_dir = state_dir
    t._current_states = {}
    return t


@pytest.fixture
def sample_state():
    return MigrationState(
        schema="testdb",
        started_at=datetime.now().isoformat(),
        current_phase=MigrationPhase.ANALYSIS,
        pending_steps=[0, 1, 2],
        total_issues=3,
        fixed_issues=0,
    )


# ============================================================
# MigrationPhase 상수 테스트
# ============================================================
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


# ============================================================
# MigrationState 데이터클래스 테스트
# ============================================================
class TestMigrationState:
    def test_defaults(self):
        state = MigrationState(schema="db", started_at="2025-01-01T00:00:00")
        assert state.current_phase == MigrationPhase.PREFLIGHT
        assert state.completed_steps == []
        assert state.pending_steps == []
        assert state.rollback_sql_path is None
        assert state.error_message is None
        assert state.total_issues == 0
        assert state.fixed_issues == 0

    def test_post_init_sets_last_updated(self):
        state = MigrationState(schema="db", started_at="2025-01-01T00:00:00")
        assert state.last_updated != ""
        # ISO 형식인지 확인
        datetime.fromisoformat(state.last_updated)

    def test_post_init_keeps_existing_last_updated(self):
        existing = "2025-06-15T12:00:00"
        state = MigrationState(
            schema="db",
            started_at="2025-01-01T00:00:00",
            last_updated=existing
        )
        # post_init은 last_updated가 비어있을 때만 설정
        assert state.last_updated == existing

    def test_to_dict(self):
        state = MigrationState(
            schema="mydb",
            started_at="2025-01-01T00:00:00",
            current_phase=MigrationPhase.EXECUTION,
            completed_steps=[0, 1],
            pending_steps=[2, 3],
            total_issues=4,
            fixed_issues=2,
        )
        d = state.to_dict()
        assert d["schema"] == "mydb"
        assert d["current_phase"] == "execution"
        assert d["completed_steps"] == [0, 1]
        assert d["pending_steps"] == [2, 3]
        assert d["total_issues"] == 4
        assert d["fixed_issues"] == 2

    def test_from_dict_roundtrip(self):
        state = MigrationState(
            schema="mydb",
            started_at="2025-01-01T00:00:00",
            current_phase=MigrationPhase.VALIDATION,
            rollback_sql_path="/tmp/rollback.sql",
            error_message="some error",
            total_issues=10,
            fixed_issues=7,
        )
        d = state.to_dict()
        restored = MigrationState.from_dict(d)

        assert restored.schema == state.schema
        assert restored.current_phase == state.current_phase
        assert restored.rollback_sql_path == state.rollback_sql_path
        assert restored.error_message == state.error_message
        assert restored.total_issues == state.total_issues
        assert restored.fixed_issues == state.fixed_issues

    def test_from_dict_with_all_fields(self):
        data = {
            "schema": "prod",
            "started_at": "2025-01-01T00:00:00",
            "current_phase": "analysis",
            "completed_steps": [1, 2, 3],
            "pending_steps": [4, 5],
            "rollback_sql_path": None,
            "last_updated": "2025-01-02T00:00:00",
            "error_message": None,
            "total_issues": 5,
            "fixed_issues": 3,
        }
        state = MigrationState.from_dict(data)
        assert state.schema == "prod"
        assert state.completed_steps == [1, 2, 3]
        assert state.pending_steps == [4, 5]


# ============================================================
# MigrationStateTracker - 파일 명 생성
# ============================================================
class TestStateFileNaming:
    def test_get_state_file_basic(self, tracker):
        path = tracker._get_state_file("mydb")
        assert path.name == "migration_state_mydb.json"
        assert path.parent == tracker._state_dir

    def test_get_state_file_special_chars(self, tracker):
        path = tracker._get_state_file("my-db_test")
        assert "my-db_test" in path.name

    def test_get_state_file_unsafe_chars(self, tracker):
        path = tracker._get_state_file("db/with/slashes")
        assert "/" not in path.name
        assert "\\" not in path.name

    def test_get_state_file_spaces(self, tracker):
        path = tracker._get_state_file("my db")
        assert " " not in path.name

    def test_get_all_state_files_empty(self, tracker):
        files = tracker._get_all_state_files()
        assert files == []

    def test_get_all_state_files(self, tracker):
        # 파일 2개 직접 생성
        (tracker._state_dir / "migration_state_a.json").write_text("{}")
        (tracker._state_dir / "migration_state_b.json").write_text("{}")
        (tracker._state_dir / "other_file.txt").write_text("not state")

        files = tracker._get_all_state_files()
        assert len(files) == 2


# ============================================================
# MigrationStateTracker - save/load
# ============================================================
class TestSaveLoadState:
    def test_save_state_creates_file(self, tracker, sample_state):
        result = tracker.save_state(sample_state)
        assert result is True

        state_file = tracker._get_state_file(sample_state.schema)
        assert state_file.exists()

    def test_save_state_json_content(self, tracker, sample_state):
        tracker.save_state(sample_state)
        state_file = tracker._get_state_file(sample_state.schema)

        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)

        assert data["schema"] == "testdb"
        assert data["current_phase"] == "analysis"

    def test_save_state_updates_last_updated(self, tracker, sample_state):
        old_ts = sample_state.last_updated
        import time
        time.sleep(0.01)
        tracker.save_state(sample_state)
        # last_updated는 save 시 갱신됨
        assert sample_state.last_updated >= old_ts

    def test_save_state_updates_cache(self, tracker, sample_state):
        tracker.save_state(sample_state)
        assert "testdb" in tracker._current_states

    def test_load_state_from_cache(self, tracker, sample_state):
        tracker.save_state(sample_state)
        # 캐시에서 로드
        loaded = tracker.load_state("testdb")
        assert loaded is sample_state  # 동일 객체

    def test_load_state_from_file(self, tracker, sample_state):
        tracker.save_state(sample_state)
        # 캐시 초기화하여 파일에서 로드 강제
        tracker._current_states.clear()

        loaded = tracker.load_state("testdb")
        assert loaded is not None
        assert loaded.schema == "testdb"
        assert loaded.current_phase == "analysis"

    def test_load_state_not_found(self, tracker):
        result = tracker.load_state("nonexistent_schema")
        assert result is None

    def test_load_state_corrupt_file(self, tracker):
        state_file = tracker._get_state_file("broken")
        state_file.write_text("not valid json", encoding="utf-8")

        result = tracker.load_state("broken")
        assert result is None

    def test_save_load_roundtrip(self, tracker):
        state = MigrationState(
            schema="roundtrip",
            started_at=datetime.now().isoformat(),
            current_phase=MigrationPhase.EXECUTION,
            completed_steps=[0, 1, 2],
            pending_steps=[3],
            rollback_sql_path="/tmp/rb.sql",
            total_issues=4,
            fixed_issues=3,
        )
        tracker.save_state(state)
        tracker._current_states.clear()

        loaded = tracker.load_state("roundtrip")
        assert loaded.completed_steps == [0, 1, 2]
        assert loaded.pending_steps == [3]
        assert loaded.rollback_sql_path == "/tmp/rb.sql"
        assert loaded.total_issues == 4


# ============================================================
# MigrationStateTracker - can_resume
# ============================================================
class TestCanResume:
    def test_can_resume_true(self, tracker, sample_state):
        tracker.save_state(sample_state)
        assert tracker.can_resume("testdb") is True

    def test_can_resume_completed(self, tracker, sample_state):
        sample_state.current_phase = MigrationPhase.COMPLETED
        tracker.save_state(sample_state)
        assert tracker.can_resume("testdb") is False

    def test_can_resume_no_state(self, tracker):
        assert tracker.can_resume("unknown") is False

    def test_can_resume_with_error(self, tracker, sample_state):
        sample_state.error_message = "something went wrong"
        tracker.save_state(sample_state)
        # 에러 있어도 재시작 가능
        assert tracker.can_resume("testdb") is True


# ============================================================
# MigrationStateTracker - clear_state
# ============================================================
class TestClearState:
    def test_clear_state_removes_file(self, tracker, sample_state):
        tracker.save_state(sample_state)
        state_file = tracker._get_state_file("testdb")
        assert state_file.exists()

        tracker.clear_state("testdb")
        assert not state_file.exists()

    def test_clear_state_removes_cache(self, tracker, sample_state):
        tracker.save_state(sample_state)
        assert "testdb" in tracker._current_states

        tracker.clear_state("testdb")
        assert "testdb" not in tracker._current_states

    def test_clear_state_nonexistent(self, tracker):
        # 파일 없어도 에러 없음
        result = tracker.clear_state("nonexistent")
        assert result is True

    def test_clear_state_returns_true(self, tracker, sample_state):
        tracker.save_state(sample_state)
        assert tracker.clear_state("testdb") is True


# ============================================================
# MigrationStateTracker - update_phase
# ============================================================
class TestUpdatePhase:
    def test_update_phase_success(self, tracker, sample_state):
        tracker.save_state(sample_state)
        result = tracker.update_phase("testdb", MigrationPhase.EXECUTION)
        assert result is True

        tracker._current_states.clear()
        loaded = tracker.load_state("testdb")
        assert loaded.current_phase == MigrationPhase.EXECUTION

    def test_update_phase_no_state(self, tracker):
        result = tracker.update_phase("ghost", MigrationPhase.ANALYSIS)
        assert result is False

    def test_update_phase_to_completed(self, tracker, sample_state):
        tracker.save_state(sample_state)
        tracker.update_phase("testdb", MigrationPhase.COMPLETED)

        tracker._current_states.clear()
        loaded = tracker.load_state("testdb")
        assert loaded.current_phase == MigrationPhase.COMPLETED


# ============================================================
# MigrationStateTracker - mark_step_completed
# ============================================================
class TestMarkStepCompleted:
    def test_mark_step_completed(self, tracker, sample_state):
        tracker.save_state(sample_state)
        result = tracker.mark_step_completed("testdb", 0)
        assert result is True

        tracker._current_states.clear()
        loaded = tracker.load_state("testdb")
        assert 0 in loaded.completed_steps

    def test_mark_step_removes_from_pending(self, tracker, sample_state):
        tracker.save_state(sample_state)
        assert 0 in sample_state.pending_steps

        tracker.mark_step_completed("testdb", 0)
        tracker._current_states.clear()
        loaded = tracker.load_state("testdb")
        assert 0 not in loaded.pending_steps

    def test_mark_step_no_duplicate(self, tracker, sample_state):
        tracker.save_state(sample_state)
        tracker.mark_step_completed("testdb", 0)
        tracker.mark_step_completed("testdb", 0)

        tracker._current_states.clear()
        loaded = tracker.load_state("testdb")
        assert loaded.completed_steps.count(0) == 1

    def test_mark_step_updates_fixed_issues(self, tracker, sample_state):
        tracker.save_state(sample_state)
        tracker.mark_step_completed("testdb", 0)
        tracker.mark_step_completed("testdb", 1)

        tracker._current_states.clear()
        loaded = tracker.load_state("testdb")
        assert loaded.fixed_issues == 2

    def test_mark_step_no_state(self, tracker):
        result = tracker.mark_step_completed("ghost", 0)
        assert result is False


# ============================================================
# MigrationStateTracker - set_error / clear_error
# ============================================================
class TestErrorHandling:
    def test_set_error(self, tracker, sample_state):
        tracker.save_state(sample_state)
        result = tracker.set_error("testdb", "FK violation")
        assert result is True

        tracker._current_states.clear()
        loaded = tracker.load_state("testdb")
        assert loaded.error_message == "FK violation"

    def test_clear_error(self, tracker, sample_state):
        sample_state.error_message = "old error"
        tracker.save_state(sample_state)

        tracker.clear_error("testdb")
        tracker._current_states.clear()
        loaded = tracker.load_state("testdb")
        assert loaded.error_message is None

    def test_set_error_no_state(self, tracker):
        result = tracker.set_error("ghost", "error")
        assert result is False

    def test_clear_error_no_state(self, tracker):
        result = tracker.clear_error("ghost")
        assert result is False


# ============================================================
# MigrationStateTracker - create_state
# ============================================================
class TestCreateState:
    def test_create_state_returns_state(self, tracker):
        state = tracker.create_state("newdb", total_issues=5, pending_steps=[0, 1, 2, 3, 4])
        assert isinstance(state, MigrationState)
        assert state.schema == "newdb"
        assert state.total_issues == 5
        assert state.pending_steps == [0, 1, 2, 3, 4]
        assert state.completed_steps == []
        assert state.fixed_issues == 0

    def test_create_state_saves_file(self, tracker):
        tracker.create_state("newdb", total_issues=3, pending_steps=[0, 1, 2])
        state_file = tracker._get_state_file("newdb")
        assert state_file.exists()

    def test_create_state_starts_at_preflight(self, tracker):
        state = tracker.create_state("newdb", total_issues=1, pending_steps=[0])
        assert state.current_phase == MigrationPhase.PREFLIGHT

    def test_create_state_has_started_at(self, tracker):
        state = tracker.create_state("newdb", total_issues=0, pending_steps=[])
        assert state.started_at != ""
        datetime.fromisoformat(state.started_at)

    def test_create_state_empty_pending(self, tracker):
        state = tracker.create_state("emptydb", total_issues=0, pending_steps=[])
        assert state.pending_steps == []


# ============================================================
# MigrationStateTracker - list_incomplete_migrations
# ============================================================
class TestListIncompleteMigrations:
    def test_list_empty(self, tracker):
        result = tracker.list_incomplete_migrations()
        assert result == []

    def test_list_incomplete_only(self, tracker):
        # 미완료 2개 + 완료 1개
        for schema, phase in [
            ("db1", MigrationPhase.ANALYSIS),
            ("db2", MigrationPhase.EXECUTION),
            ("db3", MigrationPhase.COMPLETED),
        ]:
            state = MigrationState(
                schema=schema,
                started_at=datetime.now().isoformat(),
                current_phase=phase,
            )
            tracker.save_state(state)
            tracker._current_states.clear()

        result = tracker.list_incomplete_migrations()
        schemas = [s.schema for s in result]
        assert "db1" in schemas
        assert "db2" in schemas
        assert "db3" not in schemas

    def test_list_sorted_newest_first(self, tracker):
        import time
        for schema in ["first", "second", "third"]:
            state = MigrationState(
                schema=schema,
                started_at=datetime.now().isoformat(),
                current_phase=MigrationPhase.ANALYSIS,
            )
            tracker.save_state(state)
            tracker._current_states.clear()
            time.sleep(0.01)

        result = tracker.list_incomplete_migrations()
        schemas = [s.schema for s in result]
        assert schemas.index("third") < schemas.index("second") < schemas.index("first")

    def test_list_skips_corrupt_files(self, tracker):
        # 정상 파일 1개 + 손상 파일 1개
        state = MigrationState(
            schema="good",
            started_at=datetime.now().isoformat(),
            current_phase=MigrationPhase.ANALYSIS,
        )
        tracker.save_state(state)
        tracker._current_states.clear()

        bad_file = tracker._state_dir / "migration_state_bad.json"
        bad_file.write_text("!!invalid json!!", encoding="utf-8")

        result = tracker.list_incomplete_migrations()
        assert len(result) == 1
        assert result[0].schema == "good"


# ============================================================
# MigrationStateTracker - rollback SQL
# ============================================================
class TestRollbackSQL:
    def test_set_rollback_sql(self, tracker, sample_state, tmp_path):
        tracker.save_state(sample_state)
        sql_file = tmp_path / "rollback.sql"
        sql_file.write_text("ROLLBACK;")

        result = tracker.set_rollback_sql("testdb", str(sql_file))
        assert result is True

        tracker._current_states.clear()
        loaded = tracker.load_state("testdb")
        assert loaded.rollback_sql_path == str(sql_file)

    def test_get_rollback_sql_existing_file(self, tracker, sample_state, tmp_path):
        sql_file = tmp_path / "rollback.sql"
        sql_file.write_text("ROLLBACK;")

        tracker.save_state(sample_state)
        tracker.set_rollback_sql("testdb", str(sql_file))
        tracker._current_states.clear()

        result = tracker.get_rollback_sql("testdb")
        assert result == str(sql_file)

    def test_get_rollback_sql_missing_file(self, tracker, sample_state):
        tracker.save_state(sample_state)
        tracker.set_rollback_sql("testdb", "/tmp/nonexistent_rollback.sql")
        tracker._current_states.clear()

        result = tracker.get_rollback_sql("testdb")
        assert result is None

    def test_get_rollback_sql_no_state(self, tracker):
        result = tracker.get_rollback_sql("unknown")
        assert result is None

    def test_set_rollback_sql_no_state(self, tracker):
        result = tracker.set_rollback_sql("ghost", "/tmp/rb.sql")
        assert result is False


# ============================================================
# MigrationStateTracker - get_progress
# ============================================================
class TestGetProgress:
    def test_get_progress_no_state(self, tracker):
        result = tracker.get_progress("unknown")
        assert result["exists"] is False
        assert result["progress_percent"] == 0
        assert result["phase"] is None

    def test_get_progress_zero_total(self, tracker):
        state = MigrationState(
            schema="zerotest",
            started_at=datetime.now().isoformat(),
            total_issues=0,
            fixed_issues=0,
        )
        tracker.save_state(state)

        result = tracker.get_progress("zerotest")
        assert result["progress_percent"] == 0

    def test_get_progress_partial(self, tracker):
        state = MigrationState(
            schema="partial",
            started_at=datetime.now().isoformat(),
            current_phase=MigrationPhase.EXECUTION,
            total_issues=10,
            fixed_issues=5,
        )
        tracker.save_state(state)

        result = tracker.get_progress("partial")
        assert result["exists"] is True
        assert result["progress_percent"] == 50.0
        assert result["phase"] == "execution"
        assert result["total_issues"] == 10
        assert result["fixed_issues"] == 5

    def test_get_progress_completed(self, tracker):
        state = MigrationState(
            schema="done",
            started_at=datetime.now().isoformat(),
            current_phase=MigrationPhase.COMPLETED,
            total_issues=8,
            fixed_issues=8,
        )
        tracker.save_state(state)

        result = tracker.get_progress("done")
        assert result["progress_percent"] == 100.0

    def test_get_progress_has_phase_name(self, tracker):
        state = MigrationState(
            schema="phasename",
            started_at=datetime.now().isoformat(),
            current_phase=MigrationPhase.ANALYSIS,
        )
        tracker.save_state(state)

        result = tracker.get_progress("phasename")
        assert "phase_name" in result
        assert result["phase_name"] == "분석"

    def test_get_progress_has_error_flag(self, tracker):
        state = MigrationState(
            schema="erred",
            started_at=datetime.now().isoformat(),
            error_message="kaboom",
        )
        tracker.save_state(state)

        result = tracker.get_progress("erred")
        assert result["has_error"] is True
        assert result["error_message"] == "kaboom"

    def test_get_progress_no_error(self, tracker):
        state = MigrationState(
            schema="clean",
            started_at=datetime.now().isoformat(),
        )
        tracker.save_state(state)

        result = tracker.get_progress("clean")
        assert result["has_error"] is False
        assert result["error_message"] is None

    def test_get_progress_pending_steps_count(self, tracker):
        state = MigrationState(
            schema="pend",
            started_at=datetime.now().isoformat(),
            pending_steps=[0, 1, 2, 3],
        )
        tracker.save_state(state)

        result = tracker.get_progress("pend")
        assert result["pending_steps"] == 4

    def test_get_progress_phase_names_all_phases(self, tracker):
        phase_name_map = {
            MigrationPhase.PREFLIGHT: "사전 검사",
            MigrationPhase.ANALYSIS: "분석",
            MigrationPhase.RECOMMENDATION: "권장 옵션 선택",
            MigrationPhase.EXECUTION: "실행",
            MigrationPhase.VALIDATION: "검증",
            MigrationPhase.COMPLETED: "완료",
        }
        for phase, expected_name in phase_name_map.items():
            state = MigrationState(
                schema=f"phase_{phase}",
                started_at=datetime.now().isoformat(),
                current_phase=phase,
            )
            tracker.save_state(state)
            result = tracker.get_progress(f"phase_{phase}")
            assert result["phase_name"] == expected_name, f"Phase {phase} name mismatch"
            tracker.clear_state(f"phase_{phase}")


# ============================================================
# get_state_tracker 싱글톤 테스트
# ============================================================
class TestGetStateTracker:
    def test_returns_same_instance(self):
        import src.core.migration_state_tracker as module
        # 싱글톤 초기화
        module._tracker_instance = None

        t1 = get_state_tracker()
        t2 = get_state_tracker()
        assert t1 is t2

        # 테스트 후 초기화
        module._tracker_instance = None

    def test_returns_migration_state_tracker(self):
        import src.core.migration_state_tracker as module
        module._tracker_instance = None

        t = get_state_tracker()
        assert isinstance(t, MigrationStateTracker)

        module._tracker_instance = None


# ============================================================
# 미커버 경로 추가 테스트
# ============================================================
class TestUncoveredPaths:
    # --- line 75: Unix 경로 분기 ---
    def test_get_state_dir_unix(self, monkeypatch, tmp_path):
        """os.name != 'nt' 일 때 Unix 경로 사용"""
        monkeypatch.setattr("src.core.migration_state_tracker.os.name", "posix")
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(
            "src.core.migration_state_tracker.Path.home",
            staticmethod(lambda: fake_home)
        )
        t = MigrationStateTracker()
        # Unix 경로: ~/.local/share/TunnelForge/migration_state
        assert ".local" in str(t._state_dir) or str(fake_home) in str(t._state_dir)

    # --- line 114-116: save_state 예외 핸들러 ---
    def test_save_state_exception_returns_false(self, tracker, sample_state):
        """파일 쓰기 실패 시 save_state는 False 반환"""
        from unittest.mock import patch
        with patch("builtins.open", side_effect=PermissionError("denied")):
            result = tracker.save_state(sample_state)
        assert result is False

    # --- line 194-196: clear_state 예외 핸들러 ---
    def test_clear_state_exception_returns_false(self, tracker, sample_state):
        """파일 삭제 실패 시 clear_state는 False 반환"""
        from unittest.mock import patch
        from pathlib import Path
        tracker.save_state(sample_state)
        with patch.object(Path, "unlink", side_effect=PermissionError("cannot delete")):
            result = tracker.clear_state("testdb")
        assert result is False


@pytest.fixture
def sample_state():
    return MigrationState(
        schema="testdb",
        started_at=datetime.now().isoformat(),
        current_phase=MigrationPhase.ANALYSIS,
        pending_steps=[0, 1, 2],
        total_issues=3,
        fixed_issues=0,
    )


@pytest.fixture
def tracker(tmp_path):
    state_dir = tmp_path / "migration_state"
    state_dir.mkdir()
    t = MigrationStateTracker.__new__(MigrationStateTracker)
    t._state_dir = state_dir
    t._current_states = {}
    return t
