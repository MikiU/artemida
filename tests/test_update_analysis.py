from datetime import date

import pandas as pd
import pytest

from analytics.update_analysis import UpdateRequest, collect_update, update_periods
from config import SiteConfig
from services.gsc_service import GSCError


def test_update_periods_exclude_rollout():
    request = UpdateRequest(("a",), date(2026, 11, 10), date(2026, 11, 12), 14)
    before, after = update_periods(request)
    assert (before.start, before.end) == ("2026-10-27", "2026-11-09")
    assert (after.start, after.end) == ("2026-11-13", "2026-11-26")
    with pytest.raises(ValueError):
        update_periods(UpdateRequest(("a",), date(2026, 11, 12), date(2026, 11, 10), 14))


def test_update_collection_handles_missing_dates_and_errors(monkeypatch):
    sites = {key: SiteConfig(key, key, key, f"https://{key}.pl/", []) for key in ("a", "b")}
    calls = []

    def fake_gsc(start, end, prop, credentials, dimensions, search_type, use_cache):
        calls.append((prop, dimensions, search_type))
        if prop == "b":
            raise GSCError("no access")
        if dimensions == ("date",):
            dates = pd.date_range(start, end).strftime("%Y-%m-%d").tolist()
            if search_type == "discover":
                dates.remove("2026-11-11")
            return pd.DataFrame({"date": dates, "clicks": 10, "impressions": 100})
        clicks = 20 if start == "2026-11-11" else 10
        return pd.DataFrame({"page": ["https://a.pl/item"], "clicks": [clicks], "impressions": [100]})

    monkeypatch.setattr("analytics.update_analysis.get_gsc_data", fake_gsc)
    request = UpdateRequest(("a", "b"), date(2026, 11, 10), date(2026, 11, 10), 1)
    result = collect_update(request, sites, "unused")
    assert list(result["statuses"]["status"]) == ["ok", "incomplete", "error", "error"]
    assert result["pages"].iloc[0]["clicks_change"] == 10
    assert not any(prop == "a" and dimensions == ("page",) and source == "discover" for prop, dimensions, source in calls)