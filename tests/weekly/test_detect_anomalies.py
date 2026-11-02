"""Synthetic and file-level contracts for deterministic anomaly detection."""

from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from weekly.common import Settings, Site, Threshold, load_settings, read_json, week_id, write_json
from weekly.detect_anomalies import analyze, metric_summary, run

START = date(2026, 10, 19)


def site_config() -> Site:
    """Use a real config shape without contacting the configured site."""
    return load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml")).sites[0].model_copy(deep=True)


def row(key: str, clicks: float, impressions: float = 1000, position: float = 4) -> dict[str, Any]:
    """Build a single-dimension API row."""
    return {
        "keys": [key], "clicks": clicks, "impressions": impressions,
        "ctr": clicks / impressions if impressions else 0, "position": position,
    }


def document(current: float = 75, history: tuple[float, ...] = (100, 102, 98, 100)) -> dict[str, Any]:
    """Build five consecutive weekly snapshots with complete date coverage."""
    site = site_config()
    periods = {}
    for offset, clicks in enumerate((current, *history)):
        start = START - timedelta(weeks=offset)
        label = f"T-{offset}" if offset else "T"
        totals = row("unused", clicks)
        del totals["keys"]
        periods[label] = {
            "week": week_id(start), "start": start.isoformat(),
            "end": (start + timedelta(days=6)).isoformat(),
            "totals": totals, "totals_aggregation": "byProperty",
            "pages": {"rows": [], "pagination_exhausted": True},
            "queries": {"rows": [], "pagination_exhausted": True},
            "missing_dates": [],
        }
    return {
        "schema_version": 1, "site": site.id, "property": site.gsc_property,
        "week": week_id(START), "search_type": "web", "data_state": "final",
        "warnings": ["original warning"], "periods": periods,
    }


def test_drop_25_percent() -> None:
    result = analyze(document(), site_config())
    clicks = result["metrics"]["clicks"]
    assert clicks["wow_change_pct"] == -25
    assert clicks["baseline_mean"] == 100
    assert clicks["baseline_stddev"] == pytest.approx((8 / 3) ** 0.5)
    assert clicks["z_score"] < -2
    assert clicks["status"] == "WARN"
    assert result["status"] == "WARN"
    assert "clicks" in result["flagged_metrics"]


def test_fail_requires_stronger_drop() -> None:
    assert analyze(document(current=70), site_config())["metrics"]["clicks"]["status"] == "FAIL"


def test_noise_5_percent_is_not_an_anomaly() -> None:
    result = analyze(document(current=95), site_config())
    assert result["metrics"]["clicks"]["outside_adverse_band"]
    assert result["metrics"]["clicks"]["status"] == "OK"
    assert result["flagged_metrics"] == []


def test_cyclical_history_prevents_flag_from_wow_alone() -> None:
    result = analyze(document(current=80, history=(120, 80, 120, 80)), site_config())
    clicks = result["metrics"]["clicks"]
    assert clicks["wow_change_pct"] == pytest.approx(-100 / 3)
    assert clicks["outside_adverse_band"] is False
    assert clicks["status"] == "OK"


def test_position_uses_upper_band_and_growth_is_not_failure() -> None:
    threshold = Threshold(warn=0.15, fail=0.25)
    worse = metric_summary("position", 6, [4, 4.1, 3.9, 4], threshold)
    assert worse["status"] == "FAIL"
    assert worse["z_score"] > 2
    better = metric_summary("position", 2, [4, 4.1, 3.9, 4], threshold)
    assert better["status"] == "OK"
    growth = metric_summary("clicks", 200, [100, 102, 98, 100], threshold)
    assert growth["status"] == "OK"


def test_exact_threshold_does_not_exceed_it() -> None:
    threshold = Threshold(warn=0.15, fail=0.25)
    assert metric_summary("clicks", 85, [100] * 4, threshold)["status"] == "OK"
    assert metric_summary("clicks", 75, [100] * 4, threshold)["status"] == "WARN"


def test_zero_stddev_still_detects_a_drop_without_infinity() -> None:
    summary = metric_summary("clicks", 70, [100] * 4, Threshold(warn=0.15, fail=0.25))
    assert summary["z_score"] is None
    assert summary["z_score_reason"] == "zero_stddev"
    assert summary["status"] == "FAIL"
    assert summary["adverse_boundary"] == 100


def test_zero_base_and_missing_data_are_not_ok() -> None:
    threshold = Threshold(warn=0.15, fail=0.25)
    zero = metric_summary("clicks", 10, [0, 0, 0, 0], threshold)
    assert zero["wow_change_pct"] is None
    assert zero["status"] is None
    assert zero["evaluation_reason"] == "zero_previous"
    payload = document()
    payload["periods"]["T-2"]["totals"] = None
    result = analyze(payload, site_config())
    assert result["status"] is None
    assert result["data_quality"] == "INCOMPLETE"
    assert result["metrics"]["clicks"]["baseline_mean"] is None


def test_missing_days_are_exposed() -> None:
    payload = document(current=100)
    payload["periods"]["T"]["missing_dates"] = ["2026-10-25"]
    result = analyze(payload, site_config())
    assert result["status"] is None
    assert result["missing_day_issues"] == [{"period": "T", "missing_dates": ["2026-10-25"]}]


