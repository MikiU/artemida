"""Evidence-bound Polish reports; all facts are rendered by Python."""
from __future__ import annotations

import hashlib
import json
import re

from weekly.common import METRICS, worst_status
from weekly.report_context import COLORS

LABELS = {"clicks": "Kliknięcia", "impressions": "Wyświetlenia", "ctr": "CTR", "position": "Pozycja"}


def display_value(value, path=""):
    if value is None:
        return "Brak danych"
    if isinstance(value, bool):
        return "tak" if value else "nie"
    if isinstance(value, (int, float)):
        percent = path.endswith(("wow_change_pct", "change_from_mean_pct", "deterioration_pct"))
        ctr = "/metrics/ctr/" in path and path.endswith(("/current", "/previous"))
        number = value * 100 if ctr else value
        precision = 0 if isinstance(value, int) or (float(value).is_integer() and not percent and not ctr) else 2
        result = format(number, f"{',' if not percent else '+,'}.{precision}f").replace(",", " ").replace(".", ",")
        return result + ("%" if percent or ctr else "")
    if isinstance(value, str):
        return {"INCOMPLETE": "Niepełne dane", "AVAILABLE_WITH_GSC_LIMITATIONS": "Dostępne z ograniczeniami GSC",
                "not_run": "Nie wykonano", "no_data": "Brak danych", "measured": "Wykonano"}.get(value, value)
    return json.dumps(value, ensure_ascii=False)


