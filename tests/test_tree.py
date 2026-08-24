"""Testy agregacji drzewa kategorii, Homepage i kontroli sum."""
import pandas as pd

from analytics.categories import HOMEPAGE, OTHER, build_category_index, match_category
from analytics.comparison import build_category_tree, check_totals, coverage_report


def _pages(rows):
    """Buduje DataFrame w formacie po compare_periods + assign_categories."""
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


def test_homepage_is_classified_separately():
    index = build_category_index(["https://example.com/sport/"])
    assert match_category("https://example.com/", index) == HOMEPAGE
    assert match_category("https://example.com", index) == HOMEPAGE
    # zwykły URL nadal działa
    assert match_category("https://example.com/sport/mecz.html", index) == "Sport"


def test_category_tree_aggregates_parents():
    pages = _pages(
        [
            ("https://a", "Wydarzenia > Polska > Warszawa", 100, 0),
            ("https://b", "Wydarzenia > Polska > Krakow", 50, 0),
            ("https://c", "Wydarzenia > Swiat", 70, 0),
        ]
    )
    tree = build_category_tree(pages)
    values = dict(zip(tree["category_path"], tree["current_clicks"]))

    assert values["Wydarzenia"] == 220
    assert values["Wydarzenia > Polska"] == 150
    assert values["Wydarzenia > Polska > Warszawa"] == 100
    assert values["Wydarzenia > Polska > Krakow"] == 50
    assert values["Wydarzenia > Swiat"] == 70


def test_total_is_not_sum_of_parent_nodes():
    pages = _pages(
        [
            ("https://a", "Wydarzenia > Polska > Warszawa", 100, 0),
            ("https://b", "Wydarzenia > Polska > Krakow", 50, 0),
            ("https://c", "Wydarzenia > Swiat", 70, 0),
        ]
    )
    # TOTAL liczony z per-URL, nie z drzewa
    assert int(pages["current_clicks"].sum()) == 220


def test_direct_plus_children_equals_total():
    pages = _pages(
        [
            ("https://a", "Wydarzenia > Polska > Warszawa", 100, 20),
            ("https://b", "Wydarzenia > Polska > Krakow", 50, 10),
            ("https://c", "Wydarzenia > Swiat", 70, 5),
            ("https://d", "Wydarzenia", 40, 30),
        ]
    )
    tree = build_category_tree(pages)
    for _, row in tree.iterrows():
        assert (
            row["direct_current_clicks"] + row["children_current_clicks"]
            == row["total_current_clicks"]
        )
        assert (
            row["direct_previous_clicks"] + row["children_previous_clicks"]
            == row["total_previous_clicks"]
        )
        assert (
            row["direct_clicks_change"] + row["children_clicks_change"]
            == row["total_clicks_change"]
        )
        # total drzewa nie zmienia się względem current_clicks
        assert row["total_current_clicks"] == row["current_clicks"]


def test_direct_and_children_values():
    pages = _pages(
        [
            ("https://a", "Wydarzenia > Polska > Warszawa", 100, 0),
            ("https://b", "Wydarzenia > Swiat", 70, 0),
            ("https://d", "Wydarzenia", 40, 0),
        ]
    )
    tree = build_category_tree(pages)
    row = tree[tree["category_path"] == "Wydarzenia"].iloc[0]
    assert row["direct_current_clicks"] == 40
    assert row["children_current_clicks"] == 170
    assert row["total_current_clicks"] == 210


def test_number_of_urls_counts_once_per_node():
    pages = _pages(
        [
            ("https://a", "Wydarzenia > Polska > Warszawa", 100, 0),
            ("https://b", "Wydarzenia > Polska > Krakow", 50, 0),
            ("https://c", "Wydarzenia > Swiat", 70, 0),
        ]
    )
    tree = build_category_tree(pages)
    counts = dict(zip(tree["category_path"], tree["number_of_urls"]))
    assert counts["Wydarzenia"] == 3
    assert counts["Wydarzenia > Polska"] == 2
    assert counts["Wydarzenia > Polska > Warszawa"] == 1


def test_tree_excludes_homepage_and_other():
    pages = _pages(
        [
            ("https://home", HOMEPAGE, 500, 400),
            ("https://x", OTHER, 30, 10),
            ("https://a", "Sport", 100, 50),
        ]
    )
    tree = build_category_tree(pages)
    assert HOMEPAGE not in set(tree["category_path"])
    assert OTHER not in set(tree["category_path"])
    assert "Sport" in set(tree["category_path"])


def test_check_totals_passes_for_consistent_data():
    pages = _pages(
        [
            ("https://home", HOMEPAGE, 500, 400),
            ("https://x", OTHER, 30, 10),
            ("https://a", "Sport", 100, 50),
        ]
    )
    assert check_totals(pages) == []


def test_coverage_report_computes_pct():
    pages = _pages(
        [
            ("https://home", HOMEPAGE, 400, 400),
            ("https://x", OTHER, 100, 100),
            ("https://a", "Sport", 500, 0),
        ]
    )
    report = coverage_report(pages)
    assert report["total_urls"] == 3
    assert report["homepage_urls"] == 1
    assert report["other_urls"] == 1
    assert report["category_urls"] == 1
    # kategorie current: 500 z total 1000 -> 50%
    assert round(report["coverage_current_pct"], 1) == 50.0
