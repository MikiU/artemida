"""Test wspólnego rankingu kategorii dla wielu serwisów (tryb multi)."""
import pandas as pd

from analysis import SiteAnalysis
from analytics.comparison import build_category_tree
from app import _combined_categories_df


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


def _analysis(site_key, site_name, rows):
    pages = _pages(rows)
    tree = build_category_tree(pages)
    return SiteAnalysis(
        site_key=site_key,
        site_name=site_name,
        gsc_property=f"https://{site_key}/",
        pages=pages,
        categories=pd.DataFrame(),
        tree=tree,
        coverage={},
        sum_warnings=[],
        other_urls=pd.DataFrame(),
    )


def test_combined_df_keeps_sites_separate():
    a = _analysis("a", "Serwis A", [("https://a/1", "Sport", 100, 50)])
    b = _analysis("b", "Serwis B", [("https://b/1", "Sport", 30, 60)])
    combined = _combined_categories_df([a, b])

    assert set(combined["site_key"]) == {"a", "b"}
    # Ta sama nazwa kategorii pozostaje osobna dla każdego serwisu (bez matchingu).
    sport_rows = combined[combined["category_path"] == "Sport"]
    assert len(sport_rows) == 2
    a_row = sport_rows[sport_rows["site_key"] == "a"].iloc[0]
    b_row = sport_rows[sport_rows["site_key"] == "b"].iloc[0]
    assert a_row["clicks_change"] == 50
    assert b_row["clicks_change"] == -30


def test_combined_df_includes_all_depths():
    a = _analysis(
        "a",
        "Serwis A",
        [("https://a/1", "Wydarzenia > Polska > Warszawa", 100, 0)],
    )
    combined = _combined_categories_df([a])
    depths = set(combined["depth"])
    assert depths == {1, 2, 3}


def test_combined_df_empty_when_no_trees():
    empty = SiteAnalysis(
        site_key="x",
        site_name="X",
        gsc_property="https://x/",
        pages=pd.DataFrame(),
        categories=pd.DataFrame(),
        tree=pd.DataFrame(),
        coverage={},
        sum_warnings=[],
        other_urls=pd.DataFrame(),
    )
    combined = _combined_categories_df([empty])
    assert combined.empty
