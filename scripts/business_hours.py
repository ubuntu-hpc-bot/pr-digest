"""Weekday-aware delta math for the PR digest.

A "business hour" here is any hour on a Monday-Friday. We do not
narrow to a 9-17 window — the digest only needs to know whether
roughly a workday has passed, not a precise wall-clock measure.

The threshold the digest uses is 24 business hours, which is
approximately three business days. This avoids flagging
"Friday-afternoon PR" as stale on Monday morning.
"""

from datetime import datetime, timedelta


def business_hours_between(start: datetime, end: datetime) -> float:
    """Return the number of business hours (Mon-Fri) between two datetimes.

    Partial hours are counted fractionally. The result is always >= 0.
    Weekends contribute zero. If end <= start, returns 0.0.
    """
    if end <= start:
        return 0.0

    # Normalize to naive UTC for comparison; both inputs are expected
    # to be timezone-aware UTC from the GitHub API.
    if start.tzinfo is not None:
        start = start.astimezone(tz=None).replace(tzinfo=None)
    if end.tzinfo is not None:
        end = end.astimezone(tz=None).replace(tzinfo=None)

    total = 0.0
    cursor = start
    while cursor < end:
        # weekday(): Mon=0, Sun=6. Skip Sat(5) and Sun(6).
        if cursor.weekday() >= 5:
            # Jump to next Monday 00:00.
            days_to_monday = 7 - cursor.weekday()
            cursor = (cursor + timedelta(days=days_to_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            continue

        # Within a weekday: count hours from cursor to either the end
        # of the calendar day or the end of the range, whichever is
        # sooner.
        next_day = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        segment_end = min(end, next_day)
        total += (segment_end - cursor).total_seconds() / 3600.0
        cursor = segment_end

    return total
