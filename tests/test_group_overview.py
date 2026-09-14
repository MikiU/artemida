"""Testy agregacji grupowej: tabela przeglądu i dzienny wykres wielolinowy."""
import math

import pandas as pd

from analysis import SourceAnalysis
from app import _group_daily_by_date_df, _group_overview_df


def _source(current_clicks, previous_clicks, current_impr=0, previous_impr=0, daily=None):
    pages = pd.DataFrame(
        {
            "current_clicks": [current_clicks],
            "previous_clicks": [previous_clicks],
            "current_impressions": [current_impr],
            "previous_impressions": [previous_impr],
        }
    )
    daily_current = (
        pd.DataFrame({"date": [d for d, _ in daily], "clicks": [c for _, c in daily]})
        if daily
        else pd.DataFrame({"date": [], "clicks": []})
    )
    return SourceAnalysis(
        search_type="web",
        pages=pages,
        tree=pd.DataFrame(),
        device=pd.DataFrame(),
        current_clicks=float(current_clicks),
        previous_clicks=float(previous_clicks),
        daily_current=daily_current,
    )


def test_group_overview_sums_selected_sources():
    group = [
        {
            "site_key": "a",
            "site_name": "A",
            "status": "ok",
            "results": {
                "web": _source(100, 80, 1000, 900),
                "discover": _source(50, 40, 500, 400),
                "news": _source(10, 5),
            },
        }
    ]
    df = _group_overview_df(group, ["web", "discover"])
    row = df.iloc[0]
    assert row["current_clicks"] == 150  # news pominięte
    assert row["previous_clicks"] == 120
    assert row["clicks_change"] == 30
    assert row["current_impressions"] == 1500
    assert round(row["clicks_change_pct"], 2) == 25.0


def test_group_overview_handles_zero_previous():
    group = [
        {
            "site_key": "a",
            "site_name": "A",
            "status": "ok",
            "results": {"web": _source(100, 0)},
        }
    ]
    df = _group_overview_df(group, ["web"])
    assert math.isnan(df.iloc[0]["clicks_change_pct"])


def test_group_overview_sorted_biggest_drop_first():
    group = [
        {"site_key": "up", "site_name": "Up", "status": "ok", "results": {"web": _source(100, 50)}},
        {"site_key": "down", "site_name": "Down", "status": "ok", "results": {"web": _source(50, 200)}},
    ]
    df = _group_overview_df(group, ["web"])
    assert list(df["site_name"]) == ["Down", "Up"]


def test_group_overview_marks_status_and_missing_source():
    group = [
        {"site_key": "err", "site_name": "Err", "status": "error", "results": {}},
    ]
    df = _group_overview_df(group, ["web"])
    row = df.iloc[0]
    assert row["status"] == "error"
    assert row["current_clicks"] == 0


def test_group_daily_by_date_pivots_and_sums_sources():
    group = [
        {
            "site_key": "a",
            "site_name": "A",
            "status": "ok",
            "results": {
                "web": _source(0, 0, daily=[("2026-07-01", 10), ("2026-07-02", 20)]),
                "discover": _source(0, 0, daily=[("2026-07-01", 5), ("2026-07-02", 5)]),
            },
        },
        {
            "site_key": "b",
            "site_name": "B",
            "status": "ok",
            "results": {
                "web": _source(0, 0, daily=[("2026-07-02", 100)]),
            },
        },
    ]
    df = _group_daily_by_date_df(group, ["web", "discover"])
    assert list(df.columns) == ["A", "B"]
    assert list(df.index) == ["2026-07-01", "2026-07-02"]
    assert df.loc["2026-07-01", "A"] == 15  # web + discover
    assert df.loc["2026-07-02", "A"] == 25
    assert df.loc["2026-07-01", "B"] == 0  # brak dnia -> 0
    assert df.loc["2026-07-02", "B"] == 100


def test_group_daily_by_date_empty_when_no_series():
    group = [{"site_key": "a", "site_name": "A", "status": "no_data", "results": {}}]
    df = _group_daily_by_date_df(group, ["web"])
    assert df.empty
