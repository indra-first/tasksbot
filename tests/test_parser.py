"""
Tests for parse_task() and related helpers in bot.py.

All tests mock _now() to a fixed reference point:
  2026-06-29 12:00:00 Europe/Moscow  (Monday)
  -> "tomorrow"    = 2026-06-30
  -> "послезавтра" = 2026-07-01
  -> next Friday   = 2026-07-03
"""
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest

# Allow importing from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

LOCAL_TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=LOCAL_TZ)  # Monday noon


def dt(year, month, day, hour=0, minute=0) -> datetime:
    """Shorthand for creating a timezone-aware datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)


@pytest.fixture(autouse=True)
def fixed_now():
    """Patch _now() to always return the fixed reference time."""
    with patch("bot._now", return_value=NOW):
        yield


# ---------------------------------------------------------------------------
# Russian month names (Bug #1)
# ---------------------------------------------------------------------------

class TestRussianMonthNames:
    def test_day_month_time(self):
        r = bot.parse_task("Звонок врачу 16 июля 10:00")
        assert r is not None
        assert r.deadline == dt(2026, 7, 16, 10, 0)
        assert r.text == "Звонок врачу"

    def test_day_month_with_v_time(self):
        r = bot.parse_task("Встреча 16 июля в 14:30")
        assert r is not None
        assert r.deadline == dt(2026, 7, 16, 14, 30)

    def test_day_month_with_year(self):
        r = bot.parse_task("Отпуск 15 августа 2027 09:00")
        assert r is not None
        assert r.deadline == dt(2027, 8, 15, 9, 0)

    def test_day_month_bare(self):
        """Only date, no time → defaults to 09:00."""
        r = bot.parse_task("Сдать отчёт 10 сентября")
        assert r is not None
        assert r.deadline == dt(2026, 9, 10, 9, 0)

    def test_day_month_bare_hour(self):
        """16 июля в 18 → 18:00."""
        r = bot.parse_task("Позвонить 16 июля в 18")
        assert r is not None
        assert r.deadline == dt(2026, 7, 16, 18, 0)

    def test_past_month_rolls_to_next_year(self):
        """January is in the past → rolled to next year."""
        r = bot.parse_task("Встреча 5 января 10:00")
        assert r is not None
        assert r.deadline.year == 2027
        assert r.deadline.month == 1
        assert r.deadline.day == 5

    def test_future_month_stays_this_year(self):
        r = bot.parse_task("Конференция 1 декабря 09:00")
        assert r is not None
        assert r.deadline.year == 2026
        assert r.deadline.month == 12

    def test_all_months_recognized(self):
        months = [
            ("января", 1), ("февраля", 2), ("марта", 3), ("апреля", 4),
            ("мая", 5), ("июня", 6), ("июля", 7), ("августа", 8),
            ("сентября", 9), ("октября", 10), ("ноября", 11), ("декабря", 12),
        ]
        for name, num in months:
            r = bot.parse_task(f"Задача 15 {name} 10:00")
            assert r is not None, f"Failed for month: {name}"
            if num > 6:  # future months in 2026
                assert r.deadline.month == num
            else:  # past months → 2027
                assert r.deadline.year == 2027
                assert r.deadline.month == num


# ---------------------------------------------------------------------------
# 24-hour bare hour format (Bug #4)
# ---------------------------------------------------------------------------

class TestBareHourFormat:
    def test_v_18_creates_today(self):
        """'в 18' is in the future (now=12:00) → today at 18:00."""
        r = bot.parse_task("Позвонить врачу в 18")
        assert r is not None
        assert r.deadline == dt(2026, 6, 29, 18, 0)

    def test_v_8_past_rolls_to_tomorrow(self):
        """'в 8' is in the past (now=12:00) → tomorrow at 08:00."""
        r = bot.parse_task("Позвонить в 8")
        assert r is not None
        assert r.deadline == dt(2026, 6, 30, 8, 0)

    def test_v_0_midnight(self):
        r = bot.parse_task("Задача в 0")
        assert r is not None
        assert r.deadline.hour == 0

    def test_v_23(self):
        r = bot.parse_task("Напоминание в 23")
        assert r is not None
        assert r.deadline.hour == 23

    def test_zavtra_v_18(self):
        """'завтра в 18' → tomorrow at 18:00."""
        r = bot.parse_task("Встреча завтра в 18")
        assert r is not None
        assert r.deadline == dt(2026, 6, 30, 18, 0)

    def test_poslezavtra_v_20(self):
        """'послезавтра в 20' → day after tomorrow at 20:00."""
        r = bot.parse_task("Позвонить послезавтра в 20")
        assert r is not None
        assert r.deadline == dt(2026, 7, 1, 20, 0)

    def test_ampm_takes_priority_over_bare_hour(self):
        """'в 6 вечера' is matched by _AMPM_RE, not bare-hour."""
        r = bot.parse_task("Ужин в 6 вечера")
        assert r is not None
        assert r.deadline.hour == 18

    def test_colon_format_unchanged(self):
        """'18:00' still works as before."""
        r = bot.parse_task("Позвонить врачу в 18:00")
        assert r is not None
        assert r.deadline.hour == 18
        assert r.deadline.minute == 0


# ---------------------------------------------------------------------------
# Classic date formats
# ---------------------------------------------------------------------------

class TestClassicFormats:
    def test_dd_mm_hh_mm(self):
        r = bot.parse_task("Встреча 15.07 14:00")
        assert r is not None
        assert r.deadline == dt(2026, 7, 15, 14, 0)

    def test_dd_mm_yyyy_hh_mm(self):
        r = bot.parse_task("Встреча 15.07.2027 14:00")
        assert r is not None
        assert r.deadline == dt(2027, 7, 15, 14, 0)

    def test_through_minutes(self):
        r = bot.parse_task("Таблетки через 30 минут")
        assert r is not None
        expected = NOW.replace(second=0, microsecond=0)
        from datetime import timedelta
        assert r.deadline == expected + timedelta(minutes=30)

    def test_through_hours(self):
        from datetime import timedelta
        r = bot.parse_task("Встреча через 2 часа")
        assert r is not None
        assert r.deadline == NOW.replace(second=0, microsecond=0) + timedelta(hours=2)

    def test_tomorrow_hh_mm(self):
        r = bot.parse_task("Звонок завтра 09:00")
        assert r is not None
        assert r.deadline == dt(2026, 6, 30, 9, 0)

    def test_tomorrow_v_hh_mm(self):
        r = bot.parse_task("Звонок завтра в 15:00")
        assert r is not None
        assert r.deadline == dt(2026, 6, 30, 15, 0)

    def test_tomorrow_ampm(self):
        r = bot.parse_task("Купить торт завтра в 6 вечера")
        assert r is not None
        assert r.deadline == dt(2026, 6, 30, 18, 0)

    def test_poslezavtra(self):
        r = bot.parse_task("Отправить файл послезавтра 10:00")
        assert r is not None
        assert r.deadline == dt(2026, 7, 1, 10, 0)

    def test_weekday(self):
        """Next Friday from Monday = 2026-07-03."""
        r = bot.parse_task("Отчёт в пятницу 17:00")
        assert r is not None
        assert r.deadline == dt(2026, 7, 3, 17, 0)

    def test_hh_mm_future(self):
        r = bot.parse_task("Позвонить 15:30")
        assert r is not None
        assert r.deadline == dt(2026, 6, 29, 15, 30)

    def test_hh_mm_past_rolls_to_tomorrow(self):
        r = bot.parse_task("Позвонить 09:00")
        assert r is not None
        assert r.deadline == dt(2026, 6, 30, 9, 0)

    def test_ampm_vecera(self):
        r = bot.parse_task("Ужин в 7 вечера")
        assert r is not None
        assert r.deadline.hour == 19

    def test_ampm_utra(self):
        r = bot.parse_task("Пробежка в 7 утра")
        assert r is not None
        assert r.deadline.hour == 7

    def test_ampm_nochi(self):
        r = bot.parse_task("Созвон в 11 ночи")
        assert r is not None
        assert r.deadline.hour == 23


# ---------------------------------------------------------------------------
# Text extraction (category, priority, recurrence, reminder)
# ---------------------------------------------------------------------------

class TestTextExtraction:
    def test_category_extracted(self):
        r = bot.parse_task("Позвонить маме завтра 10:00 #личное")
        assert r is not None
        assert r.category == "личное"
        assert "#личное" not in r.text

    def test_priority_urgent(self):
        r = bot.parse_task("Сдать отчёт завтра 10:00 срочно")
        assert r is not None
        assert r.priority == 2

    def test_priority_important(self):
        r = bot.parse_task("Встреча завтра 10:00 важно")
        assert r is not None
        assert r.priority == 1

    def test_priority_normal(self):
        r = bot.parse_task("Встреча завтра 10:00")
        assert r is not None
        assert r.priority == 0

    def test_recurrence_daily(self):
        r = bot.parse_task("Таблетки 09:00 каждый день")
        assert r is not None
        assert r.recurrence == "daily"

    def test_recurrence_weekly(self):
        r = bot.parse_task("Планёрка в пятницу 10:00 каждую неделю")
        assert r is not None
        assert r.recurrence == "weekly"

    def test_reminder_relative(self):
        r = bot.parse_task("Встреча завтра 15:00 напомни за 30 минут")
        assert r is not None
        assert r.reminder_minutes == 30

    def test_reminder_at_time(self):
        r = bot.parse_task("Встреча завтра 15:00 напомни в 14:30")
        assert r is not None
        assert r.reminder_minutes == 30

    def test_no_reminder(self):
        r = bot.parse_task("Встреча завтра 15:00")
        assert r is not None
        assert r.reminder_minutes is None

    def test_combined_all_fields(self):
        r = bot.parse_task("Звонок партнёру 16 июля 14:00 #работа срочно напомни за 15 минут каждую неделю")
        assert r is not None
        assert r.category == "работа"
        assert r.priority == 2
        assert r.reminder_minutes == 15
        assert r.recurrence == "weekly"
        assert r.deadline == dt(2026, 7, 16, 14, 0)


# ---------------------------------------------------------------------------
# Edge cases and failure modes
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_no_time_returns_none(self):
        assert bot.parse_task("Позвонить маме") is None

    def test_empty_string_returns_none(self):
        assert bot.parse_task("") is None

    def test_invalid_date_returns_none(self):
        assert bot.parse_task("Встреча 32.13 10:00") is None

    def test_task_text_preserved(self):
        r = bot.parse_task("Позвонить врачу завтра в 10:00")
        assert r is not None
        assert r.text == "Позвонить врачу"

    def test_task_text_preserved_with_month(self):
        r = bot.parse_task("Купить билеты 5 августа 12:00")
        assert r is not None
        assert r.text == "Купить билеты"

    def test_deadline_is_timezone_aware(self):
        r = bot.parse_task("Встреча завтра 10:00")
        assert r is not None
        assert r.deadline.tzinfo is not None

    def test_case_insensitive_month(self):
        r = bot.parse_task("Встреча 10 Июля 14:00")
        assert r is not None
        assert r.deadline.month == 7
