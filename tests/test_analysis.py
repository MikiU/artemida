"""Testy analyze_site: niezależność od nazwy serwisu i brak mieszania domen."""
import pandas as pd

import analysis as analysis_module
from analysis import Period, analyze_site
from config import SiteConfig


def _make_gsc_stub(data_by_property):
    """Zwraca funkcję udającą get_gsc_data, zależną od property (klucz cache)."""

    def _stub(start, end, gsc_property, credentials_path, use_cache=True):
        return data_by_property[gsc_property].copy()

    return _stub


def _df(rows):
    return pd.DataFrame(rows, columns=["page", "clicks", "impressions", "ctr", "position"])


def test_analyze_site_independent_of_name(monkeypatch):
    data = {
        "https://a/": _df([["https://a/sport/mecz", 100, 1000, 0.1, 3.0]]),
    }
    monkeypatch.setattr(analysis_module, "get_gsc_data", _make_gsc_stub(data))
    monkeypatch.setattr(
        analysis_module, "get_category_urls", lambda url: ["https://a/sport/"]
    )

    site = SiteConfig(
        key="whatever",
        name="Dowolna Nazwa",
        gsc_property="https://a/",
        base_url="https://a/",
        category_sitemaps=["https://a/sitemap.xml"],
    )
    result = analyze_site(
        site, Period("2026-07-01", "2026-07-31"), Period("2026-06-01", "2026-06-30"), "cred"
    )
    assert result.site_name == "Dowolna Nazwa"
    assert result.has_data
    assert "Sport" in set(result.tree["category_path"])


def test_two_sites_do_not_mix(monkeypatch):
    data = {
        "https://a/": _df([["https://a/sport/mecz", 100, 1000, 0.1, 3.0]]),
        "https://b/": _df([["https://b/biznes/gielda", 40, 400, 0.1, 5.0]]),
    }
    monkeypatch.setattr(analysis_module, "get_gsc_data", _make_gsc_stub(data))

    def _sitemap(url):
        if "a" in url and "sitemap-a" in url:
            return ["https://a/sport/"]
        return ["https://b/biznes/"]

    # Sitemap zależny od property serwisu.
    def _fake_collect(site):
        return ["https://a/sport/"] if site.gsc_property == "https://a/" else [
            "https://b/biznes/"
        ]

    monkeypatch.setattr(analysis_module, "_collect_category_urls", _fake_collect)

    site_a = SiteConfig("a", "A", "https://a/", "https://a/", ["https://a/sitemap.xml"])
    site_b = SiteConfig("b", "B", "https://b/", "https://b/", ["https://b/sitemap.xml"])
    current = Period("2026-07-01", "2026-07-31")
    previous = Period("2026-06-01", "2026-06-30")

    result_a = analyze_site(site_a, current, previous, "cred")
    result_b = analyze_site(site_b, current, previous, "cred")

    assert int(result_a.pages["current_clicks"].sum()) == 100
    assert int(result_b.pages["current_clicks"].sum()) == 40
    assert set(result_a.pages["page"]).isdisjoint(set(result_b.pages["page"]))
