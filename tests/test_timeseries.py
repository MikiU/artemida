"""Test nakładania dziennego szeregu czasowego (Current vs Previous)."""
import pandas as pd

from analysis import Period, _complete_daily_period, _daily_series


def _daily(dates, clicks):
    return pd.DataFrame(
        {
            "date": dates,
            "clicks": clicks,
            "impressions": [c * 10 for c in clicks],
            "ctr": [0.1] * len(clicks),
            "position": [3.0] * len(clicks),
        }
    )


def test_daily_series_aligns_by_day_index():
    cur = _daily(["2026-07-03", "2026-07-01", "2026-07-02"], [30, 10, 20])
    prev = _daily(["2026-06-01", "2026-06-02", "2026-06-03"], [5, 6, 7])
    result = _daily_series(cur, prev)
    assert list(result["day"]) == [1, 2, 3]
    # sortowanie po dacie: current day1=10, day2=20, day3=30
    assert list(result["Current"]) == [10, 20, 30]
    assert list(result["Previous"]) == [5, 6, 7]


def test_daily_series_handles_missing_period():
    cur = _daily(["2026-07-01", "2026-07-02"], [10, 20])
    prev = pd.DataFrame(columns=["date", "clicks", "impressions", "ctr", "position"])
    result = _daily_series(cur, prev)
    assert list(result["Current"]) == [10, 20]
    assert list(result["Previous"]) == [0, 0]


def test_complete_daily_period_keeps_zero_click_days_in_place():
    data = _daily(["2026-07-01", "2026-07-03"], [8, 5])
    result = _complete_daily_period(data, Period("2026-07-01", "2026-07-03"))
    assert list(result["date"]) == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert list(result["clicks"]) == [8, 0, 5]
