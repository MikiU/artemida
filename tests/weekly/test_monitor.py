from datetime import date
from unittest.mock import Mock

import pytest

from config import SiteConfig
from weekly import monitor
from weekly.common import week_id, week_start
from tests.weekly.test_detect_anomalies import document


def configured(key="fakt"):
    return SiteConfig(key, key, "https://www.fakt.pl/", "https://www.fakt.pl/", [], brand_terms=["fakt"])


def test_collection_is_explicit_isolated_and_offline_load_does_not_call_api(tmp_path, monkeypatch):
    builder = Mock(return_value=Mock())
    monkeypatch.setattr(monitor, "_build_service", builder)
    monkeypatch.setattr(monitor, "fetch_site", Mock(side_effect=[RuntimeError("private error"), document()]))
    sites = {key: configured(key) for key in ("broken", "fakt")}
    results = monitor.collect(sites, tuple(sites), "2026-W43", "unused", root=tmp_path)
    assert results[0]["summary"]["status"] == "WARN"
    assert results[0]["gsc"] is None
    assert results[1]["gsc"]["anomalies"]["status"] == "WARN"
    paths = monitor.history(tmp_path, "2026-W43")
    assert len(paths) == 2
    for path in paths:
        loaded = monitor.load_measurement(path, sites[path.parent.name], "2026-W43", tmp_path)
        assert loaded["tech"]["state"] == "not_run"
    assert builder.call_count == 1
    assert monitor.fetch_site.call_count == 2
    assert "private error" not in str(results)
    with pytest.raises(ValueError):
        monitor.load_measurement(paths[0], configured("wrong"), "2026-W43", tmp_path)


def test_options_preserve_existing_config_and_validate_thresholds():
    site = configured()
    site.weekly_monitoring = {"thresholds": {"clicks": {"warn": 0.1, "fail": 0.4}}, "inspection_limit": 0}
    adapted = monitor.adapt_site(site)
    assert adapted.thresholds["clicks"].warn == 0.1
    assert adapted.thresholds["ctr"].warn == 0.15
    assert adapted.inspection_limit == 0
    site.weekly_monitoring["thresholds"]["clicks"]["fail"] = 0.01
    with pytest.raises(ValueError):
        monitor.adapt_site(site)


def test_missing_gsc_and_unrun_tech_are_not_ok():
    summary = monitor.source_status(None, {"status": None, "state": "not_run"})
    assert summary["status"] == "WARN"
    assert summary["gsc_state"] == "no_data"
    assert summary["tech_state"] == "not_run"


def test_absent_brand_terms_are_not_classified_as_non_brand():
    from weekly.detect_anomalies import analyze
    site = configured()
    site.brand_terms = []
    result = analyze(document(), monitor.adapt_site(site))
    assert result["brand_non_brand"]["available"] is False
    assert result["brand_non_brand"]["periods"] == {}


def test_previous_technical_uses_measurement_time_not_report_week(tmp_path, monkeypatch):
    from pathlib import Path
    paths = [Path("2026-W43/fakt/older.json"), Path("2026-W42/fakt/newer.json")]
    monkeypatch.setattr(monitor, "history", lambda root: paths)
    def load(path, *args):
        return {"tech": {"state": "measured", "checked_at": "2026-11-02" if "newer" in path.name else "2026-11-01"}}
    monkeypatch.setattr(monitor, "load_measurement", load)
    assert monitor.previous_technical(configured(), tmp_path)["checked_at"] == "2026-11-02"


@pytest.mark.parametrize("today,expected", [(date(2021, 1, 4), "2020-W53"), (date(2026, 11, 1), "2026-W43"), (date(2026, 11, 2), "2026-W44")])
def test_completed_week(today, expected):
    assert week_id(week_start(today=today)) == expected


def test_gsc_today_uses_pacific_across_utc_week_boundary(monkeypatch):
    from datetime import datetime, timezone
    from weekly import common
    clock = Mock()
    clock.now.side_effect = lambda zone: datetime(2026, 11, 2, 1, tzinfo=timezone.utc).astimezone(zone)
    monkeypatch.setattr(common, "datetime", clock)
    assert common.today_gsc() == date(2026, 11, 1)
    assert common.week_id(common.week_start()) == "2026-W43"