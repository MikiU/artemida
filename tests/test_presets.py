"""Testy presetów dat (tryb 4)."""
from datetime import date

from app import _compute_preset_periods


TODAY = date(2026, 8, 24)  # end_ref = 2026-08-21 (dziś - 3 dni)


def _days(period):
    return (date.fromisoformat(period.end) - date.fromisoformat(period.start)).days + 1


def test_week_over_week():
    cur, prev = _compute_preset_periods(1, today=TODAY)
    assert cur.end == "2026-08-21"
    assert cur.start == "2026-08-15"
    assert _days(cur) == 7
    assert _days(prev) == 7
    # previous przylega do current
    assert prev.end == "2026-08-14"
    assert prev.start == "2026-08-08"


def test_two_weeks():
    cur, prev = _compute_preset_periods(2, today=TODAY)
    assert _days(cur) == 14
    assert _days(prev) == 14
    assert prev.end == "2026-08-07"


def test_month_30():
    cur, prev = _compute_preset_periods(3, today=TODAY)
    assert _days(cur) == 30
    assert _days(prev) == 30
    assert cur.end == "2026-08-21"
    assert prev.end == "2026-07-22"


def test_week_yoy():
    cur, prev = _compute_preset_periods(4, today=TODAY)
    assert _days(cur) == 7
    assert _days(prev) == 7
    # 52 tygodnie = 364 dni wstecz
    assert prev.start == "2025-08-16"
    assert prev.end == "2025-08-22"


def test_month_yoy():
    cur, prev = _compute_preset_periods(5, today=TODAY)
    assert _days(cur) == 30
    assert _days(prev) == 30


def test_calendar_month():
    cur, prev = _compute_preset_periods(6, today=TODAY)
    # ostatni pełny miesiąc = lipiec, poprzedni = czerwiec
    assert cur.start == "2026-07-01"
    assert cur.end == "2026-07-31"
    assert prev.start == "2026-06-01"
    assert prev.end == "2026-06-30"
