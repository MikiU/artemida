"""Offline technical checks using a fully mocked HTTP transport."""

import gzip
import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httplib2
import httpx
import pytest
import respx
from bs4 import BeautifulSoup
from googleapiclient.errors import HttpError
from weekly import tech_checks
from weekly.common import Settings, Site, load_settings, read_json, write_json
from weekly.fetch_gsc import GscClient
from weekly.tech_checks import (
    PSI_ENDPOINT,
    CheckError,
    Crawler,
    InspectionBudget,
    PoliteHttp,
    add_comparison,
    check_inspection,
    check_page,
    check_pagespeed,
    check_sitemaps,
    collect_site,
    news_article_schema,
    parse_cwv,
    same_origin,
    sitemap_entries,
)

BASE = "https://www.fakt.pl"
START = date(2026, 10, 19)
ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://www.fakt.pl/sitemap.xml\n"


def configured_site() -> Site:
    """Use the production configuration shape without live traffic."""
    site = load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml")).sites[0].model_copy(deep=True)
    site.representative_urls = {"home": BASE + "/", "article": BASE + "/article", "category": BASE + "/category"}
    return site


def crawler_for(routes: dict[str, Any], robots: str = ROBOTS) -> tuple[Crawler, list[httpx.Request]]:
    """Build a no-sleep crawler whose every request is captured."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == BASE + "/robots.txt":
            return httpx.Response(200, text=robots)
        value = routes.get(str(request.url), (404, "missing"))
        if isinstance(value, httpx.Response):
            return value
        status, content = value
        return httpx.Response(status, content=content, headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    http = PoliteHttp(client, sleep=Mock(), clock=lambda: 0, retries=0)
    crawler = Crawler(http, BASE + "/")
    crawler.load_robots()
    return crawler, requests


def xml_urls(count: int = 25) -> str:
    """Create a dated article sitemap."""
    return '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
        f"<url><loc>{BASE}/article-{index:02}</loc><lastmod>2026-10-{index + 1:02}</lastmod></url>"
        for index in range(count)
    ) + "</urlset>"


def article_html(canonical: str, extra: str = "") -> str:
    """Produce valid JSON-LD with referenced author and image graph nodes."""
    graph = {"@graph": [
        {"@type": ["Article", "NewsArticle"], "author": {"@id": "#author"}, "image": {"@id": "#image"}, "datePublished": "2026-10-25T10:00:00Z"},
        {"@id": "#author", "@type": "Person", "name": "Test Author"},
        {"@id": "#image", "@type": "ImageObject", "url": BASE + "/image.jpg"},
    ]}
    return f'<html><head><link rel="canonical" href="{canonical}">{extra}<script type="application/ld+json">{json.dumps(graph)}</script></head></html>'


def test_robots_hash_and_disallowed_redirect_target() -> None:
    crawler, requests = crawler_for({
        BASE + "/public": httpx.Response(302, headers={"location": "/private/article"}),
    }, robots="User-agent: *\nDisallow: /private\nCrawl-delay: 3\n")
    assert crawler.robots_result["status"] == "OK"
    assert len(crawler.robots_result["sha256"]) == 64
    assert crawler.http.interval == 3
    with pytest.raises(CheckError, match="robots_disallowed"):
        crawler.get(BASE + "/public")
    assert all("/private" not in request.url.path for request in requests)


@pytest.mark.parametrize("status,allowed", [(404, True), (410, True), (403, False), (503, False)])
def test_robots_failure_policy(status: int, allowed: bool) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    crawler = Crawler(PoliteHttp(client, retries=0), BASE + "/")
    result = crawler.load_robots()
    assert crawler.allowed(BASE + "/article") == allowed
    assert result["status"] == ("WARN" if allowed else "FAIL")


def test_foreign_redirect_is_not_fetched() -> None:
    crawler, requests = crawler_for({BASE + "/article": httpx.Response(301, headers={"location": "https://other.example/article"})})
    with pytest.raises(CheckError, match="foreign_origin"):
        crawler.get(BASE + "/article")
    assert len(requests) == 2
    assert not same_origin("https://user:pass@www.fakt.pl/a", BASE)
    assert not same_origin("file:///a", BASE)


def test_sitemap_index_gzip_dedup_and_latest_twenty() -> None:
    index = f"<sitemapindex><sitemap><loc>{BASE}/articles.xml.gz</loc></sitemap><sitemap><loc>{BASE}/extra.xml</loc></sitemap></sitemapindex>"
    extra = f"<urlset><url><loc>{BASE}/article-00</loc><lastmod>2026-10-30</lastmod></url></urlset>"
    crawler, _ = crawler_for({BASE + "/sitemap.xml": (200, index), BASE + "/articles.xml.gz": (200, gzip.compress(xml_urls().encode())), BASE + "/extra.xml": (200, extra)})
    result, sample = check_sitemaps(crawler, configured_site())
    assert result["status"] == "OK"
    assert result["url_count"] == 25
    assert result["url_count_complete"]
    assert len(sample) == 20
    assert sample[0]["url"] == BASE + "/article-00"
    assert sample[1]["url"] == BASE + "/article-24"
    assert result["lastmod"]["newest"] == "2026-10-30T00:00:00+00:00"


def test_sitemap_budgets_are_partial_not_complete() -> None:
    index = f"<sitemapindex><sitemap><loc>{BASE}/child.xml</loc></sitemap></sitemapindex>"
    crawler, _ = crawler_for({BASE + "/sitemap.xml": (200, index)})
    result, sample = check_sitemaps(crawler, configured_site(), max_documents=1)
    assert result["status"] == "WARN"
    assert result["truncated"] and not result["url_count_complete"]
    assert sample == []
    crawler, _ = crawler_for({BASE + "/sitemap.xml": (200, xml_urls())})
    result, sample = check_sitemaps(crawler, configured_site(), max_urls=2)
    assert result["url_count"] == 2
    assert result["truncated"]
    assert len(sample) == 2


def test_sitemap_missing_dates_and_foreign_entries() -> None:
    content = f"<urlset><url><loc>{BASE}/one</loc><lastmod>invalid</lastmod></url><url><loc>https://other.example/two</loc></url></urlset>"
    crawler, _ = crawler_for({BASE + "/sitemap.xml": (200, content)})
    result, sample = check_sitemaps(crawler, configured_site())
    assert result["status"] == "WARN"
    assert result["lastmod"]["missing_or_invalid_count"] == 1
    assert result["rejected_url_count"] == 1
    assert sample[0]["lastmod"] is None


def test_xml_entities_and_html_sitemaps_are_rejected() -> None:
    malicious = b'<!DOCTYPE a [<!ENTITY x "unsafe">]><urlset><url><loc>&x;</loc></url></urlset>'
    crawler, _ = crawler_for({BASE + "/sitemap.xml": (200, malicious)})
    result, _ = check_sitemaps(crawler, configured_site())
    assert result["status"] == "FAIL"
    with pytest.raises(CheckError, match="not_a_sitemap"):
        sitemap_entries(b"<html></html>")


def test_category_sitemap_fallback_is_not_mislabeled_as_articles() -> None:
    site = configured_site()
    crawler, _ = crawler_for({site.category_sitemaps[0]: (200, xml_urls(1))}, robots="User-agent: *\nAllow: /\n")
    result, _ = check_sitemaps(crawler, site)
    assert result["source"] == "category_fallback"
    assert result["status"] == "WARN"


def test_page_schema_canonical_and_noindex() -> None:
    url = BASE + "/article"
    crawler, _ = crawler_for({url: (200, article_html(url))})
    result = check_page(crawler, {"url": url})
    assert result["status"] == "OK"
    assert result["newsarticle"]["articles"][0]["fields"] == {"author": True, "datePublished": True, "image": True}
    crawler, _ = crawler_for({url: (200, article_html(url, '<meta name="robots" content="noindex,follow">'))})
    result = check_page(crawler, {"url": url})
    assert result["status"] == "FAIL" and result["noindex"]
    crawler, _ = crawler_for({url: httpx.Response(200, text=article_html(url), headers={"content-type": "text/html", "x-robots-tag": "googlebot: noindex"})})
    assert check_page(crawler, {"url": url})["status"] == "FAIL"


def test_missing_schema_and_bad_canonical_are_warn() -> None:
    url = BASE + "/article"
    crawler, _ = crawler_for({url: (200, '<html><link rel="canonical" href="/other"></html>')})
    result = check_page(crawler, {"url": url})
    assert result["status"] == "WARN"
    assert "canonical_points_elsewhere" in result["issues"]
    assert not result["newsarticle"]["present"]


def test_microdata_and_malformed_jsonld() -> None:
    soup = BeautifulSoup('<div itemscope itemtype="https://schema.org/NewsArticle"><span itemprop="author">Author</span><time itemprop="datePublished" datetime="2026-10-25"></time><img itemprop="image" src="image.jpg"></div>', "html.parser")
    assert news_article_schema(soup)["status"] == "OK"
    soup = BeautifulSoup('<script type="application/ld+json">{broken</script>', "html.parser")
    assert news_article_schema(soup)["invalid_jsonld_script_count"] == 1


def test_invalid_jsonld_reference_is_not_a_crash() -> None:
    soup = BeautifulSoup('<script type="application/ld+json">{"@type":"NewsArticle","author":{"@id":[]},"image":true,"datePublished":"invalid"}</script>', "html.parser")
    result = news_article_schema(soup)
    assert result["status"] == "WARN"
    assert result["articles"][0]["fields"] == {"author": False, "datePublished": False, "image": False}


def test_pagespeed_field_scope_and_missing_metrics() -> None:
    payload = {
        "loadingExperience": {"id": BASE + "/article", "metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2300, "category": "FAST"}}},
        "originLoadingExperience": {"id": BASE, "metrics": {"INTERACTION_TO_NEXT_PAINT": {"percentile": 600, "category": "SLOW"}, "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 12, "category": "AVERAGE"}}},
    }
    result = parse_cwv(payload)
    assert result["status"] == "FAIL"
    assert result["metrics"]["LCP"]["scope"] == "url"
    assert result["metrics"]["INP"]["scope"] == "origin"
    assert result["metrics"]["CLS"]["percentile_raw"] == 12
    assert result["complete"]
    assert parse_cwv({"lighthouseResult": {"audits": {}}})["status"] == "WARN"
    assert parse_cwv({})["metrics"]["LCP"] is None


def test_pagespeed_api_key_is_in_header_not_url() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    crawler = Crawler(PoliteHttp(client, sleep=Mock()), BASE + "/")
    crawler.load_robots()
    result = check_pagespeed(crawler, configured_site(), "test-key-not-a-secret")
    psi_requests = [request for request in requests if str(request.url).startswith(PSI_ENDPOINT)]
    assert len(psi_requests) == 3
    assert all(request.headers["X-Goog-Api-Key"] == "test-key-not-a-secret" for request in psi_requests)
    assert all("test-key-not-a-secret" not in str(request.url) for request in psi_requests)
    assert "test-key-not-a-secret" not in json.dumps(result)


def test_missing_psi_configuration_does_not_make_requests() -> None:
    site = configured_site()
    site.representative_urls["article"] = None
    crawler, requests = crawler_for({})
    result = check_pagespeed(crawler, site, None)
    assert len(requests) == 1
    assert result["urls"][0]["reason"] == "url_not_configured"
    assert result["urls"][1]["reason"] == "pagespeed_api_key_missing"


def test_inspection_is_sampled_and_persistent_daily_cap_is_enforced(tmp_path: Path) -> None:
    site = configured_site()
    site.inspection_limit = 2
    crawler, _ = crawler_for({})
    service = Mock()
    service.urlInspection.return_value.index.return_value.inspect.return_value.execute.return_value = {
        "inspectionResult": {"indexStatusResult": {"verdict": "PASS", "coverageState": "Indexed"}},
    }
    client = GscClient(service, sleep=Mock())
    sample = [{"url": BASE + f"/article-{index}"} for index in range(20)]
    budget = InspectionBudget(tmp_path, 2, START)
    first = check_inspection(crawler, site, sample, client, budget)
    assert len(first["urls"]) == 2
    assert first["api_attempts"] == 2
    assert first["status"] == "OK"
    second = check_inspection(crawler, site, sample, client, InspectionBudget(tmp_path, 2, START))
    assert second["api_attempts"] == 0
    assert second["urls"][0]["reason"] == "inspection_daily_local_budget_exhausted"
    assert service.urlInspection.return_value.index.return_value.inspect.call_count == 2


def test_http_retry_throttle_and_size_limit() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429 if calls == 1 else 200, text="body")

    sleep = Mock()
    http = PoliteHttp(httpx.Client(transport=httpx.MockTransport(handler)), interval=2, sleep=sleep, clock=lambda: 0)
    assert http.get(BASE).status_code == 200
    assert calls == 2
    assert any(call.args[0] == 2 for call in sleep.call_args_list)
    with pytest.raises(CheckError, match="response_size_limit"):
        http.get(BASE, limit=2)


def test_inspection_retry_cannot_bypass_daily_budget(tmp_path: Path) -> None:
    site = configured_site()
    site.inspection_limit = 1
    crawler, _ = crawler_for({})
    service = Mock()
    service.urlInspection.return_value.index.return_value.inspect.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": "429"}), b'{"error":{}}'
    )
    result = check_inspection(
        crawler, site, [{"url": BASE + "/article"}],
        GscClient(service, sleep=Mock()), InspectionBudget(tmp_path, 1, START),
    )
    assert result["api_attempts"] == 1
    assert result["urls"][0]["reason"] == "inspection_daily_local_budget_exhausted"
    assert service.urlInspection.return_value.index.return_value.inspect.call_count == 1


def test_snapshot_comparison_ignores_timestamps_and_sample_rotation(tmp_path: Path) -> None:
    crawler, _ = crawler_for({BASE + "/sitemap.xml": (200, xml_urls(1)), BASE + "/article-00": (200, article_html(BASE + "/article-00"))})
    first = collect_site(configured_site(), START, crawler.http, None, InspectionBudget(tmp_path, 3))
    assert first["changed_since_last_week"] is None
    assert first["comparison"] == "no_previous_week"
    previous = deepcopy(first)
    current = deepcopy(first)
    current["checked_at"] = "different timestamp"
    result = add_comparison(current, previous)
    assert result["changed_since_last_week"] is False
    current = deepcopy(first)
    current["sample"][0]["url"] = BASE + "/new-article"
    result = add_comparison(current, previous)
    assert result["changed_since_last_week"] is False
    assert result["sample"][0]["comparison"] == "not_in_previous_sample"
    assert result["sample_sample_comparison"]["new_sample_url_count"] == 1
    current = deepcopy(first)
    current["robots"]["sha256"] = "changed"
    result = add_comparison(current, previous)
    assert result["changed_since_last_week"] is True
    assert any(change["path"] == "/robots/sha256" for change in result["changes"])


@respx.mock
def test_run_writes_json_and_compares_previous_week(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml")).sites[0].model_copy(deep=True)
    settings = Settings(sites=[site])
    monkeypatch.setattr(tech_checks, "create_service", Mock(side_effect=ValueError("not configured")))
    monkeypatch.delenv("PAGESPEED_API_KEY", raising=False)
    monkeypatch.setenv("SEO_USER_AGENT", "SeoWeekly/1.0 (offline test)")
    monkeypatch.setattr(tech_checks.time, "sleep", Mock())
    respx.get(BASE + "/robots.txt").respond(200, text=ROBOTS)
    respx.get(BASE + "/sitemap.xml").respond(200, text=xml_urls(1))
    page_route = respx.get(BASE + "/article-00").respond(200, text=article_html(BASE + "/article-00"), headers={"content-type": "text/html"})
    paths = tech_checks.run(settings, START, tmp_path)
    assert paths == [tmp_path / "data" / "2026-W43" / "tech_fakt.json"]
    result = read_json(paths[0])
    assert result["sample_size"] == 1
    assert result["sample"][0]["status"] == "OK"
    assert result["status"] == "WARN"
    assert result["changed_since_last_week"] is None
    assert result["robots"]["changed_since_last_week"] is None
    result["week"] = "2026-W42"
    result["robots"]["sha256"] = "previous_hash"
    write_json(tmp_path / "data" / "2026-W42" / "tech_fakt.json", result)
    tech_checks.run(settings, START, tmp_path)
    updated = read_json(paths[0])
    assert updated["changed_since_last_week"]
    assert updated["robots"]["changed_since_last_week"]
    assert page_route.call_count == 2