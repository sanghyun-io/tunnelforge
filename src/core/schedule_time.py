"""예약 실행 시각 계산 (cron 시각 + 로컬 시간대/DST 규칙). Qt 비의존.

cron 은 로컬 벽시계(wall clock) 시각으로 해석한다. DST 전환에서의 규칙:

- 존재하지 않는 시각 (봄에 시계가 건너뛰는 구간, 예: 02:30): 그 날은 **전환 직후 첫 순간**에 한 번 실행한다.
- 중복되는 시각 (가을에 시계가 되돌아가는 구간, 예: 01:30 이 두 번): **첫 번째 발생에서만 한 번** 실행하고
  두 번째 발생(fold=1)은 건너뛴다. 반복 구간에서 같은 일정이 두 번 실행되지 않는다.

시계 규칙은 `Clock` 으로 주입할 수 있다. 기본 `SystemClock` 은 OS 로컬 시간대(DST 포함)를 쓰고,
테스트는 결정적인 `Clock` 구현을 주입한다 (tzdata 없이도 DST 를 검증하기 위해).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import FrozenSet, List, Optional, Tuple

MAX_SEARCH_DAYS = 366


class Clock:
    """벽시계 <-> epoch 변환 규칙."""

    def epoch(self, wall: datetime, fold: int = 0) -> float:
        raise NotImplementedError

    def wall(self, epoch: float) -> datetime:
        """epoch 순간의 로컬 벽시계 (naive, fold 는 두 번째 발생이면 1)."""
        raise NotImplementedError

    def now_epoch(self) -> float:
        import time
        return time.time()


class SystemClock(Clock):
    def epoch(self, wall: datetime, fold: int = 0) -> float:
        return wall.replace(tzinfo=None, fold=fold).timestamp()

    def wall(self, epoch: float) -> datetime:
        return datetime.fromtimestamp(epoch)


SYSTEM_CLOCK = SystemClock()


@dataclass(frozen=True)
class CronSpec:
    minutes: FrozenSet[int]
    hours: FrozenSet[int]
    days: FrozenSet[int]
    months: FrozenSet[int]
    dows: FrozenSet[int]  # cron 요일: 일=0 .. 토=6
    day_restricted: bool
    dow_restricted: bool

    def matches_date(self, wall: datetime) -> bool:
        dow = (wall.weekday() + 1) % 7
        if wall.month not in self.months:
            return False
        day_ok, dow_ok = wall.day in self.days, dow in self.dows
        # Vixie cron: 일/요일이 둘 다 제한되면 둘 중 하나만 맞아도 실행한다.
        if self.day_restricted and self.dow_restricted:
            return day_ok or dow_ok
        return day_ok and dow_ok


def _parse_field(text: str, low: int, high: int, dow: bool = False) -> FrozenSet[int]:
    values = set()
    for part in text.split(','):
        step = 1
        if '/' in part:
            part, step_text = part.split('/', 1)
            step = int(step_text)
            if step < 1:
                raise ValueError('step must be >= 1')
        if part == '*':
            start, end = low, high
        elif '-' in part:
            first, last = part.split('-', 1)
            start, end = int(first), int(last)
        else:
            start = end = int(part)
            if step != 1:
                end = high  # "5/10" 는 5부터 high 까지 10 간격
        if start > end or start < low or end > (7 if dow else high):
            raise ValueError('value out of range')
        for value in range(start, end + 1, step):
            values.add(0 if dow and value == 7 else value)
    return frozenset(v for v in values if low <= v <= high)


def parse_cron(expression: str) -> Optional[CronSpec]:
    """"분 시 일 월 요일" 5필드 cron 을 해석한다. 잘못된 표현식은 None."""
    try:
        parts = (expression or '').strip().split()
        if len(parts) != 5:
            return None
        minute, hour, day, month, dow = parts
        return CronSpec(
            minutes=_parse_field(minute, 0, 59),
            hours=_parse_field(hour, 0, 23),
            days=_parse_field(day, 1, 31),
            months=_parse_field(month, 1, 12),
            dows=_parse_field(dow, 0, 6, dow=True),
            day_restricted=day != '*',
            dow_restricted=dow != '*',
        )
    except (ValueError, TypeError):
        return None


def _next_wall_match(spec: CronSpec, start: datetime) -> Optional[datetime]:
    """start(포함) 이후 spec 에 맞는 가장 빠른 벽시계 시각 (분 단위, naive). 날짜/시간 단위로 건너뛰며 찾는다."""
    current = start.replace(second=0, microsecond=0, tzinfo=None, fold=0)
    limit = current + timedelta(days=MAX_SEARCH_DAYS)
    while current < limit:
        if not spec.matches_date(current):
            current = (current + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if current.hour not in spec.hours:
            current = (current + timedelta(hours=1)).replace(minute=0)
            continue
        if current.minute not in spec.minutes:
            current += timedelta(minutes=1)
            continue
        return current
    return None


def _gap_end_wall(clock: Clock, wall: datetime) -> datetime:
    """존재하지 않는 벽시계 시각 `wall` 의 전환 직후 첫 순간(벽시계)을 찾는다."""
    low = clock.epoch(wall, 0) - 86400.0
    high = clock.epoch(wall, 0) + 86400.0
    # wall(e) 는 단조 증가이며, 전환 직전 순간은 < wall, 전환 직후는 > wall 이다.
    while high - low > 1.0:
        mid = (low + high) / 2.0
        if clock.wall(mid).replace(fold=0) < wall:
            low = mid
        else:
            high = mid
    return clock.wall(high).replace(second=0, microsecond=0, fold=0)


def resolve_wall(clock: Clock, wall: datetime) -> Tuple[float, str]:
    """cron 이 고른 벽시계 시각을 실제 실행 순간(epoch)으로 해석한다.

    반환: (epoch, kind) — kind 는 'normal' | 'gap' (전환 직후로 이동) | 'overlap' (첫 번째 발생).
    """
    first = clock.epoch(wall, 0)
    if clock.wall(first).replace(second=0, microsecond=0, fold=0) != wall:
        end = _gap_end_wall(clock, wall)
        return clock.epoch(end, 0), 'gap'
    second = clock.epoch(wall, 1)
    if second != first and clock.wall(second).replace(second=0, microsecond=0, fold=0) == wall:
        return first, 'overlap'
    return first, 'normal'


def next_occurrence_epoch(spec: CronSpec, after_epoch: float, clock: Clock = SYSTEM_CLOCK) -> Optional[Tuple[float, str]]:
    """after_epoch 보다 엄격히 늦은 다음 실행 순간. 반복(두 번째 발생) 구간은 건너뛴다."""
    cursor = clock.wall(after_epoch).replace(second=0, microsecond=0, fold=0)
    # 반복(두 번째 발생) 구간과 이미 지난 순간은 건너뛰므로 한 구간을 넘길 만큼만 되풀이한다.
    for _ in range(4 * 24 * 60):
        match = _next_wall_match(spec, cursor)
        if match is None:
            return None
        epoch, kind = resolve_wall(clock, match)
        if epoch > after_epoch:
            return epoch, kind
        cursor = match + timedelta(minutes=1)
    return None


def next_occurrence(expression: str, after: Optional[datetime] = None, clock: Clock = SYSTEM_CLOCK) -> Optional[datetime]:
    """cron 표현식의 다음 실행 시각(로컬 벽시계, naive). 잘못된 표현식은 None.

    `CronParser.get_next_run` 이 위임하는 진입점이다.
    """
    spec = parse_cron(expression)
    if spec is None:
        return None
    base = after if after is not None else clock.wall(clock.now_epoch())
    base_epoch = clock.epoch(base, getattr(base, 'fold', 0))
    found = next_occurrence_epoch(spec, base_epoch, clock)
    if found is None:
        return None
    return clock.wall(found[0]).replace(second=0, microsecond=0, fold=0)


def occurrences(expression: str, after_epoch: float, count: int, clock: Clock = SYSTEM_CLOCK) -> List[float]:
    spec = parse_cron(expression)
    result: List[float] = []
    cursor = after_epoch
    while spec is not None and len(result) < count:
        found = next_occurrence_epoch(spec, cursor, clock)
        if found is None:
            break
        result.append(found[0])
        cursor = found[0]
    return result


def minimum_interval_minutes(expression: str, after_epoch: float, clock: Clock = SYSTEM_CLOCK, sample: int = 200) -> Optional[float]:
    """다음 `sample` 번 실행 사이의 최소 간격(분). 실행이 2번 미만이면 None."""
    times = occurrences(expression, after_epoch, sample, clock)
    if len(times) < 2:
        return None
    return min(b - a for a, b in zip(times, times[1:])) / 60.0


def validate_expression(expression: str, min_interval_minutes: int, now_epoch: Optional[float] = None,
                        clock: Clock = SYSTEM_CLOCK) -> Optional[str]:
    """사용자에게 보여줄 오류 문구. 유효하면 None."""
    if parse_cron(expression) is None:
        return '잘못된 cron 표현식입니다. "분 시 일 월 요일" 5개 필드로 입력하세요.'
    now = now_epoch if now_epoch is not None else clock.now_epoch()
    if next_occurrence_epoch(parse_cron(expression), now, clock) is None:
        return '앞으로 1년 안에 실행되는 시각이 없는 표현식입니다.'
    gap = minimum_interval_minutes(expression, now, clock)
    if gap is not None and gap + 1e-9 < min_interval_minutes:
        return f'실행 간격이 너무 짧습니다 (최소 {min_interval_minutes}분, 현재 {gap:.0f}분).'
    return None


# --------------------------------------------------------------------------- 놓친 실행

OVERDUE_GRACE_SECONDS = 120  # 루프 점검 주기(60초)의 2배: 이보다 늦으면 '놓친' 실행으로 본다


def classify_due(next_run_epoch: float, now_epoch: float, grace: float = OVERDUE_GRACE_SECONDS) -> str:
    """'not_due' | 'on_time' | 'missed'. 'missed' 는 절전/앱 미실행 등으로 한참 늦은 실행이다."""
    if next_run_epoch > now_epoch:
        return 'not_due'
    return 'missed' if now_epoch - next_run_epoch > grace else 'on_time'
