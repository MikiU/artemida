"""Offline contract tests for GSC collection."""

from datetime import date, timedelta
from unittest.mock import Mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from weekly.common import ROOT, Settings, Site, load_settings, week_id, week_start
from weekly.fetch_gsc import GscClient, fetch_site, run


def test_iso_boundaries() -> None:
    assert week_id(week_start(today=date(2021, 1, 4))) == "2020-W53"
    assert week_start("2026-W43", date(2026, 10, 30)) == date(2026, 10, 19)
    assert week_start(today=date(2026, 10, 25)) == date(2026, 10, 12)
    for invalid in ["2026-W54", "2026-43", "../2026", "2026-W44"]:
        with pytest.raises(ValueError):
            week_start(invalid, date(2026, 10, 30))


def test_pagination() -> None:
    service = Mock()
    service.searchanalytics.return_value.query.return_value.execute.side_effect = [
        {"rows": [{"keys": ["page"]}] * 25_000},
        {"rows": [{"keys": ["last"]}]},
    ]
    result = GscClient(service, sleep=Mock()).query("sc-domain:example.com", date(2026, 10, 19), ["page"])
    assert result["row_count"] == 25_001
    bodies = [call.kwargs["body"] for call in service.searchanalytics.return_value.query.call_args_list]
    assert [body["startRow"] for body in bodies] == [0, 25_000]
    assert all(body["dimensions"] == ["page"] for body in bodies)
    assert not result["all_search_rows_guaranteed"]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retry(status: int) -> None:
    service = Mock()
    error = HttpError(httplib2.Response({"status": str(status)}), b'{"error":{}}')
    service.searchanalytics.return_value.query.return_value.execute.side_effect = [error, {}]
    sleep = Mock()
    GscClient(service, sleep=sleep).query("sc-domain:example.com", date(2026, 10, 19), [])
    assert service.searchanalytics.return_value.query.return_value.execute.call_count == 2
    assert sleep.called


def test_permission_error_is_not_retried() -> None:
    service = Mock()
    service.searchanalytics.return_value.query.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": "403"}), b'{"error":{"errors":[{"reason":"forbidden"}]}}'
    )
    with pytest.raises(RuntimeError, match="403"):
        GscClient(service).query("sc-domain:example.com", date(2026, 10, 19), [])
    assert service.searchanalytics.return_value.query.return_value.execute.call_count == 1


def test_five_weeks_separate_dimensions_and_freshness() -> None:
    settings = load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml"))
    client = Mock()
    def query(property_id, start, dimensions, **kwargs):
        return {"rows": [{"keys": [(start + timedelta(days=6)).isoformat()], "clicks": 10,
                          "impressions": 100, "ctr": 0.1, "position": 3}], "aggregation": "byProperty"}
    client.query.side_effect = query
    result = fetch_site(client, settings.sites[0], date(2026, 10, 19), date(2026, 10, 26))
    assert len(result["periods"]) == 5
    assert client.query.call_count == 20
    assert {tuple(call.args[2]) for call in client.query.call_args_list} == {(), ("date",), ("page",), ("query",)}
    assert any("Last two days" in warning for warning in result["warnings"])
    assert result["periods"]["T-4"]["week"] == "2026-W39"


