"""
migration_rules 공통 베이스

세 규칙 클래스(DataIntegrityRules/SchemaRules/StorageRules)가 공유하는
커넥터 보관, 진행 상황 콜백/로깅, 요약 로그, 소스 라인 추출을 한 곳에 모은다.
"""

import re
from typing import Callable, List, Optional, TYPE_CHECKING

from ..migration_constants import CompatibilityIssue

if TYPE_CHECKING:
    from ..db_connector import MySQLConnector


class ProgressLoggingRuleBase:
    """진행 상황 콜백/로깅과 소스 라인 추출을 제공하는 규칙 베이스 클래스"""

    def __init__(self, connector: Optional['MySQLConnector'] = None):
        self.connector = connector
        self._progress_callback: Optional[Callable[[str], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def _log(self, message: str):
        """진행 상황 로깅"""
        if self._progress_callback:
            self._progress_callback(message)

    def _log_summary(
        self,
        issues: List[CompatibilityIssue],
        found_label: str,
        ok_label: str,
        emoji: str = "⚠️",
    ):
        """검사 종료 시 이슈 유무에 따른 요약 로그를 남긴다.

        이슈가 있으면 '  {emoji} {found_label} N개 발견', 없으면
        '  ✅ {ok_label}' 형식으로, 각 검사 메서드 말미에 복붙돼 있던
        'if issues: warn else: success' 패턴을 캡슐화한다.
        """
        if issues:
            self._log(f"  {emoji} {found_label} {len(issues)}개 발견")
        else:
            self._log(f"  ✅ {ok_label}")

    @staticmethod
    def _extract_source_line(content: str, match: 're.Match') -> str:
        """정규식 매치 주변의 소스 한 줄을 추출해 양끝 공백을 제거한다.

        각 검사 메서드에 복붙돼 있던 rfind/find/strip 3줄 스니펫을 한 곳으로
        모은 것으로, 반환된 문자열의 슬라이싱([:80] 등)은 호출부가 담당한다.
        """
        line_start = content.rfind('\n', 0, match.start()) + 1
        line_end = content.find('\n', match.end())
        return content[line_start:line_end].strip()
