"""
SQLHistory 단위 테스트
"""
import os
import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock


class TestSQLHistory:
    """SQLHistory 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        """각 테스트 전 임시 디렉토리로 SQLHistory 초기화"""
        self.tmp_path = tmp_path

        # OS별 경로 패치
        if os.name == 'nt':
            self.env_patch = patch.dict(
                os.environ,
                {'LOCALAPPDATA': str(tmp_path)}
            )
        else:
            history_dir = tmp_path / '.tunnelforge'
            history_dir.mkdir(parents=True, exist_ok=True)
            self.env_patch = patch('os.path.expanduser', return_value=str(tmp_path / '.tunnelforge'))

        self.env_patch.start()

        from src.core.sql_history import SQLHistory
        self.history = SQLHistory()
        # tmp_path로 히스토리 파일 경로 재설정
        if os.name == 'nt':
            expected_dir = tmp_path / 'TunnelForge'
        else:
            expected_dir = tmp_path / '.tunnelforge'
        expected_dir.mkdir(parents=True, exist_ok=True)
        self.history.history_file = str(expected_dir / 'sql_history.json')

    def teardown_method(self):
        self.env_patch.stop()

    # ================================================================
    # add_query 테스트
    # ================================================================

    def test_add_query_returns_uuid(self):
        """쿼리 추가 시 UUID 반환"""
        history_id = self.history.add_query('SELECT 1', success=True)
        assert history_id is not None
        assert len(history_id) == 36  # UUID 형식

    def test_add_query_stores_entry(self):
        """쿼리 추가 후 조회 가능"""
        self.history.add_query('SELECT * FROM users', success=True, result_count=10)

        items, total = self.history.get_history(limit=10)
        assert total == 1
        assert items[0]['query'] == 'SELECT * FROM users'
        assert items[0]['success'] is True
        assert items[0]['result_count'] == 10

    def test_add_query_with_error(self):
        """에러 정보 포함 쿼리 추가"""
        self.history.add_query(
            'INVALID SQL',
            success=False,
            error='Syntax error'
        )

        items, _ = self.history.get_history()
        assert items[0]['error'] == 'Syntax error'
        assert items[0]['success'] is False

    def test_add_query_stores_execution_time(self):
        """실행 시간 저장 확인"""
        self.history.add_query('SELECT 1', success=True, execution_time=0.123)

        items, _ = self.history.get_history()
        assert items[0]['execution_time'] == 0.123

    def test_add_query_default_not_favorite(self):
        """기본적으로 즐겨찾기 아님"""
        self.history.add_query('SELECT 1', success=True)

        items, _ = self.history.get_history()
        assert items[0]['is_favorite'] is False

    def test_add_multiple_queries_newest_first(self):
        """여러 쿼리 추가 시 최신순 정렬"""
        self.history.add_query('SELECT 1', success=True)
        self.history.add_query('SELECT 2', success=True)
        self.history.add_query('SELECT 3', success=True)

        items, total = self.history.get_history()
        assert total == 3
        assert items[0]['query'] == 'SELECT 3'
        assert items[2]['query'] == 'SELECT 1'

    # ================================================================
    # get_history 테스트
    # ================================================================

    def test_get_history_empty(self):
        """빈 히스토리 조회"""
        items, total = self.history.get_history()
        assert items == []
        assert total == 0

    def test_get_history_with_limit(self):
        """limit 적용 조회"""
        for i in range(10):
            self.history.add_query(f'SELECT {i}', success=True)

        items, total = self.history.get_history(limit=5)
        assert len(items) == 5
        assert total == 10

    def test_get_history_with_offset(self):
        """offset 적용 조회"""
        for i in range(5):
            self.history.add_query(f'SELECT {i}', success=True)

        items, total = self.history.get_history(limit=10, offset=2)
        assert len(items) == 3
        assert total == 5

    def test_get_total_count(self):
        """전체 항목 수 반환"""
        for i in range(7):
            self.history.add_query(f'SELECT {i}', success=True)

        assert self.history.get_total_count() == 7

    # ================================================================
    # update_status 테스트
    # ================================================================

    def test_update_status_by_id(self):
        """ID로 상태 업데이트"""
        history_id = self.history.add_query('INSERT INTO t VALUES (1)', success=True, status='pending')
        self.history.update_status(history_id, 'committed')

        items, _ = self.history.get_history()
        assert items[0]['status'] == 'committed'

    def test_update_status_nonexistent_id(self):
        """존재하지 않는 ID 상태 업데이트 시 예외 없음"""
        self.history.update_status('nonexistent-id', 'committed')

    def test_update_status_batch(self):
        """여러 항목 상태 일괄 업데이트"""
        id1 = self.history.add_query('INSERT 1', success=True, status='pending')
        id2 = self.history.add_query('INSERT 2', success=True, status='pending')
        id3 = self.history.add_query('INSERT 3', success=True, status='pending')

        self.history.update_status_batch([id1, id2], 'committed')

        items, _ = self.history.get_history()
        id_to_status = {item['id']: item['status'] for item in items}
        assert id_to_status[id1] == 'committed'
        assert id_to_status[id2] == 'committed'
        assert id_to_status[id3] == 'pending'

    def test_update_status_batch_empty_list(self):
        """빈 ID 목록으로 일괄 업데이트 시 변화 없음"""
        id1 = self.history.add_query('INSERT 1', success=True, status='pending')
        self.history.update_status_batch([], 'committed')

        items, _ = self.history.get_history()
        assert items[0]['status'] == 'pending'

    # ================================================================
    # search_history 테스트
    # ================================================================

    def test_search_history_by_keyword(self):
        """키워드로 히스토리 검색"""
        self.history.add_query('SELECT * FROM users', success=True)
        self.history.add_query('SELECT * FROM orders', success=True)
        self.history.add_query('INSERT INTO products VALUES (1)', success=True)

        items, total = self.history.search_history('users')
        assert total == 1
        assert items[0]['query'] == 'SELECT * FROM users'

    def test_search_history_case_insensitive(self):
        """대소문자 무시 검색"""
        self.history.add_query('SELECT * FROM Users', success=True)

        items, total = self.history.search_history('users')
        assert total == 1

    def test_search_history_no_results(self):
        """검색 결과 없음"""
        self.history.add_query('SELECT 1', success=True)

        items, total = self.history.search_history('nonexistent_table_xyz')
        assert total == 0
        assert items == []

    def test_search_history_with_limit(self):
        """검색 결과 limit 적용"""
        for i in range(5):
            self.history.add_query(f'SELECT * FROM users WHERE id = {i}', success=True)

        items, total = self.history.search_history('users', limit=2)
        assert len(items) == 2
        assert total == 5

    # ================================================================
    # get_recent_unique 테스트
    # ================================================================

    def test_get_recent_unique_removes_duplicates(self):
        """중복 쿼리 제거"""
        self.history.add_query('SELECT 1', success=True)
        self.history.add_query('SELECT 2', success=True)
        self.history.add_query('SELECT 1', success=True)  # 중복

        unique = self.history.get_recent_unique()
        assert len(unique) == 2

    def test_get_recent_unique_limit(self):
        """최대 반환 개수 제한"""
        for i in range(10):
            self.history.add_query(f'SELECT {i}', success=True)

        unique = self.history.get_recent_unique(limit=5)
        assert len(unique) == 5

    # ================================================================
    # toggle_favorite 테스트
    # ================================================================

    def test_toggle_favorite_on(self):
        """즐겨찾기 추가"""
        history_id = self.history.add_query('SELECT 1', success=True)
        result = self.history.toggle_favorite(history_id)
        assert result is True

        items, _ = self.history.get_history()
        assert items[0]['is_favorite'] is True

    def test_toggle_favorite_off(self):
        """즐겨찾기 제거 (두 번 토글)"""
        history_id = self.history.add_query('SELECT 1', success=True)
        self.history.toggle_favorite(history_id)  # ON
        result = self.history.toggle_favorite(history_id)  # OFF
        assert result is False

    def test_toggle_favorite_nonexistent_id(self):
        """존재하지 않는 ID 토글 시 False 반환"""
        result = self.history.toggle_favorite('nonexistent-id')
        assert result is False

    # ================================================================
    # get_favorites 테스트
    # ================================================================

    def test_get_favorites_empty(self):
        """즐겨찾기 없을 때 빈 목록"""
        self.history.add_query('SELECT 1', success=True)

        items, total = self.history.get_favorites()
        assert items == []
        assert total == 0

    def test_get_favorites_returns_only_favorites(self):
        """즐겨찾기만 반환"""
        id1 = self.history.add_query('SELECT 1', success=True)
        id2 = self.history.add_query('SELECT 2', success=True)
        self.history.add_query('SELECT 3', success=True)

        self.history.toggle_favorite(id1)
        self.history.toggle_favorite(id2)

        items, total = self.history.get_favorites()
        assert total == 2
        assert all(item['is_favorite'] is True for item in items)

    def test_get_favorite_count(self):
        """즐겨찾기 총 개수"""
        ids = [self.history.add_query(f'SELECT {i}', success=True) for i in range(5)]
        for hid in ids[:3]:
            self.history.toggle_favorite(hid)

        count = self.history.get_favorite_count()
        assert count == 3

    # ================================================================
    # search_advanced 테스트
    # ================================================================

    def test_search_advanced_by_keyword(self):
        """고급 검색 - 키워드 필터"""
        self.history.add_query('SELECT * FROM users', success=True)
        self.history.add_query('SELECT * FROM orders', success=True)

        items, total = self.history.search_advanced(keyword='users')
        assert total == 1

    def test_search_advanced_success_only(self):
        """고급 검색 - 성공만"""
        self.history.add_query('SELECT 1', success=True)
        self.history.add_query('INVALID', success=False)

        items, total = self.history.search_advanced(success_only=True)
        assert total == 1
        assert items[0]['success'] is True

    def test_search_advanced_failure_only(self):
        """고급 검색 - 실패만"""
        self.history.add_query('SELECT 1', success=True)
        self.history.add_query('INVALID', success=False)

        items, total = self.history.search_advanced(success_only=False)
        assert total == 1
        assert items[0]['success'] is False

    def test_search_advanced_favorites_only(self):
        """고급 검색 - 즐겨찾기만"""
        id1 = self.history.add_query('SELECT 1', success=True)
        self.history.add_query('SELECT 2', success=True)
        self.history.toggle_favorite(id1)

        items, total = self.history.search_advanced(favorites_only=True)
        assert total == 1
        assert items[0]['is_favorite'] is True

    def test_search_advanced_combined_filters(self):
        """고급 검색 - 복합 필터"""
        id1 = self.history.add_query('SELECT * FROM users', success=True)
        self.history.add_query('SELECT * FROM orders', success=True)
        self.history.add_query('SELECT * FROM users', success=False)
        self.history.toggle_favorite(id1)

        items, total = self.history.search_advanced(
            keyword='users',
            success_only=True,
            favorites_only=True
        )
        assert total == 1

    def test_search_advanced_date_from(self):
        """고급 검색 - 날짜 시작 필터"""
        self.history.add_query('OLD QUERY', success=True)
        # 현재 쿼리
        self.history.add_query('NEW QUERY', success=True)

        # 오늘부터 시작
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        items, total = self.history.search_advanced(date_from=today)

        # 오늘 추가된 쿼리는 모두 포함되어야 함
        assert total >= 1

    def test_search_advanced_no_filters_returns_all(self):
        """필터 없이 전체 조회"""
        for i in range(3):
            self.history.add_query(f'SELECT {i}', success=True)

        items, total = self.history.search_advanced()
        assert total == 3

    # ================================================================
    # 파일 I/O 테스트
    # ================================================================

    def test_load_history_missing_file_returns_empty(self):
        """히스토리 파일 없을 때 빈 리스트"""
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix='.json', delete=True) as f:
            temp_path = f.name

        # 파일이 삭제된 경로
        self.history.history_file = temp_path
        result = self.history._load_history()
        assert result == []

    def test_load_history_invalid_json_returns_empty(self):
        """손상된 JSON 파일 로드 시 빈 리스트"""
        with open(self.history.history_file, 'w') as f:
            f.write('{invalid json}')

        result = self.history._load_history()
        assert result == []
