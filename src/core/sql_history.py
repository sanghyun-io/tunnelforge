"""
SQL 쿼리 히스토리 관리
- 실행한 쿼리 영구 저장
- 히스토리 조회 (Chunk 지원)
- 히스토리는 절대 삭제 불가 (영구 보관)
- 고급 검색 (키워드, 날짜 범위, 성공/실패)
- 즐겨찾기 기능
"""
import os
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional

from src.core.logger import get_logger
from src.core.platform_paths import sql_history_file

logger = get_logger('sql_history')


def _matches_history_id(entry: Dict[str, Any], history_id: str) -> bool:
    """엔트리가 특정 history_id(UUID 또는 timestamp, 하위 호환성)와 일치하는지 확인"""
    return entry.get('id') == history_id or entry.get('timestamp') == history_id


@dataclass
class HistorySearchFilter:
    """search_advanced의 다중 필터 조건"""
    keyword: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    success_only: Optional[bool] = None
    favorites_only: bool = False


class SQLHistory:
    """SQL 쿼리 히스토리 관리자 (영구 보관)"""

    def __init__(self):
        """히스토리 관리자 초기화"""
        self.history_file = self._get_history_file_path()
        self._ensure_directory()

    def _get_history_file_path(self) -> str:
        """히스토리 파일 경로 반환"""
        return str(sql_history_file())

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
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"히스토리 파일 로드 실패: {e}")
            return []

    def _save_history(self, history: List[Dict[str, Any]]):
        """히스토리 파일 저장"""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump({'history': history}, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"히스토리 저장 오류: {e}")

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
            생성된 히스토리 ID (UUID)
        """
        history = self._load_history()

        history_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        entry = {
            'id': history_id,
            'timestamp': timestamp,
            'query': query,
            'success': success,
            'result_count': result_count,
            'execution_time': execution_time,
            'status': status,
            'is_favorite': False
        }

        if error:
            entry['error'] = error

        # 맨 앞에 추가 (최신순)
        history.insert(0, entry)

        # 영구 보관 - 삭제 없음
        self._save_history(history)
        return history_id

    def update_status(self, history_id: str, new_status: str):
        """
        히스토리 항목의 상태 업데이트

        Args:
            history_id: 히스토리 ID (UUID 또는 timestamp)
            new_status: 새 상태 ('committed', 'rolled_back')
        """
        history = self._load_history()

        for entry in history:
            if _matches_history_id(entry, history_id):
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

        for entry in history:
            if any(_matches_history_id(entry, hid) for hid in history_ids):
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

    def search_advanced(
        self,
        keyword: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        success_only: Optional[bool] = None,
        favorites_only: bool = False,
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        고급 검색 (다중 필터)

        Args:
            keyword: 쿼리 키워드 (대소문자 무시)
            date_from: 시작 날짜
            date_to: 종료 날짜
            success_only: True=성공만, False=실패만, None=전체
            favorites_only: 즐겨찾기만 조회
            limit: 반환할 항목 수
            offset: 시작 위치

        Returns:
            (결과 목록, 전체 결과 수) 튜플
        """
        history = self._load_history()
        filt = HistorySearchFilter(
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            success_only=success_only,
            favorites_only=favorites_only,
        )
        results = self._apply_filters(history, filt)

        total = len(results)
        return results[offset:offset + limit], total

    def _apply_filters(
        self, history: List[Dict[str, Any]], filt: HistorySearchFilter
    ) -> List[Dict[str, Any]]:
        """HistorySearchFilter 조건에 따라 히스토리 목록을 순차 필터링"""
        results = history

        # 키워드 필터
        if filt.keyword:
            keyword_lower = filt.keyword.lower()
            results = [r for r in results
                       if keyword_lower in r.get('query', '').lower()]

        # 날짜 범위 필터
        if filt.date_from:
            results = [r for r in results
                       if self._parse_timestamp(r.get('timestamp', '')) >= filt.date_from]

        if filt.date_to:
            # date_to를 하루의 끝으로 설정 (23:59:59)
            date_to_end = datetime(filt.date_to.year, filt.date_to.month, filt.date_to.day, 23, 59, 59)
            results = [r for r in results
                       if self._parse_timestamp(r.get('timestamp', '')) <= date_to_end]

        # 성공/실패 필터
        if filt.success_only is not None:
            results = [r for r in results if r.get('success') == filt.success_only]

        # 즐겨찾기 필터
        if filt.favorites_only:
            results = [r for r in results if r.get('is_favorite', False)]

        return results

    def _parse_timestamp(self, timestamp_str: str) -> datetime:
        """타임스탬프 문자열을 datetime으로 변환"""
        try:
            return datetime.fromisoformat(timestamp_str)
        except (ValueError, TypeError):
            return datetime.min

    def toggle_favorite(self, history_id: str) -> bool:
        """
        즐겨찾기 토글

        Args:
            history_id: 히스토리 ID (UUID 또는 timestamp)

        Returns:
            새 즐겨찾기 상태
        """
        history = self._load_history()

        for entry in history:
            if _matches_history_id(entry, history_id):
                entry['is_favorite'] = not entry.get('is_favorite', False)
                self._save_history(history)
                return entry['is_favorite']

        return False

    def get_favorites(self, limit: int = 50, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
        """
        즐겨찾기 항목 조회

        Args:
            limit: 반환할 항목 수
            offset: 시작 위치

        Returns:
            (즐겨찾기 목록, 전체 즐겨찾기 수) 튜플
        """
        history = self._load_history()
        favorites = [entry for entry in history if entry.get('is_favorite', False)]
        total = len(favorites)
        return favorites[offset:offset + limit], total

    def get_favorite_count(self) -> int:
        """즐겨찾기 총 개수 반환"""
        history = self._load_history()
        return sum(1 for entry in history if entry.get('is_favorite', False))
