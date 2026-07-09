"""
간단한 Cron 표현식 파서
"""
from datetime import datetime, timedelta
from typing import List, Optional

from src.core.logger import get_logger

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
        """다음 실행 시간 계산

        Args:
            expression: Cron 표현식 "분 시 일 월 요일"
            after: 이 시간 이후의 다음 실행 시간 (기본: 현재)

        Returns:
            다음 실행 datetime 또는 None (파싱 실패 시)
        """
        if after is None:
            after = datetime.now()

        try:
            parts = expression.strip().split()
            if len(parts) != 5:
                logger.warning(f"잘못된 cron 표현식: {expression}")
                return None

            minute_field, hour_field, day_field, month_field, dow_field = parts

            # 최대 1년간 검색
            check_time = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
            end_time = after + timedelta(days=366)

            while check_time < end_time:
                minutes = CronParser.parse_field(minute_field, 0, 59, check_time.minute)
                hours = CronParser.parse_field(hour_field, 0, 23, check_time.hour)
                days = CronParser.parse_field(day_field, 1, 31, check_time.day)
                months = CronParser.parse_field(month_field, 1, 12, check_time.month)
                dows = CronParser.parse_field(dow_field, 0, 6, check_time.weekday(), normalize_dow_7=True)
                # cron에서 0=일요일, Python에서 0=월요일 변환
                # Python weekday(): 월=0, 화=1, ..., 일=6
                # Cron: 일=0, 월=1, ..., 토=6
                python_dow = (check_time.weekday() + 1) % 7

                if (check_time.month in months and
                    check_time.day in days and
                    check_time.hour in hours and
                    check_time.minute in minutes and
                    python_dow in dows):
                    return check_time

                check_time += timedelta(minutes=1)

            return None

        except Exception as e:
            logger.error(f"Cron 파싱 오류: {e}")
            return None

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
