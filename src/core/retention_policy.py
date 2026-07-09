"""
백업/결과 파일 보관 정책
- 개수 제한(retention_count), 기간 제한(retention_days)에 따라 삭제 대상 경로를 선정한다.
- 목록화·삭제 side effect는 caller(스케줄 작업 실행기) 책임이며, 이 모듈은 선정 로직만 담당한다.
"""
from datetime import datetime, timedelta
from typing import List, Tuple


def select_paths_for_retention(
    entries: List[Tuple[str, datetime]],
    retention_days: int,
    retention_count: int,
) -> List[str]:
    """보관 정책에 따라 삭제할 경로 목록을 선정한다

    알고리즘: entries를 timestamp 오름차순 정렬 → now-timedelta(days=retention_days)보다
    오래된 항목을 삭제대상에 추가 → 남은 것 중 retention_count 초과분을 가장 오래된 것부터
    삭제대상에 추가 → 삭제대상 path 리스트 반환.

    Args:
        entries: (path, timestamp) 튜플 목록
        retention_days: 보관 기간 (일)
        retention_count: 보관할 개수

    Returns:
        삭제 대상 path 리스트
    """
    sorted_entries = sorted(entries, key=lambda entry: entry[1])

    now = datetime.now()
    to_delete: List[str] = []

    cutoff = now - timedelta(days=retention_days)
    for path, timestamp in sorted_entries:
        if timestamp < cutoff:
            to_delete.append(path)

    remaining = [entry for entry in sorted_entries if entry[0] not in to_delete]
    if len(remaining) > retention_count:
        excess = len(remaining) - retention_count
        for path, _ in remaining[:excess]:
            to_delete.append(path)

    return to_delete
