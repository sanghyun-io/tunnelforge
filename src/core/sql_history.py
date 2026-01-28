"""
SQL 쿼리 히스토리 관리
- 실행한 쿼리 영구 저장
- 히스토리 조회 (Chunk 지원)
- 히스토리는 절대 삭제 불가 (영구 보관)
"""
import os
import json
from datetime import datetime
from typing import List, Dict, Any, Tuple


class SQLHistory:
    """SQL 쿼리 히스토리 관리자 (영구 보관)"""

    def __init__(self):
        """히스토리 관리자 초기화"""
        self.history_file = self._get_history_file_path()
        self._ensure_directory()

    def _get_history_file_path(self) -> str:
        """히스토리 파일 경로 반환"""
        # Windows: %APPDATA%\Local\TunnelForge\sql_history.json
        if os.name == 'nt':
            base_dir = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'TunnelForge')
        else:
            # Linux/Mac: ~/.tunnelforge
            base_dir = os.path.expanduser('~/.tunnelforge')

        return os.path.join(base_dir, 'sql_history.json')

    def _ensure_directory(self):
        """히스토리 파일 디렉토리 생성"""
        dir_path = os.path.dirname(self.history_file)
        if not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)

    def _load_history(self) -> List[Dict[str, Any]]:
        """히스토리 파일 로드"""
        if not os.path.exists(self.history_file):
            return []

        try:
            with open(self.history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('history', [])
        except (json.JSONDecodeError, IOError):
            return []

    def _save_history(self, history: List[Dict[str, Any]]):
        """히스토리 파일 저장"""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump({'history': history}, f, ensure_ascii=False, indent=2)
        except IOError as e:
            print(f"히스토리 저장 오류: {e}")

    def add_query(self, query: str, success: bool, result_count: int = 0,
                  execution_time: float = 0.0, status: str = 'completed', error: str = None) -> str:
        """
        쿼리 히스토리에 추가 (영구 저장)

        Args:
            query: 실행한 SQL 쿼리
            success: 성공 여부
            result_count: 결과 행 수 (SELECT의 경우) 또는 영향받은 행 수
            execution_time: 실행 시간 (초)
            status: 상태 ('completed', 'pending', 'committed', 'rolled_back', 'error')
            error: 에러 메시지 (실패 시)

        Returns:
            생성된 히스토리 ID (timestamp)
        """
        history = self._load_history()

        timestamp = datetime.now().isoformat()
        entry = {
            'timestamp': timestamp,
            'query': query,
            'success': success,
            'result_count': result_count,
            'execution_time': execution_time,
            'status': status
        }

        if error:
            entry['error'] = error

        # 맨 앞에 추가 (최신순)
        history.insert(0, entry)

        # 영구 보관 - 삭제 없음
        self._save_history(history)
        return timestamp

    def update_status(self, history_id: str, new_status: str):
        """
        히스토리 항목의 상태 업데이트

        Args:
            history_id: 히스토리 ID (timestamp)
            new_status: 새 상태 ('committed', 'rolled_back')
        """
        history = self._load_history()

        for entry in history:
            if entry.get('timestamp') == history_id:
                entry['status'] = new_status
                break

        self._save_history(history)

    def update_status_batch(self, history_ids: List[str], new_status: str):
        """
        여러 히스토리 항목의 상태 일괄 업데이트

        Args:
            history_ids: 히스토리 ID 목록
            new_status: 새 상태
        """
        if not history_ids:
            return

        history = self._load_history()
        ids_set = set(history_ids)

        for entry in history:
            if entry.get('timestamp') in ids_set:
                entry['status'] = new_status

        self._save_history(history)

    def get_history(self, limit: int = 50, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
        """
        히스토리 조회 (Chunk 지원)

        Args:
            limit: 반환할 항목 수
            offset: 시작 위치 (0부터)

        Returns:
            (히스토리 목록, 전체 항목 수) 튜플
        """
        history = self._load_history()
        total_count = len(history)
        return history[offset:offset + limit], total_count

    def search_history(self, keyword: str, limit: int = 50, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
        """
        키워드로 히스토리 검색 (Chunk 지원)

        Args:
            keyword: 검색 키워드
            limit: 반환할 항목 수
            offset: 시작 위치

        Returns:
            (검색 결과 목록, 전체 검색 결과 수) 튜플
        """
        history = self._load_history()
        keyword_lower = keyword.lower()

        results = [
            entry for entry in history
            if keyword_lower in entry.get('query', '').lower()
        ]

        total_count = len(results)
        return results[offset:offset + limit], total_count

    def get_total_count(self) -> int:
        """전체 히스토리 항목 수 반환"""
        history = self._load_history()
        return len(history)

    def get_recent_unique(self, limit: int = 20) -> List[str]:
        """
        최근 실행한 고유 쿼리 목록 (중복 제거)

        Args:
            limit: 최대 반환 항목 수

        Returns:
            쿼리 문자열 목록
        """
        history = self._load_history()
        seen = set()
        unique_queries = []

        for entry in history:
            query = entry.get('query', '').strip()
            if query and query not in seen:
                seen.add(query)
                unique_queries.append(query)
                if len(unique_queries) >= limit:
                    break

        return unique_queries