def test_empty_totals_are_not_zero() -> None:
    client = Mock()
    client.query.return_value = {"rows": [], "aggregation": "byProperty"}
    result = fetch_site(client, load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml")).sites[0], date(2026, 10, 19))
    assert result["periods"]["T"]["totals"] is None
    assert len(result["periods"]["T"]["missing_dates"]) == 7
    assert result["periods"]["T"]["coverage"]["effective_end"] is None
    assert client.query.call_count == 5


@pytest.mark.parametrize("day_count", [7, 6, 5, 1])
def test_collection_ends_on_last_available_day(day_count):
    start = date(2026, 10, 26)
    client = Mock()
    def query(property_id, period_start, dimensions, **kwargs):
        if dimensions == ["date"]:
            return {"rows": [{"keys": [(period_start + timedelta(days=index)).isoformat()]}
                             for index in reversed(range(day_count))]}
        if dimensions == []:
            return {"rows": [{"clicks": 100, "impressions": 1000, "ctr": 0.1, "position": 4}],
                    "aggregation": "byProperty"}
        return {"rows": [], "pagination_exhausted": True, "aggregation": "auto"}
    client.query.side_effect = query
    site = load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml")).sites[0]
    result = fetch_site(client, site, start, date(2026, 11, 2))
    period = result["periods"]["T"]
    assert period["end"] == "2026-11-01"
    assert period["coverage"]["effective_end"] == (start + timedelta(days=day_count - 1)).isoformat()
    assert period["coverage"]["available_day_count"] == day_count
    assert period["coverage"]["complete"] is (day_count == 7)
    from weekly.detect_anomalies import analyze
    anomalies = analyze(result, site)
    assert anomalies["data_quality"] == ("AVAILABLE_WITH_GSC_LIMITATIONS" if day_count == 7 else "INCOMPLETE")
    for call in client.query.call_args_list:
        if call.args[2] != ["date"]:
            assert call.kwargs["end"] == call.args[1] + timedelta(days=day_count - 1)


def test_truncated_query_keeps_end_date_across_pagination():
    service = Mock()
    service.searchanalytics.return_value.query.return_value.execute.side_effect = [
        {"rows": [{"keys": ["page"]}] * 25_000}, {"rows": []},
    ]
    GscClient(service, sleep=Mock()).query("sc-domain:example.com", date(2026, 10, 26), ["page"], end=date(2026, 10, 30))
    bodies = [call.kwargs["body"] for call in service.searchanalytics.return_value.query.call_args_list]
    assert [body["endDate"] for body in bodies] == ["2026-10-30", "2026-10-30"]
    assert [body["startRow"] for body in bodies] == [0, 25_000]


def test_configuration_and_placeholder_guard() -> None:
    settings = load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml"))
    assert len(settings.sites) == 11
    placeholder_settings = settings.model_copy(deep=True)
    placeholder_settings.sites[0].placeholder = True
    with pytest.raises(ValueError, match="demo"):
        run(placeholder_settings, date(2026, 10, 19), ROOT)
    payload = settings.model_dump()
    payload["sites"][1]["id"] = payload["sites"][0]["id"]
    with pytest.raises(ValueError, match="unique"):
        Settings.model_validate(payload)


def test_imported_site_configuration() -> None:
    sites = {site.id: site for site in load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml")).sites}
    assert set(sites) == {
        "fakt", "onet", "onet_wiadomosci", "auto-swiat", "kobieta.onet.pl",
        "komputerswiat.pl", "businessinsider.com.pl", "plejada.pl", "kultura.onet.pl",
        "przegladsportowy.onet.pl", "podroze.onet.pl",
    }
    assert sites["fakt"].gsc_property == "https://www.fakt.pl/"
    assert sites["onet_wiadomosci"].gsc_property == "https://wiadomosci.onet.pl/"
    assert sites["businessinsider.com.pl"].category_sitemaps == [
        "https://businessinsider.com.pl/sitemap-service-nodes-index.xml.gz"
    ]
    assert sites["onet"].category_sitemaps == []
    assert sites["podroze.onet.pl"].brand_terms == ["podr\u00f3\u017ce onet", "podr\u00f3\u017ce"]
    for site in sites.values():
        assert not site.placeholder
        assert site.gsc_property == site.base_url
        assert site.sitemap_url is None
        assert site.representative_urls == {"home": site.base_url, "article": None, "category": None}
        assert site.thresholds["clicks"].warn == 0.15
        assert site.thresholds["clicks"].fail == 0.25


@pytest.mark.parametrize("site_id", ["../fakt", "fakt/other", "fakt\\other", "fakt..pl", ".fakt"])
def test_site_ids_cannot_escape_output_directory(site_id: str) -> None:
    payload = load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml")).sites[0].model_dump()
    payload["id"] = site_id
    with pytest.raises(ValueError):
        Site.model_validate(payload)