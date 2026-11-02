"""Test status aggregation before language models read report evidence."""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from weekly.common import METRICS, Settings, load_settings, read_json, write_json
from weekly.report_context import run

START = date(2026, 10, 19)
WEEK = "2026-W43"


def sources(root: Path, gsc_status: str | None = "OK", tech_status: str = "OK") -> Settings:
    """Write minimal source contracts with no real traffic or credentials."""
    settings = load_settings(__import__("pathlib").Path(__file__).with_name("sites.yaml")).model_copy(deep=True)
    settings.sites = settings.sites[:1]
    site = settings.sites[0]
    identity: dict[str, Any] = {
        "schema_version": 1, "site": site.id, "property": site.gsc_property, "week": WEEK,
    }
    write_json(root / "data" / WEEK / f"gsc_{site.id}.json", {
        **identity,
        "anomalies": {
            "status": gsc_status,
            "data_quality": "INCOMPLETE" if gsc_status is None else "AVAILABLE_WITH_GSC_LIMITATIONS",
            "metrics": {metric: {} for metric in METRICS},
        },
    })
    write_json(root / "data" / WEEK / f"tech_{site.id}.json", {**identity, "status": tech_status})
    return settings


@pytest.mark.parametrize("gsc,tech,expected", [
    ("OK", "OK", "OK"), ("OK", "WARN", "WARN"), ("OK", "FAIL", "FAIL"),
    ("WARN", "OK", "WARN"), ("FAIL", "OK", "FAIL"),
    (None, "OK", "WARN"), (None, "FAIL", "FAIL"),
])
def test_status_is_computed_by_code(tmp_path: Path, gsc: str | None, tech: str, expected: str) -> None:
    settings = sources(tmp_path, gsc, tech)
    context = read_json(run(settings, START, tmp_path))
    assert context["status"] == expected
    assert context["sites"][0]["status"] == expected
    assert context["color"] == {"OK": "zielony", "WARN": "\u017c\u00f3\u0142ty", "FAIL": "czerwony"}[expected]
    assert context["site_count"] == 1
    assert context["status_counts"][expected] == 1


def test_incomplete_quality_cannot_be_green(tmp_path: Path) -> None:
    settings = sources(tmp_path)
    path = tmp_path / "data" / WEEK / "gsc_fakt.json"
    payload = read_json(path)
    payload["anomalies"]["data_quality"] = "INCOMPLETE"
    write_json(path, payload)
    context = read_json(run(settings, START, tmp_path))
    assert context["status"] == "WARN"
    assert context["sites"][0]["status_reasons"] == ["gsc_not_fully_evaluated"]


@pytest.mark.parametrize("field,value", [("week", "2026-W42"), ("site", "wrong"), ("property", "wrong"), ("status", "UNKNOWN")])
def test_invalid_technical_input_does_not_create_context(tmp_path: Path, field: str, value: str) -> None:
    settings = sources(tmp_path)
    path = tmp_path / "data" / WEEK / "tech_fakt.json"
    payload = read_json(path)
    payload[field] = value
    write_json(path, payload)
    with pytest.raises(ValueError):
        run(settings, START, tmp_path)
    assert not (tmp_path / "data" / WEEK / "report_context.json").exists()


def test_anomaly_stage_is_required(tmp_path: Path) -> None:
    settings = sources(tmp_path)
    path = tmp_path / "data" / WEEK / "gsc_fakt.json"
    payload = read_json(path)
    del payload["anomalies"]
    write_json(path, payload)
    with pytest.raises(ValueError, match="detect_anomalies"):
        run(settings, START, tmp_path)


def test_site_order_and_report_paths(tmp_path: Path) -> None:
    settings = sources(tmp_path)
    site = settings.sites[0]
    second_site = site.model_copy(deep=True)
    second_site.id = "another.site"
    settings.sites.append(second_site)
    for kind in ("gsc", "tech"):
        payload = read_json(tmp_path / "data" / WEEK / f"{kind}_{site.id}.json")
        payload["site"] = second_site.id
        write_json(tmp_path / "data" / WEEK / f"{kind}_{second_site.id}.json", payload)
    context = read_json(run(settings, START, tmp_path))
    assert [entry["id"] for entry in context["sites"]] == ["fakt", "another.site"]
    assert context["sites"][1]["reports"]["insights"] == f"out/{WEEK}/another.site_insights.md"
    assert context["reports"]["final"] == f"out/{WEEK}/raport.md"
    assert context["status_counts"] == {"OK": 2, "WARN": 0, "FAIL": 0}