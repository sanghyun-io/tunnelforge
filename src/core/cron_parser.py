"""
간단한 Cron 표현식 파서
"""
from datetime import datetime
from typing import List, Optional

from src.core.logger import get_logger
from src.core.schedule_time import next_occurrence

logger = get_logger(__name__)


class CronParser:
    """간단한 Cron 표현식 파서

    지원 형식: "분 시 일 월 요일"
    예:
        "0 3 * * *"   = 매일 03:00
        "0 0 * * 0"   = 매주 일요일 00:00
        "0 12 1 * *"  = 매월 1일 12:00
        "30 6 * * 1-5" = 평일 06:30
    """

    @staticmethod
    def parse_field(field: str, min_val: int, max_val: int, current: int, normalize_dow_7: bool = False) -> List[int]:
        """크론 필드를 값 목록으로 파싱

        Args:
            normalize_dow_7: 요일 필드에서 7을 0(일요일)으로 취급 (cron 관용 표기 0/7=일요일 모두 허용)
        """
        if field == '*':
            return list(range(min_val, max_val + 1))

        def _normalize(v: int) -> int:
            if normalize_dow_7 and v == 7:
                return 0
            return v

        values = []
        for part in field.split(','):
            # 범위 (예: 1-5)
            if '-' in part:
                start, end = part.split('-')
                values.extend(_normalize(v) for v in range(int(start), int(end) + 1))
            # 간격 (예: */5)
            elif part.startswith('*/'):
                step = int(part[2:])
                values.extend(range(min_val, max_val + 1, step))
            else:
                values.append(_normalize(int(part)))

        return sorted(set(v for v in values if min_val <= v <= max_val))

    @staticmethod
    def get_next_run(expression: str, after: datetime = None) -> Optional[datetime]:
        """다음 실행 시간 계산 (로컬 벽시계, naive)

        DST 전환 규칙(존재하지 않는 시각은 전환 직후 한 번, 반복되는 시각은 첫 번째 발생에서만)은
        src/core/schedule_time.py 가 정의한다.

        Args:
            expression: Cron 표현식 "분 시 일 월 요일"
            after: 이 시간 이후의 다음 실행 시간 (기본: 현재)

        Returns:
            다음 실행 datetime 또는 None (파싱 실패/1년 내 실행 없음)
        """
        try:
            result = next_occurrence(expression, after)
        except Exception as e:
            logger.error(f"Cron 파싱 오류: {e}")
            return None
        if result is None:
            logger.warning(f"잘못되었거나 1년 안에 실행되지 않는 cron 표현식: {expression}")
        return result

    @staticmethod
    def describe(expression: str) -> str:
        """Cron 표현식을 사람이 읽기 쉬운 형태로 변환"""
        try:
            parts = expression.strip().split()
            if len(parts) != 5:
                return expression

            minute, hour, day, month, dow = parts

            # 매일
            if day == '*' and month == '*' and dow == '*':
                if minute == '0' and hour != '*':
                    return f"매일 {hour}:00"
                elif minute != '*' and hour != '*':
                    return f"매일 {hour}:{minute.zfill(2)}"

            # 매주
            dow_names = ['일', '월', '화', '수', '목', '금', '토']
            if day == '*' and month == '*' and dow != '*':
                if dow.isdigit():
                    dow_index = 0 if int(dow) == 7 else int(dow)
                    day_name = dow_names[dow_index]
                    return f"매주 {day_name}요일 {hour}:{minute.zfill(2)}"
                elif dow == '1-5':
                    return f"평일 {hour}:{minute.zfill(2)}"

            # 매월
            if day != '*' and month == '*' and dow == '*':
                return f"매월 {day}일 {hour}:{minute.zfill(2)}"

            return expression

        except Exception:
            return expression
