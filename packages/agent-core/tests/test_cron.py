"""Cron evaluator tests — field expansion, POSIX day rules, DST, validation."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_core.scheduling.cron import (
    CronError,
    describe_cron,
    next_run_at,
    validate_cron,
)

UTC = timezone.utc


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


class TestFieldExpansion:
    def test_every_minute(self):
        nxt = next_run_at("* * * * *", "UTC", _utc(2026, 3, 4, 10, 30, 45))
        assert nxt == _utc(2026, 3, 4, 10, 31)

    def test_single_value(self):
        assert next_run_at("30 9 * * *", "UTC", _utc(2026, 3, 4, 9, 29)) == _utc(
            2026, 3, 4, 9, 30
        )

    def test_exact_match_is_strictly_after(self):
        # 09:30 already happened — the next one is tomorrow.
        assert next_run_at("30 9 * * *", "UTC", _utc(2026, 3, 4, 9, 30)) == _utc(
            2026, 3, 5, 9, 30
        )

    def test_range(self):
        expr = "0 9-11 * * *"
        assert next_run_at(expr, "UTC", _utc(2026, 3, 4, 9, 1)) == _utc(2026, 3, 4, 10)
        assert next_run_at(expr, "UTC", _utc(2026, 3, 4, 11, 30)) == _utc(2026, 3, 5, 9)

    def test_step_from_star(self):
        assert next_run_at("*/15 * * * *", "UTC", _utc(2026, 3, 4, 10, 31)) == _utc(
            2026, 3, 4, 10, 45
        )

    def test_step_on_range(self):
        assert next_run_at("0-30/10 9 * * *", "UTC", _utc(2026, 3, 4, 9, 11)) == _utc(
            2026, 3, 4, 9, 20
        )

    def test_bare_value_with_step_is_vixie_open_ended(self):
        # ``5/20`` means 5,25,45 in minute field — not just 5.
        assert next_run_at("5/20 * * * *", "UTC", _utc(2026, 3, 4, 10, 5)) == _utc(
            2026, 3, 4, 10, 25
        )

    def test_list(self):
        expr = "0,30 9,18 * * *"
        assert next_run_at(expr, "UTC", _utc(2026, 3, 4, 9, 0)) == _utc(2026, 3, 4, 9, 30)
        assert next_run_at(expr, "UTC", _utc(2026, 3, 4, 9, 30)) == _utc(2026, 3, 4, 18)

    def test_sunday_seven_is_alias(self):
        # 2026-03-08 is a Sunday.
        assert next_run_at("0 0 * * 7", "UTC", _utc(2026, 3, 7, 12)) == _utc(2026, 3, 8)
        assert next_run_at("0 0 * * 0", "UTC", _utc(2026, 3, 7, 12)) == _utc(2026, 3, 8)

    def test_weekday_names_not_supported(self):
        # Documented limitation: numeric day-of-week only.
        assert validate_cron("0 0 * * MON") is not None


class TestDayRules:
    def test_day_of_month(self):
        assert next_run_at("0 3 15 * *", "UTC", _utc(2026, 3, 1)) == _utc(2026, 3, 15, 3)
        assert next_run_at("0 3 15 * *", "UTC", _utc(2026, 3, 16)) == _utc(2026, 4, 15, 3)

    def test_day_of_month_skips_short_months(self):
        # 31 only exists in some months; February is skipped entirely.
        assert next_run_at("0 0 31 * *", "UTC", _utc(2026, 2, 1)) == _utc(2026, 3, 31)

    def test_day_of_week_only(self):
        # Mondays: 2026-03-02, 03-09.
        assert next_run_at("0 8 * * 1", "UTC", _utc(2026, 3, 3)) == _utc(2026, 3, 9, 8)

    def test_dom_and_dow_use_or_semantics(self):
        # "the 1st or any Sunday": from 03-02 the next hit is Sunday 03-08
        # (dow branch), and from 03-30 the next hit is 04-01 (dom branch) —
        # which is a Wednesday, so only the OR rule can produce it.
        expr = "0 0 1 * 0"
        assert next_run_at(expr, "UTC", _utc(2026, 3, 2)) == _utc(2026, 3, 8)
        assert next_run_at(expr, "UTC", _utc(2026, 3, 30)) == _utc(2026, 4, 1)

    def test_star_dom_with_restricted_dow_is_dow_only(self):
        # A literal ``*`` means "unrestricted", not "matches everything" — the
        # OR rule must not fire and turn this into every-day.
        assert next_run_at("0 0 * * 1", "UTC", _utc(2026, 3, 3)) == _utc(2026, 3, 9)

    def test_leap_day(self):
        # 2028 is the next leap year after 2026.
        assert next_run_at("0 0 29 2 *", "UTC", _utc(2026, 3, 1)) == _utc(2028, 2, 29)

    def test_month_restriction(self):
        assert next_run_at("0 0 1 12 *", "UTC", _utc(2026, 3, 1)) == _utc(2026, 12, 1)


class TestTimezones:
    def test_local_time_converted_to_utc(self):
        # Asia/Shanghai is UTC+8 year round.
        nxt = next_run_at("0 9 * * *", "Asia/Shanghai", _utc(2026, 3, 4, 0, 0))
        assert nxt == _utc(2026, 3, 4, 1, 0)

    def test_dst_spring_forward_shifts_utc_offset(self):
        # US DST starts 2026-03-08: 09:00 local is UTC-5 after, UTC-4 before.
        before = next_run_at("0 9 * * *", "America/New_York", _utc(2026, 3, 7, 12))
        after = next_run_at("0 9 * * *", "America/New_York", _utc(2026, 3, 8, 12))
        assert before == _utc(2026, 3, 7, 14, 0)
        assert after == _utc(2026, 3, 8, 13, 0)

    def test_dst_fall_back_keeps_wall_clock(self):
        # US DST ends 2026-11-01: 09:00 local is UTC-5 after the change.
        before = next_run_at("0 9 * * *", "America/New_York", _utc(2026, 10, 31, 12))
        after = next_run_at("0 9 * * *", "America/New_York", _utc(2026, 11, 1, 12))
        assert before == _utc(2026, 10, 31, 13, 0)
        assert after == _utc(2026, 11, 1, 14, 0)

    def test_nonexistent_local_time_still_advances(self):
        # 02:30 does not exist on 2026-03-08 in New York; the schedule must not
        # stall or raise — it lands on the pre-transition offset (07:30 UTC).
        nxt = next_run_at("30 2 * * *", "America/New_York", _utc(2026, 3, 7, 12))
        assert nxt == _utc(2026, 3, 8, 7, 30)

    def test_naive_after_is_treated_as_utc(self):
        nxt = next_run_at("0 9 * * *", "UTC", datetime(2026, 3, 4, 0, 0))
        assert nxt == _utc(2026, 3, 4, 9, 0)

    def test_unknown_timezone(self):
        with pytest.raises(CronError):
            next_run_at("0 9 * * *", "Mars/Olympus", _utc(2026, 3, 4))


class TestValidation:
    @pytest.mark.parametrize(
        "expr",
        [
            "",
            "* * * *",
            "* * * * * *",
            "60 * * * *",
            "* 24 * * *",
            "* * 0 * *",
            "* * 32 * *",
            "* * * 13 *",
            "* * * * 8",
            "*/0 * * * *",
            "*/x * * * *",
            "10-5 * * * *",
            "1,,2 * * * *",
            "a * * * *",
        ],
    )
    def test_invalid_expressions(self, expr):
        assert validate_cron(expr) is not None

    @pytest.mark.parametrize(
        "expr",
        ["* * * * *", "0 9 * * 1-5", "*/15 9-18 * * 1,3,5", "0 0 29 2 *", "0 0 * * 7"],
    )
    def test_valid_expressions(self, expr):
        assert validate_cron(expr) is None

    def test_no_match_within_horizon_raises(self):
        # 30 February never happens.
        with pytest.raises(CronError):
            next_run_at("0 0 30 2 *", "UTC", _utc(2026, 1, 1))


class TestDescribe:
    def test_daily(self):
        assert describe_cron("0 9 * * *") == "at 09:00 every day"

    def test_weekdays(self):
        assert describe_cron("30 8 * * 1-5") == "at 08:30 on Mon,Tue,Wed,Thu,Fri"

    def test_every_minute(self):
        assert describe_cron("* * * * *") == "every minute every day"

    def test_day_of_month_and_month(self):
        text = describe_cron("0 0 1 12 *")
        assert text == "at 00:00 on day-of-month 1 in months 12"

    def test_dom_and_dow_or(self):
        assert describe_cron("0 0 1 * 0") == "at 00:00 on day-of-month 1 or Sun"

    def test_invalid_is_reported_not_raised(self):
        assert describe_cron("nope").startswith("invalid:")
