"""Testy metryk per URL (tryb 5)."""
import math

import pandas as pd

from analysis import build_url_metrics


def _page_df(rows):
    return pd.DataFrame(rows, columns=["page", "clicks", "impressions", "ctr", "position"])


def test_single_period_lookup():
    current = _page_df(
        [
            ["https://a/1", 100, 1000, 0.10, 3.5],
            ["https://a/2", 50, 800, 0.06, 8.0],
        ]
    )
    urls = ["https://a/1", "https://a/2", "https://a/missing"]
    result = build_url_metrics(urls, current)
    assert list(result["url"]) == urls
    row = result[result["url"] == "https://a/1"].iloc[0]
    assert row["current_clicks"] == 100
    assert row["current_impressions"] == 1000
    # brakujący URL -> 0 klików, pozycja NaN
    miss = result[result["url"] == "https://a/missing"].iloc[0]
    assert miss["current_clicks"] == 0
    assert math.isnan(miss["current_position"])


def test_comparison_two_periods():
    current = _page_df([["https://a/1", 120, 1200, 0.1, 3.0]])
    previous = _page_df([["https://a/1", 100, 1000, 0.1, 4.0]])
    result = build_url_metrics(["https://a/1"], current, previous)
    row = result.iloc[0]
    assert row["clicks_change"] == 20
    assert row["impressions_change"] == 200
    # pozycja: 3.0 - 4.0 = -1.0 (poprawa)
    assert row["position_change"] == -1.0


def test_discover_drops_position():
    current = _page_df([["https://a/1", 500, 9000, 0.05, 0.0]])
    result = build_url_metrics(["https://a/1"], current, with_position=False)
    assert not any("position" in c for c in result.columns)
    assert result.iloc[0]["current_clicks"] == 500


def test_deduplicates_urls():
    current = _page_df([["https://a/1", 10, 100, 0.1, 5.0]])
    result = build_url_metrics(["https://a/1", "https://a/1"], current)
    assert len(result) == 1
