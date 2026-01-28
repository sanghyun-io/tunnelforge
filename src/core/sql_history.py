"""
SQL 쿼리 히스토리 관리
- 실행한 쿼리 저장
- 히스토리 조회/삭제
"""
import os
import json
from datetime import datetime
from typing import List, Dict, Any


class SQLHistory:
    """SQL 쿼리 히스토리 관리자"""

    def __init__(self, max_entries: int = 500):
        """
        Args:
            max_entries: 최대 저장 항목 수 (기본 500개)
        """
        self.max_entries = max_entries
        self.history_file = self._get_history_file_path()
        self._ensure_directory()

    def _get_history_file_path(self) -> str:
        """히스토리 파일 경로 반환"""
        # Windows: %APPDATA%\Local\TunnelDB\sql_history.json
        if os.name == 'nt':
            base_dir = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'TunnelDB')
        else:
            # Linux/Mac: ~/.tunneldb
            base_dir = os.path.expanduser('~/.tunneldb')

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

    def add_query(self, query: str, success: bool, result_count: int = 0, execution_time: float = 0.0):
        """
        쿼리 히스토리에 추가

        Args:
            query: 실행한 SQL 쿼리
            success: 성공 여부
            result_count: 결과 행 수 (SELECT의 경우)
            execution_time: 실행 시간 (초)
        """
        history = self._load_history()

        entry = {
            'timestamp': datetime.now().isoformat(),
            'query': query,
            'success': success,
            'result_count': result_count,
            'execution_time': execution_time
        }

        # 맨 앞에 추가
        history.insert(0, entry)

        # 최대 항목 수 제한
        if len(history) > self.max_entries:
            history = history[:self.max_entries]

        self._save_history(history)

    def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        히스토리 조회

        Args:
            limit: 최대 반환 항목 수

        Returns:
            히스토리 목록 (최신순)
        """
        history = self._load_history()
        return history[:limit]

    def search_history(self, keyword: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        키워드로 히스토리 검색

        Args:
            keyword: 검색 키워드
            limit: 최대 반환 항목 수

        Returns:
            검색 결과 목록
        """
        history = self._load_history()
        keyword_lower = keyword.lower()

        results = [
            entry for entry in history
            if keyword_lower in entry.get('query', '').lower()
        ]

        return results[:limit]

    def clear_history(self):
        """히스토리 전체 삭제"""
        self._save_history([])

    def remove_entry(self, timestamp: str):
        """
        특정 항목 삭제

        Args:
            timestamp: 삭제할 항목의 타임스탬프
        """
        history = self._load_history()
        history = [entry for entry in history if entry.get('timestamp') != timestamp]
        self._save_history(history)

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
