"""Schedule time rules: cron matching, DST gaps and overlaps, minimum interval, missed runs."""
from datetime import datetime, timedelta, timezone

import pytest

from src.core import schedule_time as st
from src.core.cron_parser import CronParser

UTC = timezone.utc


class UsClock(st.Clock):
    """Deterministic US-Eastern-like zone for 2026: EST (-5h) / EDT (-4h).

    DST starts 2026-03-08 02:00 local (07:00 UTC) and ends 2026-11-01 02:00 local EDT (06:00 UTC).
    """
    STD, DST = -5 * 3600, -4 * 3600
    START = datetime(2026, 3, 8, 7, 0, tzinfo=UTC).timestamp()
    END = datetime(2026, 11, 1, 6, 0, tzinfo=UTC).timestamp()

    def offset(self, epoch):
        return self.DST if self.START <= epoch < self.END else self.STD

    def wall(self, epoch):
        value = datetime.fromtimestamp(epoch + self.offset(epoch), UTC).replace(tzinfo=None)
        second_pass = self.END <= epoch < self.END + 3600
        return value.replace(fold=1 if second_pass else 0)

    def epoch(self, wall, fold=0):
        base = wall.replace(tzinfo=UTC, fold=0).timestamp()
        std_epoch, dst_epoch = base - self.STD, base - self.DST
        valid = [e for e in (dst_epoch, std_epoch)
                 if self.wall(e).replace(fold=0) == wall.replace(fold=0)]
        if len(valid) == 2:  # repeated wall time: the earlier epoch is the first occurrence
            return min(valid) if fold == 0 else max(valid)
        if valid:
            return valid[0]
        return std_epoch if fold == 0 else dst_epoch  # nonexistent: Python's fold semantics


CLOCK = UsClock()


def utc(*args):
    return datetime(*args, tzinfo=UTC).timestamp()


def local(epoch):
    return CLOCK.wall(epoch).replace(fold=0)


def next_local(expression, after_epoch):
    spec = st.parse_cron(expression)
    found = st.next_occurrence_epoch(spec, after_epoch, CLOCK)
    return found[0], found[1], local(found[0])


# ------------------------------------------------------------------ cron parsing

@pytest.mark.parametrize("expression", ["", "* * * *", "61 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
                                         "*/0 * * * *", "5-1 * * * *", "a b c d e", "* * * * 8"])
def test_invalid_expressions_are_rejected(expression):
    assert st.parse_cron(expression) is None
    assert CronParser.get_next_run(expression) is None


def test_fields_support_lists_ranges_and_steps():
    spec = st.parse_cron("0,30 8-10/2 1-15/7 */3 1-5")
    assert spec.minutes == {0, 30} and spec.hours == {8, 10} and spec.days == {1, 8, 15}
    assert spec.months == {1, 4, 7, 10} and spec.dows == {1, 2, 3, 4, 5}
    assert st.parse_cron("0 0 * * 7").dows == {0}


def test_day_of_month_and_weekday_are_ored_when_both_are_restricted():
    # 2026-10-01 is a Thursday; "1st of the month OR Monday"
    spec = st.parse_cron("0 9 1 * 1")
    assert spec.matches_date(datetime(2026, 10, 1)) and spec.matches_date(datetime(2026, 10, 5))
    assert not spec.matches_date(datetime(2026, 10, 6))
    only_dow = st.parse_cron("0 9 * * 1")
    assert not only_dow.matches_date(datetime(2026, 10, 1)) and only_dow.matches_date(datetime(2026, 10, 5))


def test_cron_parser_facade_keeps_the_existing_naive_local_contract():
    after = datetime(2025, 1, 1, 2, 0)
    assert CronParser.get_next_run("0 3 * * *", after=after) == datetime(2025, 1, 1, 3, 0)
    assert CronParser.get_next_run("0 3 * * *", after=datetime(2025, 1, 1, 3, 0)) == datetime(2025, 1, 2, 3, 0)
    assert CronParser.get_next_run("0 3 * * 7", after=datetime(2025, 1, 1)).date() == datetime(2025, 1, 5).date()
    assert CronParser.get_next_run("* * * * *").tzinfo is None


def test_leap_day_schedule_is_found_within_the_search_window():
    spec = st.parse_cron("0 0 29 2 *")
    assert st.next_occurrence_epoch(spec, utc(2026, 3, 2, 0, 0), CLOCK) is None  # Feb 29 2028 is beyond the window
    assert st.next_occurrence_epoch(spec, utc(2027, 12, 1, 0, 0), CLOCK) is not None


# ------------------------------------------------------------------ DST

def test_normal_day_runs_at_the_wall_clock_time():
    epoch, kind, wall = next_local("30 2 * * *", utc(2026, 6, 1, 0, 0))
    assert kind == "normal" and wall == datetime(2026, 6, 1, 2, 30)


def test_nonexistent_time_runs_once_right_after_the_gap():
    # 2026-03-08 02:30 does not exist (02:00 -> 03:00). It runs at the first instant after the jump.
    epoch, kind, wall = next_local("30 2 * * *", utc(2026, 3, 8, 0, 0))
    assert kind == "gap" and epoch == UsClock.START and wall == datetime(2026, 3, 8, 3, 0)
    again, kind, wall = next_local("30 2 * * *", epoch)
    assert kind == "normal" and wall == datetime(2026, 3, 9, 2, 30), "the gap day runs only once"


