"""Five-field cron expressions — parsing and next-fire computation.

Stdlib only (`datetime` + `zoneinfo`): the repo ships no cron library and the
platform's schedules are simple (minute/hour/day/month/weekday). The evaluator
scans forward day by day and takes the first matching hh:mm, which keeps rare
expressions (``0 0 29 2 *``) cheap — at most one day-loop iteration per
calendar day, not per minute.

Semantics follow Vixie cron:

- fields are ``minute hour day-of-month month day-of-week``;
- ``*`` ``a`` ``a-b`` ``*/n`` ``a-b/n`` ``a/n`` ``a,b,c`` are supported;
- day-of-week is 0=Sunday .. 6=Saturday, with 7 accepted as Sunday;
- when **both** day-of-month and day-of-week are restricted (neither field is
  literally ``*``) a day matches if **either** matches — POSIX OR semantics.

Times are computed in the task's IANA timezone and returned as UTC-aware
datetimes, matching the ``UTCDateTime`` storage contract. A local wall time
that does not exist on a spring-forward day is treated as its pre-transition
offset, i.e. it fires at the instant the clock passes that reading.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["CronError", "describe_cron", "next_run_at", "validate_cron"]


class CronError(ValueError):
    """Raised for a malformed expression or an unresolvable timezone."""


# (name, min, max) in field order; weekday accepts 7 as an alias for 0.
_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
)

# Four years of days covers every weekday/day-of-month alignment, including
# Feb 29 (the worst case is a Feb-29-only expression scanned from Mar 1).
_MAX_DAYS = 366 * 4 + 1

_WEEKDAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


class _Field:
    """One expanded cron field: its allowed values and whether it is restricted."""

    __slots__ = ("values", "restricted")

    def __init__(self, values: frozenset[int], restricted: bool) -> None:
        self.values = values
        self.restricted = restricted


def _parse_field(
    raw: str, name: str, lo: int, hi: int, *, sunday_alias: bool = False
) -> _Field:
    """Expand one field. A field written as exactly ``*`` is unrestricted —
    that flag drives the POSIX day-of-month/day-of-week OR rule."""
    text = raw.strip()
    if not text:
        raise CronError(f"empty {name} field")

    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty {name} entry in {text!r}")

        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.strip().isdigit() or int(step_text) == 0:
                raise CronError(f"invalid step in {name} field: {raw!r}")
            step = int(step_text)
            part = part.strip()

        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            start = _int_field(start_text, name, lo, hi)
            end = _int_field(end_text, name, lo, hi)
            if start > end:
                raise CronError(f"inverted range in {name} field: {part!r}")
        else:
            start = _int_field(part, name, lo, hi)
            # Vixie treats ``a/n`` as ``a-max/n``; a bare value stays single.
            end = hi if step > 1 else start

        values.update(range(start, end + 1, step))

    if sunday_alias and 7 in values:  # cron accepts 7 as Sunday
        values.discard(7)
        values.add(0)
    if not values:
        raise CronError(f"{name} field matches no value: {raw!r}")
    return _Field(frozenset(values), text != "*")


def _int_field(text: str, name: str, lo: int, hi: int) -> int:
    text = text.strip()
    if not text.isdigit():
        raise CronError(f"invalid {name} value: {text!r}")
    value = int(text)
    if value < lo or value > hi:
        raise CronError(f"{name} value {value} out of range {lo}-{hi}")
    return value


def _parse(expr: str) -> tuple[_Field, _Field, _Field, _Field, _Field]:
    if not isinstance(expr, str):
        raise CronError("cron expression must be a string")
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(
            "cron expression must have 5 fields (minute hour day month weekday), "
            f"got {len(parts)}"
        )
    return tuple(  # type: ignore[return-value]
        _parse_field(raw, name, lo, hi, sunday_alias=(name == "weekday"))
        for raw, (name, lo, hi) in zip(parts, _FIELDS)
    )


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"unknown timezone: {tz!r}") from exc


def _day_matches(day: date, dom: _Field, dow: _Field) -> bool:
    if not dom.restricted and not dow.restricted:
        return True
    # date.weekday() is Mon=0..Sun=6; cron is Sun=0..Sat=6.
    cron_dow = (day.weekday() + 1) % 7
    if not dom.restricted:
        return cron_dow in dow.values
    if not dow.restricted:
        return day.day in dom.values
    return day.day in dom.values or cron_dow in dow.values


def next_run_at(expr: str, tz: str, after: datetime) -> datetime:
    """First fire time strictly after ``after``, as a UTC-aware datetime.

    Missed occurrences are skipped, not caught up: the scan starts at the
    minute after ``after`` regardless of how far in the past the last run was.
    """
    minute, hour, dom, month, dow = _parse(expr)
    zone = _zone(tz)

    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    local_after = after.astimezone(zone)
    start = local_after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    hours = sorted(hour.values)
    minutes = sorted(minute.values)
    day = start.date()

    for _ in range(_MAX_DAYS):
        if day.month not in month.values:
            # Jump straight to the first day of the next month.
            day = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
            continue
        if not _day_matches(day, dom, dow):
            day += timedelta(days=1)
            continue
        for h in hours:
            for m in minutes:
                candidate = datetime.combine(day, time(h, m), tzinfo=zone)
                if candidate > local_after:
                    return candidate.astimezone(timezone.utc)
        day += timedelta(days=1)

    raise CronError(f"no matching time within {_MAX_DAYS} days for {expr!r}")


def validate_cron(expr: str) -> str | None:
    """Return a readable error message, or None when the expression is valid."""
    try:
        _parse(expr)
    except CronError as exc:
        return str(exc)
    return None


def describe_cron(expr: str) -> str:
    """Short English summary for tool output and logs (the UI localizes its own)."""
    try:
        minute, hour, dom, month, dow = _parse(expr)
    except CronError as exc:
        return f"invalid: {exc}"

    if len(minute.values) == 60:
        cadence = (
            "every minute"
            if len(hour.values) == 24
            else "every minute during hours "
            + ", ".join(f"{h:02d}" for h in sorted(hour.values))
        )
    elif len(hour.values) == 24:
        cadence = "every hour at minute " + ", ".join(str(m) for m in sorted(minute.values))
    else:
        cadence = "at " + ", ".join(
            f"{h:02d}:{m:02d}" for h in sorted(hour.values) for m in sorted(minute.values)
        )

    if not dom.restricted and not dow.restricted:
        days = "every day"
    elif not dow.restricted:
        days = "on day-of-month " + ",".join(str(d) for d in sorted(dom.values))
    elif not dom.restricted:
        days = "on " + ",".join(_WEEKDAY_NAMES[d] for d in sorted(dow.values))
    else:
        days = (
            "on day-of-month "
            + ",".join(str(d) for d in sorted(dom.values))
            + " or "
            + ",".join(_WEEKDAY_NAMES[d] for d in sorted(dow.values))
        )

    parts = [cadence, days]
    if len(month.values) != 12:
        parts.append("in months " + ",".join(str(m) for m in sorted(month.values)))
    return " ".join(parts)
