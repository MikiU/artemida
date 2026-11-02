from copy import deepcopy

import pytest

from weekly import ai
from weekly.detect_anomalies import analyze
from weekly.monitor import adapt_site, source_status
from weekly.report import render_report, validate_interpretation, verify_report
from tests.weekly.test_detect_anomalies import document
from tests.weekly.test_monitor import configured


def measurement():
    gsc = document()
    gsc["anomalies"] = analyze(gsc, adapt_site(configured()))
    tech = {"state": "not_run", "status": None}
    return {"site": "fakt", "name": "Fakt", "week": "2026-W43", "gsc": gsc, "tech": tech,
            "property": "https://www.fakt.pl/", "configuration": adapt_site(configured()).model_dump(mode="json"),
            "measured_at": "2026-10-30T10:00:00Z", "summary": source_status(gsc, tech)}


def test_report_is_ready_only_when_unchanged_and_evidence_bound():
    docs = [measurement()]
    text = render_report(docs)
    assert verify_report(docs, text)["ready"]
    assert not verify_report(docs, text.replace("-25,00%", "-90,00%"))["ready"]
    assert "<!-- evidence: measurement_fakt.json#/gsc/anomalies/metrics/clicks/wow_change_pct -->" in text
    assert "Nie wykonano" in text
    for heading in ("## Serwisy", "## Anomalie", "## Co zagrało / co nie", "## Technika", "## Follow-upy", "## Na spotkanie"):
        assert heading in text


def item():
    return {"kind": "interpretation", "source": "/gsc/anomalies/metrics/clicks/current", "value": 75,
            "hypothesis": "Mozliwa zmiana zainteresowania tematem", "confidence": "niska",
            "verification": "Sprawdzic rozklad zapytan"}


@pytest.mark.parametrize("field,value", [("value", 999), ("source", "/unknown"), ("hypothesis", "Ruch wzrosnie o 90%"), ("kind", "followup")])
def test_ai_cannot_invent_numbers_or_use_wrong_source(field, value):
    proposed = item()
    proposed[field] = value
    with pytest.raises(ValueError):
        validate_interpretation(measurement(), {"items": [proposed]})


def test_ai_requires_second_review(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: True)
    responses = iter([{"items": [item()]}, {"accepted": False, "rejected": ["Niepotwierdzona przyczyna"]}])
    monkeypatch.setattr(ai, "completion", lambda *args: next(responses))
    with pytest.raises(ValueError, match="odrzucila"):
        ai.interpret(measurement())


def test_missing_gsc_still_has_report():
    doc = measurement()
    doc["gsc"] = None
    doc["summary"] = source_status(None, doc["tech"])
    text = render_report([doc])
    assert "Brak danych GSC" in text
    assert not verify_report([doc], text)["ready"]
    assert verify_report([doc], text)["verified"]


def test_tampered_calculations_cannot_be_published():
    doc = measurement()
    doc["gsc"]["anomalies"]["metrics"]["clicks"]["wow_change_pct"] = -90
    text = render_report([doc])
    assert not verify_report([doc], text)["ready"]


def test_six_day_week_is_a_draft_not_a_final_weekly_alert():
    doc = measurement()
    doc["gsc"]["periods"]["T"]["missing_dates"] = ["2026-10-25"]
    doc["gsc"]["anomalies"] = analyze(doc["gsc"], adapt_site(configured()))
    doc["summary"] = source_status(doc["gsc"], doc["tech"])
    text = render_report([doc])
    result = verify_report([doc], text)
    assert result["verified"] and not result["ready"]
    assert result["decision"] == "SZKIC"
    assert "2026-10-25" in text
    assert "**SZKIC" in text
    assert "sygnały do ręcznej oceny" in text.lower()
    assert "warto zapytac redakcje" not in text


def test_report_formats_numbers_but_keeps_exact_source_references():
    from weekly.report import display_value
    assert display_value(927109.0) == "927 109"
    assert display_value(-13.84658131015859, "/wow_change_pct") == "-13,85%"
    assert display_value(0.06669671862667818, "/gsc/anomalies/metrics/ctr/current") == "6,67%"
    assert display_value("not_run") == "Nie wykonano"


def test_report_shows_effective_end_and_observed_day_count():
    doc = measurement()
    doc["gsc"]["periods"]["T"]["missing_dates"] = ["2026-10-24", "2026-10-25"]
    doc["gsc"]["periods"]["T"]["coverage"] = {
        "effective_end": "2026-10-23", "available_day_count": 5, "expected_day_count": 7, "complete": False,
    }
    doc["gsc"]["anomalies"] = analyze(doc["gsc"], adapt_site(configured()))
    doc["summary"] = source_status(doc["gsc"], doc["tech"])
    text = render_report([doc])
    assert "Faktyczny koniec danych T: 2026-10-23" in text
    assert "/gsc/periods/T/coverage/available_day_count" in text
    assert "okresy różnej długości" in text
    assert not verify_report([doc], text)["ready"]