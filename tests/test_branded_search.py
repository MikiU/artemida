from unittest.mock import Mock

from analysis import _brand_query_regex
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
