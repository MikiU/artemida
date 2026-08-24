"""Testy agregacji i porównania grup kategorii między serwisami."""
import math

import pandas as pd

from analytics.category_groups import (
    aggregate_group,
    build_group_comparison,
    direction_pattern,
    growth_difference_pp,
)
from analytics.comparison import build_category_tree


def _pages(rows):
    data = []
    for page, cat, cur, prev in rows:
        data.append(
            {
                "page": page,
                "current_clicks": cur,
                "previous_clicks": prev,
                "clicks_change": cur - prev,
                "clicks_change_pct": 0.0,
                "current_impressions": cur * 10,
                "previous_impressions": prev * 10,
                "impressions_change": (cur - prev) * 10,
                "impressions_change_pct": 0.0,
                "category_path": cat,
            }
        )
    return pd.DataFrame(data)


def _tree():
    pages = _pages(
        [
            ("https://a", "Wydarzenia > Polska > Warszawa", 100, 50),
            ("https://b", "Wydarzenia > Swiat", 70, 30),
            ("https://c", "Polityka", 50, 40),
        ]
    )
    return build_category_tree(pages)


def test_group_sums_two_top_level_nodes():
    tree = _tree()
    result = aggregate_group(tree, ["Wydarzenia", "Polityka"])
    # Wydarzenia total = 170, Polityka total = 50
    assert result["current_clicks"] == 220
    assert result["previous_clicks"] == 120
    assert result["clicks_change"] == 100
    assert result["missing_paths"] == []


def test_group_does_not_double_count_children():
    tree = _tree()
    # Sama "Wydarzenia" już zawiera dzieci (Polska, Swiat).
    only_parent = aggregate_group(tree, ["Wydarzenia"])
    assert only_parent["current_clicks"] == 170  # 100 + 70, nie 340


def test_missing_path_returns_warning_not_crash():
    tree = _tree()
    result = aggregate_group(tree, ["NieIstnieje"])
    assert result["current_clicks"] == 0
    assert result["missing_paths"] == ["NieIstnieje"]


def test_growth_difference_pp():
    assert growth_difference_pp(25.0, -10.0) == 35.0
    assert math.isnan(growth_difference_pp(math.nan, 5.0))


def test_direction_pattern_variants():
    assert direction_pattern(25, -10) == "site_a_growing_site_b_declining"
    assert direction_pattern(-3, 4) == "site_a_declining_site_b_growing"
    assert direction_pattern(10, 8) == "both_growing"
    assert direction_pattern(-5, -8) == "both_declining"
    assert direction_pattern(0.5, -0.5) == "flat_or_mixed"


def test_build_group_comparison_end_to_end():
    tree_a = _tree()
    tree_b = _pages(
        [
            ("https://x", "Wiadomosci", 90, 100),
        ]
    )
    tree_b = build_category_tree(tree_b)
    comparison = build_group_comparison(
        "news",
        "Wiadomości",
        "fakt",
        "Fakt",
        tree_a,
        ["Wydarzenia", "Polityka"],
        "onet",
        "Onet",
        tree_b,
        ["Wiadomosci"],
    )
    assert comparison["site_a_current_clicks"] == 220
    assert comparison["site_b_current_clicks"] == 90
    assert comparison["direction_pattern"] in {
        "site_a_growing_site_b_declining",
        "both_growing",
        "both_declining",
        "site_a_declining_site_b_growing",
        "flat_or_mixed",
    }
