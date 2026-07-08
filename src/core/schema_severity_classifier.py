"""
스키마 diff 심각도 분류기
"""
import re
from typing import List, Optional, Tuple

from src.core.schema_diff_models import (
    ColumnDiff, DiffSeverity, DiffType, ForeignKeyDiff, IndexDiff,
    SeveritySummary, TableDiff, VersionContext,
)


class SeverityClassifier:
    """비교 결과에 심각도를 부여하는 후처리 레이어"""

    # display width만 다른 integer 계열 타입
    _INTEGER_TYPES = frozenset([
        'tinyint', 'smallint', 'mediumint', 'int', 'integer', 'bigint'
    ])

    def __init__(self, version_context: Optional[VersionContext] = None):
        self.version_context = version_context

    def classify(
        self, diffs: List[TableDiff]
    ) -> Tuple[List[TableDiff], SeveritySummary]:
        """비교 결과에 심각도 부여

        Args:
            diffs: TableDiff 목록

        Returns:
            (심각도 부여된 diffs, SeveritySummary)
        """
        summary = SeveritySummary()

        for table_diff in diffs:
            # 테이블 레벨 심각도
            table_diff.severity = self._classify_table(table_diff)
            if table_diff.severity:
                self._count(summary, table_diff.severity)

            # 컬럼 심각도
            for col_diff in table_diff.column_diffs:
                col_diff.severity = self._classify_column(col_diff)
                if col_diff.severity:
                    self._count(summary, col_diff.severity)

            # 인덱스 심각도
            for idx_diff in table_diff.index_diffs:
                idx_diff.severity = self._classify_index(idx_diff)
                if idx_diff.severity:
                    self._count(summary, idx_diff.severity)

            # FK 심각도
            for fk_diff in table_diff.fk_diffs:
                fk_diff.severity = self._classify_fk(fk_diff)
                if fk_diff.severity:
                    self._count(summary, fk_diff.severity)

        return diffs, summary

    def _count(self, summary: SeveritySummary, severity: DiffSeverity):
        """심각도 카운트 증가"""
        if severity == DiffSeverity.CRITICAL:
            summary.critical += 1
        elif severity == DiffSeverity.WARNING:
            summary.warning += 1
        elif severity == DiffSeverity.INFO:
            summary.info += 1

    def _classify_table(self, diff: TableDiff) -> Optional[DiffSeverity]:
        """테이블 레벨 심각도 분류"""
        if diff.diff_type in (DiffType.ADDED, DiffType.REMOVED):
            return DiffSeverity.CRITICAL
        return None

    def _classify_column(self, diff: ColumnDiff) -> Optional[DiffSeverity]:
        """컬럼 심각도 분류"""
        if diff.diff_type == DiffType.UNCHANGED:
            return None
        if diff.diff_type in (DiffType.ADDED, DiffType.REMOVED):
            return DiffSeverity.CRITICAL

        # MODIFIED: 각 변경 항목별 심각도 판단
        max_severity = None

        for d in diff.differences:
            if d.startswith("타입:"):
                sev = self._classify_type_change(d, diff)
            elif d.startswith("Nullable:"):
                sev = DiffSeverity.WARNING
            elif d.startswith("Default:"):
                sev = DiffSeverity.WARNING
            elif d.startswith("Extra:"):
                sev = self._classify_extra_change(d)
            elif d.startswith("Charset:") or d.startswith("Collation:"):
                sev = DiffSeverity.WARNING
            else:
                sev = DiffSeverity.WARNING

            max_severity = self._max_severity(max_severity, sev)

        return max_severity

    def _classify_type_change(
        self, diff_text: str, col_diff: ColumnDiff
    ) -> DiffSeverity:
        """타입 변경 심각도 판단"""
        src_type = col_diff.source_info.data_type if col_diff.source_info else ""
        tgt_type = col_diff.target_info.data_type if col_diff.target_info else ""

        if self._is_display_width_only_diff(src_type, tgt_type):
            return DiffSeverity.INFO

        # base type이 다르면 Critical
        src_base = re.sub(r'\(.*?\)', '', src_type).strip().lower()
        tgt_base = re.sub(r'\(.*?\)', '', tgt_type).strip().lower()

        if src_base != tgt_base:
            return DiffSeverity.CRITICAL

        # 같은 base type이지만 size/precision이 다른 경우
        return DiffSeverity.WARNING

    def _is_display_width_only_diff(self, src: str, tgt: str) -> bool:
        """int(11) vs int 같은 display width만 다른지 확인"""
        # display width 제거 후 비교
        src_stripped = re.sub(r'\(\d+\)', '', src).strip().lower()
        tgt_stripped = re.sub(r'\(\d+\)', '', tgt).strip().lower()

        if src_stripped != tgt_stripped:
            return False

        # base type이 integer 계열인지 확인
        # "int unsigned" -> "int"
        base_type = src_stripped.split()[0] if src_stripped else ""
        return base_type in self._INTEGER_TYPES

    def _classify_extra_change(self, diff_text: str) -> DiffSeverity:
        """Extra 필드 변경 심각도 판단"""
        diff_lower = diff_text.lower()
        if 'auto_increment' in diff_lower:
            return DiffSeverity.CRITICAL
        return DiffSeverity.WARNING

    def _classify_index(self, diff: IndexDiff) -> Optional[DiffSeverity]:
        """인덱스 심각도 분류"""
        if diff.diff_type == DiffType.UNCHANGED:
            return None

        # 이름만 변경은 Info (내용 동일)
        if diff.diff_type == DiffType.RENAMED:
            return DiffSeverity.INFO

        # PRIMARY KEY 변경은 Critical
        if diff.index_name.upper() == 'PRIMARY':
            return DiffSeverity.CRITICAL

        # 기타 인덱스 변경은 Warning
        return DiffSeverity.WARNING

    def _classify_fk(self, diff: ForeignKeyDiff) -> Optional[DiffSeverity]:
        """FK 심각도 분류"""
        if diff.diff_type == DiffType.UNCHANGED:
            return None

        # 이름만 변경은 Info (내용 동일)
        if diff.diff_type == DiffType.RENAMED:
            return DiffSeverity.INFO

        return DiffSeverity.WARNING

    def _max_severity(
        self,
        current: Optional[DiffSeverity],
        new: Optional[DiffSeverity]
    ) -> Optional[DiffSeverity]:
        """두 심각도 중 더 높은 값 반환"""
        if current is None:
            return new
        if new is None:
            return current

        order = {
            DiffSeverity.CRITICAL: 3,
            DiffSeverity.WARNING: 2,
            DiffSeverity.INFO: 1,
        }
        if order.get(new, 0) > order.get(current, 0):
            return new
        return current