def test_ctr_fraction_and_percentage_point_delta() -> None:
    summary = metric_summary("ctr", 0.075, [0.1] * 4, Threshold(warn=0.15, fail=0.3))
    assert summary["wow_change_pct"] == pytest.approx(-25)
    assert summary["wow_change_pp"] == pytest.approx(-2.5)
    assert summary["status"] == "WARN"


def test_page_ranking_is_absolute_and_includes_absent_pages() -> None:
    payload = document()
    payload["periods"]["T"]["pages"]["rows"] = [
        row("big", 600), row("small", 20), row("new", 30), row("lost", 10),
        *[row(f"extra-{index:02}", index + 1) for index in range(12)],
    ]
    payload["periods"]["T-1"]["pages"]["rows"] = [
        row("big", 500), row("small", 1), row("lost", 200), row("gone", 60),
    ]
    pages = analyze(payload, site_config())["pages"]
    assert len(pages["top_gains"]) == 10
    assert pages["growing_page_count"] == 15
    assert [item["url"] for item in pages["top_gains"][:3]] == ["big", "new", "small"]
    assert [item["url"] for item in pages["top_losses"]] == ["lost", "gone"]
    assert pages["top_losses"][0]["wow_delta"] == -190
    assert not pages["top_losses"][1]["present_in_current_rows"]
    assert pages["top_gains"][1]["wow_change_pct"] is None


def test_brand_aggregation_uses_weighted_metrics() -> None:
    payload = document()
    payload["periods"]["T"]["queries"]["rows"] = [
        row("FAKT.PL news", 10, 100, 2), row("fakt news", 90, 900, 6),
        row("other news", 5, 50, 10),
    ]
    payload["periods"]["T-1"]["queries"]["rows"] = [row("fakt", 50, 500, 4)]
    result = analyze(payload, site_config())["brand_non_brand"]
    brand = result["periods"]["T"]["brand"]
    assert brand["query_count"] == 2
    assert brand["clicks"] == 100
    assert brand["ctr"] == pytest.approx(0.1)
    assert brand["position"] == pytest.approx(5.6)
    assert result["wow"]["brand"]["clicks"]["wow_change_pct"] == 100
    assert result["periods"]["T-1"]["non_brand"]["position"] is None


def test_unicode_brand_matching_and_new_queries_look_back_four_weeks() -> None:
    site = site_config()
    site.brand_terms = ["podr\u00f3\u017ce"]
    payload = document()
    payload["periods"]["T"]["queries"]["rows"] = [
        row("PODRO\u0301Z\u0307E news", 10), row("old-in-T4", 20), row("new", 30),
    ]
    payload["periods"]["T-4"]["queries"]["rows"] = [row("old-in-T4", 1)]
    result = analyze(payload, site)
    assert result["brand_non_brand"]["periods"]["T"]["brand"]["query_count"] == 1
    assert [item["query"] for item in result["new_queries"]] == ["new", "PODRO\u0301Z\u0307E news"]
    assert result["new_query_count"] == 2


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1])
def test_invalid_numbers_are_rejected(invalid: float) -> None:
    payload = document()
    payload["periods"]["T"]["totals"]["clicks"] = invalid
    with pytest.raises(ValueError):
        analyze(payload, site_config())


@pytest.mark.parametrize("defect", ["property", "week", "period", "duplicates", "pagination"])
def test_mismatched_or_partial_input_is_rejected(defect: str) -> None:
    payload = document()
    if defect == "property":
        payload["property"] = "https://wrong.example/"
    elif defect == "week":
        payload["week"] = "2026-W42"
    elif defect == "period":
        payload["periods"]["T-4"]["start"] = "2026-09-01"
    elif defect == "duplicates":
        payload["periods"]["T"]["pages"]["rows"] = [row("same", 10), row("same", 20)]
    else:
        payload["periods"]["T"]["queries"]["pagination_exhausted"] = False
    with pytest.raises(ValueError):
        analyze(payload, site_config())


def test_run_preserves_data_and_is_idempotent(tmp_path: Path) -> None:
    site = site_config()
    settings = Settings(sites=[site])
    payload = document()
    original = deepcopy(payload)
    path = tmp_path / "data" / week_id(START) / f"gsc_{site.id}.json"
    write_json(path, payload)
    assert run(settings, START, tmp_path) == [path]
    first = read_json(path)
    assert first["anomalies"]["status"] == "WARN"
    assert {key: value for key, value in first.items() if key != "anomalies"} == original
    run(settings, START, tmp_path)
    assert read_json(path) == first
    assert payload == original


def test_run_does_not_update_other_sites_when_input_is_missing(tmp_path: Path) -> None:
    settings = load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml")).model_copy(deep=True)
    settings.sites = settings.sites[:2]
    path = tmp_path / "data" / week_id(START) / f"gsc_{settings.sites[0].id}.json"
    payload = document()
    write_json(path, payload)
    with pytest.raises(FileNotFoundError):
        run(settings, START, tmp_path)
    assert read_json(path) == payload