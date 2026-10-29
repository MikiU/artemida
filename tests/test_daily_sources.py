import pandas as pd

import analysis as analysis_module
from analysis import Period, analyze_site_daily_sources
from config import SiteConfig


def test_daily_sources_aligns_periods_and_fills_missing_dates(monkeypatch):
    calls = []

    def fake_get_gsc_data(start, end, prop, credentials, **kwargs):
        calls.append((start, end, kwargs["dimensions"], kwargs["search_type"]))
        if start == "2026-07-01":
            return pd.DataFrame(
                {
                    "date": ["2026-07-01", "2026-07-03"],
                    "clicks": [10, 30],
                    "impressions": [100, 300],
                }
            )
        return pd.DataFrame(
            {
                "date": ["2026-06-01", "2026-06-02", "2026-06-03"],
                "clicks": [5, 10, 15],
                "impressions": [50, 100, 150],
            }
        )

    monkeypatch.setattr(analysis_module, "get_gsc_data", fake_get_gsc_data)
    site = SiteConfig("site", "Site", "https://example.com/", "https://example.com/", [])

    result = analyze_site_daily_sources(
        site,
        Period("2026-07-01", "2026-07-03"),
        Period("2026-06-01", "2026-06-03"),
        "credentials",
        ["web"],
    )["web"]

    assert list(result["day"]) == [1, 2, 3]
    assert list(result["current_clicks"]) == [10, 0, 30]
    assert list(result["previous_clicks"]) == [5, 10, 15]
    assert list(result["clicks_change"]) == [5, -10, 15]
    assert calls == [
        ("2026-07-01", "2026-07-03", ("date",), "web"),
        ("2026-06-01", "2026-06-03", ("date",), "web"),
    ]


def test_daily_sources_returns_no_percentage_for_zero_previous(monkeypatch):
    monkeypatch.setattr(
        analysis_module,
        "get_gsc_data",
        lambda *args, **kwargs: pd.DataFrame(
            columns=["date", "clicks", "impressions"]
        ),
    )
    site = SiteConfig("site", "Site", "https://example.com/", "https://example.com/", [])

    result = analyze_site_daily_sources(
        site,
        Period("2026-07-01", "2026-07-01"),
        Period("2026-06-01", "2026-06-01"),
        "credentials",
        ["discover", "news"],
    )

    assert set(result) == {"discover", "news"}
    assert pd.isna(result["discover"].loc[0, "clicks_change_pct"])
    assert result["discover"].loc[0, "current_clicks"] == 0