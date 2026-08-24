"""Test nakładania dziennego szeregu czasowego (Current vs Previous)."""
import pandas as pd

from analysis import _daily_series


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
