"""Streamlit presentation; network entry points exist only inside button actions."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pandas as pd
import streamlit as st

from weekly import ai, monitor
from weekly.common import read_json, write_json, week_id, week_start
from weekly.report import build_context, render_report, verify_report

STATES = {"measured": "Wykonano", "incomplete": "Niepełne dane", "no_data": "Brak danych", "not_run": "Nie wykonano"}


def table(rows):
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.caption("Brak pozycji.")


def check_state(row):
    if row.get("urls"):
        states = {check_state(child) for child in row["urls"]}
        for state in ("Problem", "Brak danych", "Nie wykonano"):
            if state in states:
                return state
        return "OK"
    reason = row.get("reason", "") or ""
    if reason in {"inspection_disabled", "url_not_configured"}:
        return "Nie wykonano"
    if any(fragment in reason for fragment in ("missing", "unavailable", "no_sample", "credentials", "api_error", "budget", "invalid_response")):
        return "Brak danych"
    return "OK" if row.get("status") == "OK" else "Problem"


def details(doc):
    st.caption(f"Pomiar: {doc['measured_at']}")
    for error in doc.get("errors", []):
        st.error(error)
    gsc = doc.get("gsc")
    tabs = st.tabs(["Wyniki GSC", "Anomalie", "Strony", "Zapytania", "Technika"])
    with tabs[0]:
        if not gsc:
            st.warning("Brak danych GSC.")
        else:
            st.caption("Search / web; dane finalne; America/Los_Angeles. Sumy pochodzą z agregacji property, nie z tabel zapytań.")
            rows = []
            for label, period in gsc["periods"].items():
                rows.append({"Okres": label, "Tydzień": period["week"], **(period["totals"] or {}),
                             "Początek tygodnia": period["start"], "Koniec tygodnia ISO": period["end"],
                             "Dane do": period.get("coverage", {}).get("effective_end") or max(period.get("available_dates", []), default="Brak danych"),
                             "Dostępne dni": len(period.get("available_dates", [])),
                             "Brakujące dni": ", ".join(period["missing_dates"])})
            table(rows)
            coverage = gsc["periods"]["T"].get("coverage")
            if coverage and not coverage["complete"]:
                st.warning(f"T: dane do {coverage['effective_end'] or 'brak'} — {coverage['available_day_count']}/{coverage['expected_day_count']} dni. Nie mamy pełnych danych tygodnia; porównania są wstępne.")
            for warning in gsc.get("warnings", []):
                st.warning(warning)
    with tabs[1]:
        if gsc:
            st.caption("Baza T-1...T-4; odchylenie próbki ddof=1; pasmo 2 sigma; próg pogorszenia WoW z operatorem >. Wyższa pozycja oznacza pogorszenie.")
            table([{"Metryka": metric, **summary} for metric, summary in gsc["anomalies"]["metrics"].items()])
        else:
            st.caption("Brak danych.")
    with tabs[2]:
        if gsc:
            st.caption("Tylko zwrócone wiersze GSC. Brak URL-a oznacza zero jedynie w tym rankingu, nie dowód braku ruchu.")
            st.subheader("Wzrosty stron")
            table(gsc["anomalies"]["pages"]["top_gains"])
            st.subheader("Spadki stron")
            table(gsc["anomalies"]["pages"]["top_losses"])
            with st.expander("Wszystkie zwrócone strony T"):
                table(gsc["periods"]["T"]["pages"]["rows"])
    with tabs[3]:
        if gsc:
            brand = gsc["anomalies"]["brand_non_brand"]
            st.subheader("Brand / non-brand")
            if not doc["configuration"]["brand_terms"]:
                st.warning("Brak fraz brandowych w konfiguracji: podział nie został oceniony.")
            else:
                table([{"Okres": period, "Typ": bucket, **values} for period, buckets in brand["periods"].items() for bucket, values in buckets.items()])
                table([{"Typ": bucket, "Metryka": metric, **values} for bucket, metrics in brand["wow"].items() for metric, values in metrics.items()])
            st.subheader("Zapytania nieobecne w zwróconych danych T-1...T-4")
            st.caption("Nie oznacza pierwszego wyszukiwania w historii. Zapytania anonimowe i pominięte przez GSC są poza zakresem.")
            table(gsc["anomalies"]["new_queries"])
            with st.expander("Wszystkie zwrócone zapytania T"):
                table(gsc["periods"]["T"]["queries"]["rows"])
    with tabs[4]:
        tech = doc["tech"]
        if tech["state"] != "measured":
            st.info(STATES[tech["state"]])
        else:
            st.caption(f"Pomiar aktualny: {tech['checked_at']}. Nie jest rekonstrukcją historyczną. Poprzedni pomiar: {tech.get('comparison_measurement_at') or 'brak'}.")
            table([{"Kontrola": section, "Status": tech[section]["status"], "Stan": check_state(tech[section])}
                   for section in ("robots", "sitemap", "pagespeed", "inspection")])
            table(tech["sample"])
            for section in ("pagespeed", "inspection"):
                table([{**row, "Stan": check_state(row)} for row in tech[section]["urls"]])
            st.subheader("Zmiany od poprzedniego zapisanego pomiaru")
            if tech["changed_since_last_week"] is None:
                st.info("Brak porównania.")
            elif not tech["changed_since_last_week"]:
                st.caption("Bez zmian w porównywalnych sygnałach. Rotacja próbki nie dowodzi nowej usterki.")
            table(tech["changes"])
            with st.expander("Szczegóły techniczne"):
                st.json(tech)


def report_view(documents, signature):
    numeric = [doc for doc in documents if "summary" in doc]
    if not numeric:
        return
    state_key = "weekly:ai:" + repr(signature)
    saved_reports = sorted((monitor.STORE / "reports" / numeric[0]["week"]).glob("*.json"), reverse=True)
    if saved_reports:
        saved_path = st.selectbox("Zapisane raporty", saved_reports, format_func=lambda path: path.stem, key="weekly:saved_report")
        if st.button("Odczytaj zapisany raport", key="weekly:read_report"):
            try:
                payload = read_json(saved_path)
                normalized = lambda rows: [{key: value for key, value in doc.items() if key != "saved_path"} for doc in rows]
                if normalized(payload["measurements"]) != normalized(numeric):
                    raise ValueError("Raport dotyczy innych pomiarow")
                review = verify_report(numeric, payload["markdown"], payload["interpretations"])
                if not (review.get("verified") or review["ready"]):
                    raise ValueError("Raport niezgodny ze zrodlami")
                st.session_state[state_key] = {"interpretations": payload["interpretations"], "reviews": payload["ai_reviews"]}
            except Exception:
                st.error("Zapisany raport jest niezgodny z wybranymi pomiarami lub nie przeszedł weryfikacji.")
    offline = st.session_state.get("weekly:mode") == "Odczyt zapisanych wyników"
    if not ai.available():
        st.caption("AI nie jest skonfigurowane. Dostępny jest raport liczbowy i sygnały do ręcznej oceny, bez interpretacji modelowej.")
    if st.button("Interpretuj z AI", disabled=offline or not ai.available(), key="weekly:interpret"):
        interpretations, reviews = {}, {}
        for doc in numeric:
            if not doc.get("gsc"):
                continue
            try:
                with st.spinner(f"Interpretacja i recenzja: {doc['name']}"):
                    result = ai.interpret(doc)
                interpretations[doc["site"]] = result["interpretation"]
                reviews[doc["site"]] = result["review"]
            except Exception:
                st.error(f"{doc['name']}: interpretacja AI nie przeszła weryfikacji lub jest niedostępna. Wyniki liczbowe pozostają dostępne.")
        st.session_state[state_key] = {"interpretations": interpretations, "reviews": reviews}
    ai_result = st.session_state.get(state_key, {"interpretations": {}, "reviews": {}})
    interpretations = ai_result["interpretations"]
    markdown = render_report(numeric, interpretations)
    verification = verify_report(numeric, markdown, interpretations)
    if verification["ready"]:
        st.caption("Raport liczbowy: gotowy po weryfikacji pól danych.")
    elif verification.get("verified"):
        st.warning("SZKIC: pola i obliczenia są zgodne z zapisem, ale dane GSC są niepełne. Sygnały anomalii nie są potwierdzoną oceną pełnego tygodnia.")
    else:
        st.error("Raport nie przeszedł weryfikacji danych.")
    if interpretations:
        st.caption("Interpretacje AI: po walidacji sygnałów i osobnej recenzji modelowej. Hipotezy nie są potwierdzonymi przyczynami.")
    st.markdown(markdown)
    payload = {"measurements": numeric, "interpretations": interpretations, "ai_reviews": ai_result["reviews"],
               "report_context": build_context(numeric), "markdown": markdown, "verification": verification}
    encoded = json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False).encode()
    week = numeric[0]["week"]
    st.download_button("Pobierz JSON", encoded, f"monitoring_{week}.json", "application/json", key="weekly:json")
    st.download_button("Pobierz raport Markdown", markdown.encode("utf-8"), f"raport_{week}.md", "text/markdown", key="weekly:md")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("raport.md", markdown)
        zipped.writestr("report_context.json", json.dumps(build_context(numeric), ensure_ascii=True, indent=2))
        for doc in numeric:
            zipped.writestr(f"measurement_{doc['site']}.json", json.dumps(doc, ensure_ascii=True, indent=2, allow_nan=False))
        zipped.writestr("review.json", json.dumps(verification))
    st.download_button("Pobierz raport ze źródłami ZIP", archive.getvalue(), f"monitoring_{week}.zip", "application/zip", key="weekly:zip")
    if st.button("Zapisz raport", key="weekly:save_report"):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid4().hex[:8]
        try:
            write_json(monitor.STORE / "reports" / week / f"{stamp}.json", payload)
            st.success("Raport i recenzja zapisane.")
        except OSError:
            st.error("Nie udalo sie zapisac raportu.")


def render(sites, selected):
    st.header("Monitoring tygodniowy SEO")
    mode = st.sidebar.radio("Tryb monitoringu", ["Nowy pomiar", "Odczyt zapisanych wyników"], key="weekly:mode")
    default_week = week_id(week_start())
    week = st.sidebar.text_input("Tydzień ISO", value=default_week, key="weekly:week")
    try:
        start = week_start(week)
    except ValueError:
        st.error("Wybierz zakończony tydzień ISO w formacie YYYY-Www (America/Los_Angeles).")
        return
    st.sidebar.caption(f"Poniedziałek: {start.isoformat()} / niedziela: {timedelta(days=6) + start}")
    paths = monitor.history(monitor.STORE)
    weeks = sorted({path.parent.parent.name for path in paths}, reverse=True)
    with st.expander("Historia tygodni"):
        st.write(", ".join(weeks) if weeks else "Brak zapisanych pomiarów.")
    signature = (selected, week, mode)
    if mode == "Nowy pomiar":
        technical = st.sidebar.checkbox("Kontrole techniczne (pomiar aktualny)", key="weekly:technical")
        signature += (technical,)
        if st.sidebar.button("Uruchom monitoring", type="primary", disabled=not selected, key="weekly:run"):
            from config import load_global_config
            try:
                credentials = load_global_config().google_credentials_path
                progress = st.progress(0.0, text="Pobieranie danych")
                try:
                    documents = monitor.collect(sites, selected, week, credentials, technical=technical,
                                                root=monitor.STORE, progress=lambda value, text: progress.progress(value, text=text))
                finally:
                    progress.empty()
                st.session_state["weekly:result"] = {"signature": signature, "documents": documents,
                                                      "id": uuid4().hex}
            except Exception:
                st.error("Nie udało się rozpocząć pomiaru. Sprawdź konfigurację autoryzacji.")
    else:
        choices = []
        for key in selected:
            candidates = [path for path in paths if path.parent.name == key and path.parent.parent.name == week]
            if not candidates:
                st.warning(f"{sites[key].name}: brak zapisanych pomiarów tego tygodnia.")
                continue
            path = st.selectbox(f"Pomiar: {sites[key].name}", candidates, format_func=lambda value: value.stem, key="weekly:saved:" + key)
            choices.append((key, path))
        signature += (tuple(str(path) for _, path in choices),)
        if st.button("Odczytaj wyniki", disabled=not choices, key="weekly:load"):
            documents = []
            for key, path in choices:
                try:
                    documents.append(monitor.load_measurement(path, sites[key], week, monitor.STORE))
                except Exception:
                    st.error(f"{sites[key].name}: zapis jest uszkodzony lub niezgodny z konfiguracja.")
            st.session_state["weekly:result"] = {"signature": signature, "documents": documents,
                                                  "id": uuid4().hex}
    saved = st.session_state.get("weekly:result")
    if not saved or saved["signature"] != signature:
        st.info("Wybierz serwisy i uruchom pomiar lub odczytaj zapisane wyniki.")
        return
    documents = saved["documents"]
    incomplete = [doc for doc in documents if doc.get("summary", {}).get("gsc_data_quality") == "INCOMPLETE"]
    if incomplete:
        st.warning("Niepełne dane GSC: " + ", ".join(doc["name"] for doc in incomplete)
                   + ". Wyniki są wstępne. Brakujący dzień nie oznacza zera ruchu; przed oceną tygodnia pobierz go ponownie po uzupełnieniu danych przez GSC.")
    table([{"Serwis": doc["name"], "Status": doc.get("summary", {}).get("status", "WARN"),
            "GSC": STATES.get(doc.get("summary", {}).get("gsc_state"), "Blad konfiguracji"),
            "Technika": STATES.get(doc.get("summary", {}).get("tech_state"), "Nie wykonano"),
            "Pomiar": doc["measured_at"]} for doc in documents])
    for doc in documents:
        if "summary" not in doc:
            st.error(f"{doc['name']}: nieprawidlowa konfiguracja monitoringu.")
            continue
        with st.expander(doc["name"], expanded=len(documents) == 1):
            details(doc)
    with st.expander("Raport po polsku", expanded=True):
        report_view(documents, saved["id"])