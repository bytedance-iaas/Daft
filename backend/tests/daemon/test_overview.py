"""The overview's periods (C4 1.10.0): which buckets a ``days`` value gives, in the site's time zone."""
from __future__ import annotations

import datetime as dt

import pytest

from daemon import overview as O

DAY = 24 * 60 * 60 * 1000


def _local(y, m, d, hh=0, mm=0, *, tz=8 * 60) -> int:
    zone = dt.timezone(dt.timedelta(minutes=tz))
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=zone).timestamp()) * 1000


def test_days_end_with_today():
    now = _local(2026, 9, 23, 10, 0)
    kind, spans = O.buckets(now, 8 * 60, 7)
    assert kind == "day" and spans == [(_local(2026, 9, d), f"09-{d}") for d in range(17, 24)]
    kind, spans = O.buckets(now, 8 * 60, 30)
    assert kind == "day" and len(spans) == 30
    assert spans[0] == (_local(2026, 8, 25), "08-25") and spans[-1] == (_local(2026, 9, 23), "09-23")
    assert all(b - a == DAY for (a, _), (b, _) in zip(spans, spans[1:]))


def test_weeks_start_on_monday():
    for now in (_local(2026, 9, 21), _local(2026, 9, 23, 10), _local(2026, 9, 27, 23, 59)):
        kind, spans = O.buckets(now, 8 * 60, 90)                    # Monday .. Sunday
        assert kind == "week" and len(spans) == 13
        assert spans[-1] == (_local(2026, 9, 21), "09-21 周")
        assert spans[0] == (_local(2026, 6, 29), "06-29 周")
        assert all(b - a == 7 * DAY for (a, _), (b, _) in zip(spans, spans[1:]))
    _, spans = O.buckets(_local(2026, 9, 28), 8 * 60, 90)          # the next Monday, 00:00
    assert spans[-1] == (_local(2026, 9, 28), "09-28 周") and spans[0][1] == "07-06 周"


def test_months_are_calendar_months():
    kind, spans = O.buckets(_local(2026, 9, 23, 10), 8 * 60, 365)
    assert kind == "month"
    assert [label for _, label in spans] == [
        "2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
        "2026-06", "2026-07", "2026-08", "2026-09"]
    assert spans[0][0] == _local(2025, 10, 1) and spans[-1][0] == _local(2026, 9, 1)
    # month lengths, across a leap February
    _, spans = O.buckets(_local(2028, 3, 15), 8 * 60, 365)
    starts = [s for s, _ in spans]
    assert (spans[0][1], spans[-1][1]) == ("2027-04", "2028-03")
    assert [(b - a) // DAY for a, b in zip(starts, starts[1:])] == \
        [30, 31, 30, 31, 31, 30, 31, 30, 31, 31, 29]
    _, spans = O.buckets(_local(2027, 1, 1), 8 * 60, 365)          # new year's first minute
    assert (spans[0][1], spans[-1][1]) == ("2026-02", "2027-01")


@pytest.mark.parametrize("tz", [8 * 60, 0, -5 * 60 - 30, 14 * 60, -12 * 60])
def test_buckets_follow_the_site_offset(tz):
    """Local midnight in the site's zone starts every bucket, whatever the UTC offset."""
    for days in (7, 30, 90, 365):
        now = _local(2026, 9, 1, 0, 0, tz=tz)                      # the 1st, 00:00 local
        _, spans = O.buckets(now, tz, days)
        assert spans[-1][0] <= now
        assert spans[-1][1] == {7: "09-01", 30: "09-01", 90: "08-31 周", 365: "2026-09"}[days]
        _, before = O.buckets(now - 1, tz, days)                    # a millisecond earlier
        assert before[-1][1] == {7: "08-31", 30: "08-31", 90: "08-31 周", 365: "2026-08"}[days]
    # one instant, two sites: 2026-08-31 20:00 UTC is September at +08:00, still August at -05:30
    instant = int(dt.datetime(2026, 8, 31, 20, tzinfo=dt.timezone.utc).timestamp()) * 1000
    assert O.buckets(instant, 8 * 60, 365)[1][-1][1] == "2026-09"
    assert O.buckets(instant, -5 * 60 - 30, 365)[1][-1][1] == "2026-08"
    assert O.buckets(instant, -5 * 60 - 30, 365)[1][-1][0] == _local(2026, 8, 1, tz=-5 * 60 - 30)


def test_only_the_four_periods():
    assert sorted(O.RANGES) == [7, 30, 90, 365] and O.DEFAULT_DAYS == 7
    with pytest.raises(KeyError):
        O.buckets(_local(2026, 9, 23), 8 * 60, 14)
