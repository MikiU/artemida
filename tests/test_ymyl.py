"""Testy YMYL i podziału na źródła (Search/Discover)."""
import math

import pandas as pd

from analytics.comparison import build_category_tree
from analytics.ymyl import is_ymyl, tag_ymyl, ymyl_summary


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


def test_is_ymyl_matches_path_and_children():
    ymyl = ["Polityka", "Pieniadze"]
    assert is_ymyl("Polityka", ymyl)
    assert is_ymyl("Polityka > Wybory", ymyl)
    assert is_ymyl("Pieniadze", ymyl)
    assert not is_ymyl("Kobieta", ymyl)
    # częściowe dopasowanie nazwy nie łapie (granica segmentu)
    assert not is_ymyl("Politykana", ymyl)


def test_is_ymyl_exclusion_has_priority():
    ymyl = ["Wydarzenia"]
    exclude = ["Wydarzenia > Pogoda"]
    # Pogoda pod Wydarzenia (YMYL), ale wykluczona -> nie YMYL
    assert not is_ymyl("Wydarzenia > Pogoda", ymyl, exclude)
    assert not is_ymyl("Wydarzenia > Pogoda > Warszawa", ymyl, exclude)
    # inne dzieci Wydarzenia nadal YMYL
    assert is_ymyl("Wydarzenia > Polska", ymyl, exclude)


def test_tag_ymyl_respects_exclusion():
    tree = build_category_tree(
        _pages(
            [
                ("https://a", "Wydarzenia > Polska", 100, 50),
                ("https://b", "Wydarzenia > Pogoda", 80, 40),
            ]
        )
    )
    tagged = tag_ymyl(tree, ["Wydarzenia"], ["Wydarzenia > Pogoda"])
    flags = dict(zip(tagged["category_path"], tagged["ymyl"]))
    assert flags["Wydarzenia > Polska"] == True  # noqa: E712
    assert flags["Wydarzenia > Pogoda"] == False  # noqa: E712
    # rodzic Wydarzenia (d1) nadal YMYL
    assert flags["Wydarzenia"] == True  # noqa: E712


def test_tag_ymyl_adds_column():
    tree = build_category_tree(
        _pages(
            [
                ("https://a", "Polityka", 100, 50),
                ("https://b", "Kobieta", 20, 30),
            ]
        )
    )
    tagged = tag_ymyl(tree, ["Polityka"])
    flags = dict(zip(tagged["category_path"], tagged["ymyl"]))
    assert flags["Polityka"] is True or flags["Polityka"] == True  # noqa: E712
    assert flags["Kobieta"] == False  # noqa: E712


def test_ymyl_summary_no_double_count():
    # Wydarzenia (YMYL) z dziećmi + Kobieta (non-YMYL)
    tree = build_category_tree(
        _pages(
            [
                ("https://a", "Wydarzenia > Polska", 100, 40),
                ("https://b", "Wydarzenia > Swiat", 60, 20),
                ("https://c", "Kobieta", 30, 50),
            ]
        )
    )
    tagged = tag_ymyl(tree, ["Wydarzenia"])
    summary = ymyl_summary(tagged)
    by_flag = dict(zip(summary["ymyl"], summary["current_clicks"]))
    # tylko depth=1: Wydarzenia=160 (YMYL), Kobieta=30 (non-YMYL); bez podwójnego liczenia dzieci
    assert by_flag[True] == 160
    assert by_flag[False] == 30


def test_ymyl_summary_empty_tree():
    empty = pd.DataFrame(
        columns=["category_path", "depth", "current_clicks", "previous_clicks"]
    )
    result = ymyl_summary(empty)
    assert result.empty