def test_hourly_schedule_through_the_gap_does_not_double_run():
    times = st.occurrences("0 * * * *", utc(2026, 3, 8, 5, 30), 4, CLOCK)  # 00:30 EST
    walls = [local(t) for t in times]
    assert walls == [datetime(2026, 3, 8, 1, 0), datetime(2026, 3, 8, 3, 0), datetime(2026, 3, 8, 4, 0), datetime(2026, 3, 8, 5, 0)]
    assert times[1] == UsClock.START and len(set(times)) == 4


def test_repeated_time_runs_only_at_its_first_occurrence():
    # 2026-11-01 01:30 happens twice; the schedule runs at the first (EDT) one only.
    epoch, kind, wall = next_local("30 1 * * *", utc(2026, 11, 1, 0, 0))
    assert kind == "overlap" and wall == datetime(2026, 11, 1, 1, 30)
    assert epoch == utc(2026, 11, 1, 5, 30), "first occurrence is EDT (UTC-4)"
    again, kind, wall = next_local("30 1 * * *", epoch)
    assert wall == datetime(2026, 11, 2, 1, 30), "the second 01:30 (EST) must not run"


def test_hourly_schedule_through_the_overlap_skips_the_repeated_hour():
    times = st.occurrences("0 * * * *", utc(2026, 11, 1, 3, 30), 5, CLOCK)  # 23:30 EDT the day before
    walls = [local(t) for t in times]
    assert walls[:4] == [datetime(2026, 11, 1, 0, 0), datetime(2026, 11, 1, 1, 0),
                         datetime(2026, 11, 1, 2, 0), datetime(2026, 11, 1, 3, 0)]
    assert times[1] == utc(2026, 11, 1, 5, 0) and times[2] == utc(2026, 11, 1, 7, 0)
    assert all(b > a for a, b in zip(times, times[1:]))


def test_search_started_inside_the_repeated_hour_never_reruns_a_wall_time_that_already_ran():
    second_pass = utc(2026, 11, 1, 6, 10)  # 01:10 EST (second pass)
    assert CLOCK.wall(second_pass).fold == 1
    epoch, kind, wall = next_local("30 1 * * *", second_pass)
    assert wall == datetime(2026, 11, 2, 1, 30)
    epoch, kind, wall = next_local("30 2 * * *", second_pass)
    assert wall == datetime(2026, 11, 1, 2, 30) and epoch == utc(2026, 11, 1, 7, 30)


def test_result_is_always_strictly_after_the_reference_instant():
    for after in (utc(2026, 3, 8, 6, 59), utc(2026, 3, 8, 7, 0), utc(2026, 11, 1, 5, 59), utc(2026, 11, 1, 6, 0), utc(2026, 11, 1, 6, 59)):
        for expression in ("* * * * *", "0 * * * *", "*/15 * * * *", "30 1 * * *", "30 2 * * *"):
            found = st.next_occurrence_epoch(st.parse_cron(expression), after, CLOCK)
            assert found[0] > after, (expression, after)


# ------------------------------------------------------------------ minimum interval

@pytest.mark.parametrize("expression, minimum, ok", [
    ("0 3 * * *", 15, True),
    ("0 * * * *", 15, True),
    ("*/15 * * * *", 15, True),
    ("*/5 * * * *", 15, False),
    ("0,10 * * * *", 15, False),
    ("* * * * *", 15, False),
])
def test_minimum_interval_validation(expression, minimum, ok):
    error = st.validate_expression(expression, minimum, now_epoch=utc(2026, 10, 1, 0, 0), clock=CLOCK)
    assert (error is None) is ok, error
    if not ok:
        assert "너무 짧습니다" in error


def test_validation_messages_for_invalid_and_unreachable_expressions():
    now = utc(2026, 10, 1, 0, 0)
    assert "잘못된 cron" in st.validate_expression("nope", 15, now, CLOCK)
    assert "1년 안에" in st.validate_expression("0 0 31 2 *", 15, now, CLOCK)


def test_hourly_schedule_keeps_its_interval_across_the_dst_days():
    gap = st.minimum_interval_minutes("0 * * * *", utc(2026, 3, 7, 0, 0), CLOCK, sample=60)
    assert gap == 60.0 or gap == 120.0 or gap >= 60.0  # the skipped hour never creates a shorter gap
    assert st.validate_expression("0 * * * *", 60, utc(2026, 3, 7, 0, 0), CLOCK) is None


# ------------------------------------------------------------------ missed runs

def test_due_classification():
    now = 1000.0
    assert st.classify_due(now + 1, now) == "not_due"
    assert st.classify_due(now, now) == "on_time"
    assert st.classify_due(now - st.OVERDUE_GRACE_SECONDS, now) == "on_time"
    assert st.classify_due(now - st.OVERDUE_GRACE_SECONDS - 1, now) == "missed"


def test_system_clock_round_trip_and_default_now():
    clock = st.SYSTEM_CLOCK
    now = clock.now_epoch()
    wall = clock.wall(now)
    assert abs(clock.epoch(wall, wall.fold) - now) < 1.5
    found = st.next_occurrence("* * * * *", None)
    assert found is not None and found > datetime.now() - timedelta(seconds=1)