def pointer(document, path):
    current = document
    for token in path.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def escaped(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\n", " ").replace("`", "&#96;")


def evidence(document, path):
    value = pointer(document, path)
    filename = f"measurement_{document['site']}.json" if "site" in document else "report_context.json"
    return f"{escaped(display_value(value, path))} <!-- evidence: {filename}#{path} -->"


def ai_facts(document):
    if not document.get("gsc"):
        return {}
    facts = {}
    for metric in METRICS:
        for field in ("current", "wow_change_pct", "status"):
            path = f"/gsc/anomalies/metrics/{metric}/{field}"
            facts[path] = pointer(document, path)
    for section, field in (("pages/top_gains", "wow_delta"), ("new_queries", "clicks")):
        for index, row in enumerate(pointer(document, "/gsc/anomalies/" + section)[:5]):
            path = f"/gsc/anomalies/{section}/{index}/{field}"
            facts[path] = row[field]
            label = "url" if "url" in row else "query"
            facts[f"/gsc/anomalies/{section}/{index}/{label}"] = row[label]
    return facts


def build_context(documents):
    return {
        "week": documents[0]["week"], "site_count": len(documents),
        "status": worst_status([doc["summary"]["status"] for doc in documents]),
        "status_counts": {status: sum(doc["summary"]["status"] == status for doc in documents) for status in COLORS},
        "incomplete_gsc_count": sum(doc["summary"]["gsc_data_quality"] == "INCOMPLETE" for doc in documents),
        "unrun_tech_count": sum(doc["tech"]["state"] == "not_run" for doc in documents),
    }


def validate_interpretation(document, payload):
    if not isinstance(payload, dict) or set(payload) != {"items"} or not isinstance(payload["items"], list) or len(payload["items"]) > 5:
        raise ValueError("Niepoprawny format interpretacji AI")
    facts = ai_facts(document)
    for item in payload["items"]:
        if not isinstance(item, dict) or set(item) != {"kind", "source", "value", "hypothesis", "confidence", "verification"}:
            raise ValueError("Niepoprawny format twierdzenia AI")
        if not isinstance(item["source"], str) or item["source"] not in facts or item["value"] != facts[item["source"]] or type(item["value"]) not in (int, float):
            raise ValueError("AI wskazuje niezgodny sygnal zrodlowy")
        if item["kind"] not in {"interpretation", "followup"} or item["confidence"] not in {"niska", "srednia", "wysoka"}:
            raise ValueError("Niepoprawny typ lub pewnosc hipotezy")
        if item["kind"] == "followup" and not any(part in item["source"] for part in ("/pages/top_gains/", "/new_queries/")):
            raise ValueError("Follow-up bez wzrostu strony lub nowego zapytania")
        for field in ("hypothesis", "verification"):
            text = item[field]
            if not isinstance(text, str) or not text.strip() or len(text) > 500:
                raise ValueError("Brak tresci interpretacji")
            if re.search(r"\d|%|\b(?:OK|WARN|FAIL)\b|prognoz|forecast|predict|gwarant|wzrosnie|wzrośnie", text, re.I):
                raise ValueError("AI dodaje liczby, statusy lub prognoze poza polami danych")
    return payload


def render_report(documents, interpretations=None):
    if not documents or len({doc["week"] for doc in documents}) != 1:
        raise ValueError("Raport wymaga pomiarow tego samego tygodnia")
    interpretations = interpretations or {}
    for doc in documents:
        if doc["site"] in interpretations:
            validate_interpretation(doc, interpretations[doc["site"]])
    context = build_context(documents)
    lines = [f"# Raport SEO {documents[0]['week']}"]
    lines += ["Liczba serwisów w raporcie: " + evidence(context, "/site_count") + ".",
              "Status zbiorczy: " + evidence(context, "/status") + "; " + "; ".join(status + ": " + evidence(context, "/status_counts/" + status) for status in COLORS) + ".",
              "Serwisy z niepełnym lub niedostępnym GSC: " + evidence(context, "/incomplete_gsc_count") + "; technika niewykonana: " + evidence(context, "/unrun_tech_count") + ".", ""]
    if context["incomplete_gsc_count"]:
        lines += ["> **SZKIC — niepełne dane GSC.** Porównania, rankingi i sygnały WARN/FAIL są wstępne. Nie traktuj ich jako potwierdzonej oceny pełnego tygodnia. Nie uzupełniamy brakujących dni zerami.", ""]
    if not interpretations:
        lines += ["> Raport liczbowy bez interpretacji AI. Lista tematów poniżej to sygnały do ręcznej oceny, nie gotowe rekomendacje redakcyjne.", ""]
    lines += ["## Serwisy", "| Serwis | Status zbiorczy | Kolor | Kliknięcia WoW | Jakość GSC |", "| --- | --- | --- | --- | --- |"]
    for doc in documents:
        status = doc["summary"]["status"]
        signal = evidence(doc, "/gsc/anomalies/metrics/clicks/wow_change_pct") if doc.get("gsc") else evidence(doc, "/gsc")
        lines.append(f"| {escaped(doc['name'])} | {evidence(doc, '/summary/status')} | {COLORS[status]} | {signal} | {evidence(doc, '/summary/gsc_data_quality')} |")
    lines += ["", "## Anomalie"]
    for doc in documents:
        lines += [f"### {escaped(doc['name'])}", "Pomiar GSC: " + evidence(doc, "/gsc/fetched_at") if (doc.get("gsc") or {}).get("fetched_at") else "Pomiar: " + evidence(doc, "/measured_at"),
                  "Jakość GSC: " + evidence(doc, "/summary/gsc_data_quality")]
        if not doc.get("gsc"):
            lines += ["Brak danych GSC: " + evidence(doc, "/gsc")]
            continue
        if "coverage" in doc["gsc"]["periods"]["T"]:
            path = "/gsc/periods/T/coverage"
            lines += ["Faktyczny koniec danych T: " + evidence(doc, path + "/effective_end")
                      + "; dostępne dni: " + evidence(doc, path + "/available_day_count")
                      + "/" + evidence(doc, path + "/expected_day_count") + "."]
        if doc["summary"]["gsc_data_quality"] == "INCOMPLETE":
            lines += ["**Ocena wstępna: niepełne dane.** Statusy poniżej są sygnałami z dostępnych danych, nie potwierdzeniem anomalii pełnego tygodnia. Porównanie może obejmować okresy różnej długości."]
        lines += ["| Metryka | T | T-1 | Zmiana WoW | Sygnał |", "| --- | --- | --- | --- | --- |"]
        for metric in METRICS:
            path = f"/gsc/anomalies/metrics/{metric}"
            lines += [f"| {LABELS[metric]} | " + " | ".join(evidence(doc, path + '/' + field) for field in ("current", "previous", "wow_change_pct", "status")) + " |"]
        for index, issue in enumerate(doc["gsc"]["anomalies"]["missing_day_issues"]):
            for day_index in range(len(issue["missing_dates"])):
                lines += ["- Brak danych dziennych: " + evidence(doc, f"/gsc/anomalies/missing_day_issues/{index}/period") + ", " + evidence(doc, f"/gsc/anomalies/missing_day_issues/{index}/missing_dates/{day_index}")]
        for item in interpretations.get(doc["site"], {}).get("items", []):
            if item["kind"] == "interpretation":
                lines += [hypothesis_line(doc, item)]
    lines += ["", "## Co zagrało / co nie"]
    for doc in documents:
        if not doc.get("gsc"):
            continue
        lines += [f"### {escaped(doc['name'])}", "Ranking obejmuje tylko zwrocone wiersze GSC; nieobecny URL to zero tylko na potrzeby rankingu."]
        for section, title in (("top_gains", "Wzrosty stron"), ("top_losses", "Spadki stron")):
            lines += [f"#### {title}"]
            rows = doc["gsc"]["anomalies"]["pages"][section][:3]
            if not rows:
                lines += ["Brak pozycji: " + evidence(doc, f"/gsc/anomalies/pages/{section}")]
            for index in range(len(rows)):
                path = f"/gsc/anomalies/pages/{section}/{index}"
                lines += ["- URL: " + evidence(doc, path + "/url") + "; delta klikniec: " + evidence(doc, path + "/wow_delta")]
        lines += ["Zapytania nieobecne w zwroconych danych poprzednich czterech tygodni: " + evidence(doc, "/gsc/anomalies/new_query_count"),
                  "Nie jest to dowod pierwszego takiego wyszukiwania w historii. Zapytania anonimowe i pominiete nie sa uwzglednione."]
    lines += ["", "## Technika"]
    for doc in documents:
        lines += [f"### {escaped(doc['name'])}", "Stan kontroli: " + evidence(doc, "/tech/state")]
        if doc["tech"]["state"] == "not_run":
            lines += ["Brak oceny technicznej, nie potwierdzona usterka. Kontrole uruchamia się osobno przy nowym pomiarze."]
        if doc["tech"]["state"] != "measured":
            continue
        lines += ["Pomiar aktualny, nie historyczny: " + evidence(doc, "/tech/checked_at"),
                  "Poprzedni zapisany pomiar: " + evidence(doc, "/tech/comparison_measurement_at"),
                  "Status techniki: " + evidence(doc, "/tech/status")]
        changed = doc["tech"]["changed_since_last_week"]
        lines += [("Brak porownania: " if changed is None else "bez zmian. " if changed is False else "Zmiany before/after: ") + evidence(doc, "/tech/changes" if changed else "/tech/changed_since_last_week")]
        for section in ("robots", "sitemap", "pagespeed", "inspection"):
            if doc["tech"][section]["status"] != "OK":
                lines += [f"- {section}: " + evidence(doc, f"/tech/{section}")]
        for index, row in enumerate(doc["tech"]["sample"]):
            if row["status"] != "OK":
                lines += ["- Proba strony: " + evidence(doc, f"/tech/sample/{index}")]
    lines += ["", "## Follow-upy"]
    for doc in documents:
        lines += [f"### {escaped(doc['name'])}"]
        items = [item for item in interpretations.get(doc["site"], {}).get("items", []) if item["kind"] == "followup"]
        if items:
            lines += [hypothesis_line(doc, item) for item in items]
        elif doc.get("gsc"):
            lines += ["**Sygnały do ręcznej oceny — bez rekomendacji AI.** Sprawdź treść, aktualność i intencję przed wyborem tematu."]
            gains = doc["gsc"]["anomalies"]["pages"]["top_gains"][:3]
            for index in range(len(gains)):
                path = f"/gsc/anomalies/pages/top_gains/{index}"
                lines += ["- Wzrost strony: " + evidence(doc, path + "/url") + "; zmiana kliknięć: " + evidence(doc, path + "/wow_delta") + "."]
            rows = doc["gsc"]["anomalies"]["new_queries"][:5 - len(gains)]
            for index in range(len(rows)):
                path = f"/gsc/anomalies/new_queries/{index}"
                lines += ["- Zapytanie nieobecne w zwróconej historii: " + evidence(doc, path + "/query") + "; kliknięcia: " + evidence(doc, path + "/clicks") + "."]
            if not rows and not gains:
                lines += ["Brak propozycji opartych na dostepnych danych: " + evidence(doc, "/gsc/anomalies/new_queries") + "; wzrosty stron: " + evidence(doc, "/gsc/anomalies/pages/top_gains")]
        else:
            lines += ["Brak danych: " + evidence(doc, "/gsc")]
    lines += ["", "## Na spotkanie"]
    if context["incomplete_gsc_count"]:
        lines += ["- Uzupełnić brakujące dni przed oceną tygodnia. Liczba serwisów z niepełnym GSC: " + evidence(context, "/incomplete_gsc_count") + "."]
    candidates = sorted(documents, key=lambda doc: {"FAIL": 0, "WARN": 1, "OK": 2}[doc["summary"]["status"]])
    for doc in candidates[:4 if context["incomplete_gsc_count"] else 5]:
        lines += [f"- {escaped(doc['name'])}: jakie dzialanie wynika ze statusu " + evidence(doc, "/summary/status") + "?"]
    if len(documents) < 3:
        lines += ["- Jak uzupelnic niepelne pomiary GSC? Liczba serwisow: " + evidence(context, "/incomplete_gsc_count"),
                  "- Ktore kontrole techniczne zaplanowac? Liczba serwisow bez kontroli: " + evidence(context, "/unrun_tech_count")]
    return "\n".join(lines) + "\n"


def hypothesis_line(doc, item):
    return (f"- HIPOTEZA (pewnosc: {item['confidence']}): {escaped(item['hypothesis'])} "
            f"Sygnal: {evidence(doc, item['source'])}. Weryfikacja: {escaped(item['verification'])}")


def verify_report(documents, markdown, interpretations=None):
    from weekly.common import Site
    from weekly.detect_anomalies import analyze
    from weekly.monitor import source_status

    try:
        if len({doc["site"] for doc in documents}) != len(documents):
            raise ValueError("Duplicate sites")
        for doc in documents:
            site = Site.model_validate(doc["configuration"])
            if site.id != doc["site"] or site.gsc_property != doc["property"]:
                raise ValueError("Identity mismatch")
            if doc.get("gsc") and analyze(doc["gsc"], site) != doc["gsc"]["anomalies"]:
                raise ValueError("Calculation mismatch")
            if source_status(doc.get("gsc"), doc["tech"]) != doc["summary"]:
                raise ValueError("Status mismatch")
    except (ValueError, KeyError, TypeError):
        return {"ready": False, "decision": "ODRZUC", "reason": "Niezgodne dane, obliczenia lub statusy"}
    expected = render_report(documents, interpretations)
    if markdown != expected:
        return {"ready": False, "decision": "ODRZUC", "reason": "Tresc niezgodna z danymi lub zatwierdzonym szablonem"}
    incomplete = any(doc["summary"]["gsc_data_quality"] == "INCOMPLETE" for doc in documents)
    verification = {
        "ready": not incomplete,
        "verified": True,
        "decision": "SZKIC" if incomplete else "AKCEPTUJ",
        "reason": "incomplete_gsc" if incomplete else None,
        "method": "deterministic_render_and_evidence_validation",
        "sha256": hashlib.sha256(markdown.encode()).hexdigest(),
    }
    return verification