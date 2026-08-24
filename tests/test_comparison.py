"""Testy porównania okresów."""
import math

import pandas as pd

from analytics.comparison import aggregate_categories, compare_periods, pct_change


def test_compare_periods_computes_difference():
    current = pd.DataFrame(
        {"page": ["https://a"], "clicks": [120], "impressions": [1000]}
    )
    previous = pd.DataFrame(
        {"page": ["https://a"], "clicks": [100], "impressions": [800]}
    )
    result = compare_periods(current, previous)
    row = result.iloc[0]
    assert row["current_clicks"] == 120
    assert row["previous_clicks"] == 100
    assert row["clicks_change"] == 20
    assert row["clicks_change_pct"] == 20.0
    assert row["impressions_change"] == 200


def test_url_only_in_current_period():
    current = pd.DataFrame(
        {"page": ["https://new"], "clicks": [50], "impressions": [500]}
    )
    previous = pd.DataFrame({"page": [], "clicks": [], "impressions": []})
    result = compare_periods(current, previous)
    row = result[result["page"] == "https://new"].iloc[0]
    assert row["previous_clicks"] == 0
    assert row["clicks_change"] == 50


def test_previous_clicks_zero_does_not_return_infinity():
    # previous == 0, current > 0 -> NaN, nie inf
    assert math.isnan(pct_change(10, 0))
    assert not math.isinf(pct_change(10, 0))
    # oba zero -> 0.0
    assert pct_change(0, 0) == 0.0


def test_aggregate_categories_sums_by_path():
    comparison = pd.DataFrame(
        {
            "page": ["https://a", "https://b"],
            "current_clicks": [10, 30],
            "previous_clicks": [5, 20],
            "current_impressions": [100, 300],
            "previous_impressions": [50, 200],
            "clicks_change": [5, 10],
            "clicks_change_pct": [100.0, 50.0],
            "impressions_change": [50, 100],
            "impressions_change_pct": [100.0, 50.0],
            "category_path": ["Sport", "Sport"],
        }
    )
    agg = aggregate_categories(comparison)
    row = agg.iloc[0]
    assert row["category_path"] == "Sport"
    assert row["current_clicks"] == 40
    assert row["previous_clicks"] == 25
    assert row["clicks_change"] == 15
    assert row["number_of_urls"] == 2
