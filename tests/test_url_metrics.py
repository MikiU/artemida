from unittest.mock import Mock

from services import gsc_service


def test_page_filter_is_sent_to_api_and_isolates_cache(monkeypatch):
    service = Mock()
    service.searchanalytics.return_value.query.return_value.execute.return_value = {"rows": []}
    monkeypatch.setattr(gsc_service, "_build_service", lambda credentials: service)
    expression = r"^(?:https://a\.pl/article\?id=1)$"
    gsc_service.get_gsc_data(
        "2026-07-01", "2026-07-02", "sc-domain:a.pl", "unused",
        dimensions=("date",), use_cache=False, page_filter=expression, query_filter="brand",
    )
    filters = service.searchanalytics.return_value.query.call_args.kwargs["body"]["dimensionFilterGroups"][0]["filters"]
    assert filters == [
        {"dimension": "query", "operator": "includingRegex", "expression": "brand"},
        {"dimension": "page", "operator": "includingRegex", "expression": expression},
    ]
    common = ("sc-domain:a.pl", "2026-07-01", "2026-07-02", ["date"], "web")
    assert gsc_service._cache_path(*common) == gsc_service._cache_path(*common, page_filter=None)
    assert gsc_service._cache_path(*common, page_filter=expression) != gsc_service._cache_path(*common)
    assert gsc_service._cache_path(*common, page_filter=expression) != gsc_service._cache_path(*common, page_filter="other")

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


def test_url_ownership_respects_host_property_and_path_boundaries():
    from analytics.dashboard import url_candidates
    from config import SiteConfig

    sites = {
        "a": SiteConfig("a", "A", "sc-domain:a.pl", "https://a.pl/", []),
        "section": SiteConfig("section", "A section", "https://a.pl/news/", "https://a.pl/news/", []),
    }
    assert url_candidates("https://a.pl/news/article?x=1", sites) == ["a", "section"]
    assert url_candidates("https://a.pl/newspaper/article", sites) == ["a"]
    assert url_candidates("https://a.pl.evil.org/news/", sites) == []
    assert url_candidates("https://other.pl/news/", sites) == []
    assert url_candidates("https://user@a.pl/news/", sites) == []


def test_url_filter_batches_are_exact_disjoint_and_bounded():
    import re
    from analytics.dashboard import url_filter_batches

    urls = [f"https://a.pl/article?id={number}" for number in range(10)]
    filters = url_filter_batches(urls + urls, limit=100)
    assert len(filters) > 1
    assert all(len(expression) <= 100 for expression in filters)
    assert all(sum(bool(re.search(expression, url)) for expression in filters) == 1 for url in urls)
    assert not any(re.search(expression, urls[0] + "extra") for expression in filters)
