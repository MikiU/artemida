from unittest.mock import Mock

import pandas as pd

import analysis as analysis_module
from analysis import (
    Period,
    _brand_query_regex,
    analyze_branded_search,
    analyze_branded_search_period,
)
from config import SiteConfig
from services import gsc_service


def test_brand_query_regex_escapes_terms_and_preserves_phrases():
    assert _brand_query_regex(["fakt.pl", "Onet wiadomości"]) == (
        r"(?i).*(?:fakt\.pl|Onet wiadomości).*"
    )


def test_brand_query_regex_returns_none_for_empty_terms():
    assert _brand_query_regex([" ", ""]) is None


def test_gsc_request_includes_query_filter(monkeypatch):
    service = Mock()
    service.searchanalytics.return_value.query.return_value.execute.return_value = {
        "rows": []
    }
    monkeypatch.setattr(gsc_service, "_build_service", lambda credentials: service)

    gsc_service.get_gsc_data(
        "2026-07-01",
        "2026-07-03",
        "https://www.fakt.pl/",
        "credentials",
        dimensions=("date",),
        use_cache=False,
        query_filter=r"(?i).*(?:fakt\.pl|fakt).*",
    )

    body = service.searchanalytics.return_value.query.call_args.kwargs["body"]
    assert body["dimensionFilterGroups"] == [
        {
            "filters": [
                {
                    "dimension": "query",
                    "operator": "includingRegex",
                    "expression": r"(?i).*(?:fakt\.pl|fakt).*",
                }
            ]
        }
    ]


def test_gsc_cache_separates_query_filters():
    common = ("https://www.fakt.pl/", "2026-07-01", "2026-07-03", ["date"], "web")
    assert gsc_service._cache_path(*common, query_filter="fakt") != (
        gsc_service._cache_path(*common, query_filter="onet")
    )


def test_analyze_branded_search_returns_aligned_daily_series(monkeypatch):
    site = SiteConfig(
        "fakt", "Fakt", "https://www.fakt.pl/", "https://www.fakt.pl/", [],
        brand_terms=["fakt"],
    )
    calls = []

    def fake_get_gsc_data(start, end, prop, credentials, **kwargs):
        calls.append(kwargs)
        clicks = [4, 6] if start == "2026-07-01" else [2, 3]
        return pd.DataFrame({"date": [start, end], "clicks": clicks})

    monkeypatch.setattr(analysis_module, "get_gsc_data", fake_get_gsc_data)
    result = analyze_branded_search(
        site,
        Period("2026-07-01", "2026-07-02"),
        Period("2026-06-01", "2026-06-02"),
        "credentials",
    )

    assert list(result.columns) == ["day", "Current", "Previous"]
    assert list(result["Current"]) == [4, 6]
    assert list(result["Previous"]) == [2, 3]
    assert len(calls) == 2
    assert all(call["search_type"] == "web" for call in calls)
    assert all(call["query_filter"] == r"(?i).*(?:fakt).*" for call in calls)


def test_analyze_branded_search_period_fills_missing_dates(monkeypatch):
    site = SiteConfig(
        "fakt", "Fakt", "https://www.fakt.pl/", "https://www.fakt.pl/", [],
        brand_terms=["fakt"],
    )
    monkeypatch.setattr(
        analysis_module,
        "get_gsc_data",
        lambda *args, **kwargs: pd.DataFrame(
            {"date": ["2026-07-01", "2026-07-03"], "clicks": [8, 5]}
        ),
    )

    result = analyze_branded_search_period(
        site, Period("2026-07-01", "2026-07-03"), "credentials"
    )

    assert list(result["date"]) == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert list(result["clicks"]) == [8, 0, 5]
