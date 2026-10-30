"""Streamlit dashboard. Run with: streamlit run dashboard.py."""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import zipfile
from datetime import date, timedelta
from types import SimpleNamespace

import altair as alt
import pandas as pd
import streamlit as st

import app
from analysis import Period
from analytics.dashboard import (
    AnalysisRequest,
    IDENTITY,
    SOURCE_LABELS,
    VIEWS,
    collect_dashboard,
    combine_sources,
    daily_overview,
    parse_urls,
    properties_overlap,
    select_sources,
    url_candidates,
)
from analytics.comparison import build_category_tree
from analytics.ymyl import tag_ymyl
from config import ConfigError, load_global_config, load_sites


PRESETS = {
    "Tydzień do tygodnia": 1,
    "Dwa tygodnie do dwóch tygodni": 2,
    "Miesiąc do miesiąca (30 dni)": 3,
    "Tydzień rok do roku": 4,
    "Miesiąc rok do roku": 5,
    "Miesiąc kalendarzowy": 6,
    "Równe okresy (do 16 miesięcy)": 8,
    "Własne daty": 7,
}
COLORS = ("#007f75", "#c44240", "#3973b9", "#bb7900", "#9b5390", "#518333", "#cf6341", "#546a76")
COLUMN_LABELS = {
    "site_key": "ID serwisu", "site_name": "Serwis", "source": "Źródło",
    "period": "Okres", "date": "Data", "day": "Dzień okresu", "page": "URL", "url": "URL",
    "category_path": "Kategoria", "depth": "Poziom", "number_of_urls": "Liczba URL-i",
    "current_clicks": "Kliknięcia", "previous_clicks": "Kliknięcia poprzednio",
    "current_impressions": "Wyświetlenia", "previous_impressions": "Wyświetlenia poprzednio",
    "clicks_change": "Zmiana kliknięć", "clicks_change_pct": "Zmiana kliknięć (%)",
    "impressions_change": "Zmiana wyświetleń", "impressions_change_pct": "Zmiana wyświetleń (%)",
    "current_ctr": "CTR", "previous_ctr": "CTR poprzednio", "status": "Status", "error": "Komunikat",
    "clicks": "Kliknięcia", "impressions": "Wyświetlenia", "device": "Urządzenie", "ymyl": "YMYL",
    "current_position": "Pozycja", "previous_position": "Pozycja poprzednio", "position_change": "Zmiana pozycji",
    "share_clicks_pct": "Udział kliknięć (%)", "share_impressions_pct": "Udział wyświetleń (%)",
    "current_date": "Data bieżąca", "previous_date": "Data porównawcza",
}


def _password_gate():
    password = os.environ.get("APP_PASSWORD", "").strip()
    if not password or st.session_state.get("_auth_ok"):
        return
    entered = st.text_input("Hasło dostępu", type="password")
    if entered and hmac.compare_digest(entered.encode(), password.encode()):
        st.session_state["_auth_ok"] = True
        st.rerun()
    if entered:
        st.error("Błędne hasło.")
    st.stop()


def _set_sites(keys):
    st.session_state["selected_sites"] = list(keys)


def _single_period(label, today):
    end = today - timedelta(days=app.GSC_LAG_DAYS)
    if label == "Maksymalny zakres (16 miesięcy)":
        start = (pd.Timestamp(today) - pd.DateOffset(months=16)).date()
    elif label == "Ostatni pełny miesiąc":
        return app._compute_preset_periods(6, today)[0]
    else:
        days = {"Ostatnie 7 dni": 7, "Ostatnie 14 dni": 14, "Ostatnie 30 dni": 30, "Ostatni rok (365 dni)": 365}[label]
        start = end - timedelta(days=days - 1)
    return Period(start.isoformat(), end.isoformat())


def _equal_periods(today, days=None, months=None):
    history = _single_period("Maksymalny zakres (16 miesięcy)", today)
    end = date.fromisoformat(history.end)
    maximum = ((end - date.fromisoformat(history.start)).days + 1) // 2
    if months is not None:
        end_exclusive = pd.Timestamp(end + timedelta(days=1))
        days = (end_exclusive - (end_exclusive - pd.DateOffset(months=months))).days
    length = maximum if days is None else days
    if not 1 <= length <= maximum:
        raise ValueError("Oba równe okresy muszą mieścić się w historii GSC.")
    start = end - timedelta(days=length - 1)
    return (
        Period(start.isoformat(), end.isoformat()),
        Period((start - timedelta(days=length)).isoformat(), (start - timedelta(days=1)).isoformat()),
    )


def _period_controls(view):
    today = date.today()
    prefix = f"settings:{view}"
    compare = st.sidebar.checkbox("Porównaj okresy", value=view in VIEWS[:2], key=prefix + ":compare")
    labels = list(PRESETS) if compare else [
        "Ostatnie 7 dni", "Ostatnie 14 dni", "Ostatnie 30 dni", "Ostatni rok (365 dni)",
        "Ostatni pełny miesiąc", "Maksymalny zakres (16 miesięcy)", "Własne daty",
    ]
    default = len(labels) - 1 if view in VIEWS[2:] else 0
    label = st.sidebar.selectbox("Okres", labels, index=default, key=prefix + ":preset")
    if label == "Równe okresy (do 16 miesięcy)":
        maximum_period, _ = _equal_periods(today)
        maximum = (date.fromisoformat(maximum_period.end) - date.fromisoformat(maximum_period.start)).days + 1
        unit = st.sidebar.radio("Długość okresu", ["Maksimum", "Dni", "Miesiące"], key=prefix + ":unit")
        days, months = None, None
        if unit == "Dni":
            days = st.sidebar.slider("Dni w każdym okresie", 1, maximum, maximum, key=prefix + ":days")
        elif unit == "Miesiące":
            end = pd.Timestamp(maximum_period.end) + pd.Timedelta(days=1)
            max_months = max(count for count in range(1, 17) if (end - (end - pd.DateOffset(months=count))).days <= maximum)
            months = st.sidebar.slider("Miesiące w bieżącym okresie", 1, max_months, max_months, key=prefix + ":months")
        return _equal_periods(today, days=days, months=months)
    if label != "Własne daty":
        if not compare:
            return _single_period(label, today), None
        return app._compute_preset_periods(PRESETS[label], today)
    start = st.sidebar.date_input("Od", today - timedelta(days=32), key=prefix + ":start")
    end = st.sidebar.date_input("Do", today - timedelta(days=3), key=prefix + ":end")
    current = Period(start.isoformat(), end.isoformat())
    previous = None
    if compare:
        previous_start = st.sidebar.date_input("Poprzedni okres od", today - timedelta(days=62), key=prefix + ":previous_start")
        previous_end = st.sidebar.date_input("Poprzedni okres do", today - timedelta(days=33), key=prefix + ":previous_end")
        previous = Period(previous_start.isoformat(), previous_end.isoformat())
    return current, previous


def _url_controls(sites):
    pasted = st.text_area("Adresy URL", key="url_input", height=140)
    uploaded = st.file_uploader("Plik z adresami", type=["txt", "csv"], key="url_file")
    text = pasted
    if uploaded is not None:
        text += "\n" + uploaded.getvalue().decode("utf-8-sig", errors="replace")
    urls = parse_urls(text)
    assignments, unresolved, rows = [], [], []
    for url in urls:
        candidates = url_candidates(url, sites)
        owner = candidates[0] if len(candidates) == 1 else None
        if len(candidates) > 1:
            owner = st.selectbox(
                f"Serwis dla {url}", [None] + candidates,
                format_func=lambda key: "Wybierz przypisanie" if key is None else f"{sites[key].name} ({key})",
                key="url_owner:" + hashlib.sha256(url.encode()).hexdigest(),
            )
        if owner is not None:
            assignments.append((url, owner))
        else:
            unresolved.append(url)
        rows.append({"url": url, "site_name": sites[owner].name if owner else "Nieprzypisany", "status": "OK" if owner else "Niejednoznaczny" if candidates else "Poza wybranymi serwisami"})
    if rows:
        _table(pd.DataFrame(rows), "Przypisanie URL-i", "url_assignments.csv")
    return tuple(assignments), urls, unresolved


def _table(frame, title, filename):
    st.subheader(title)
    if frame.empty:
        st.caption("Brak danych.")
        return
    frame = frame.dropna(axis=1, how="all")
    front = [column for column in IDENTITY if column in frame]
    frame = frame[front + [column for column in frame if column not in front]].copy()
    if "source" in frame:
        frame["source"] = frame["source"].replace(SOURCE_LABELS)
    if "period" in frame:
        frame["period"] = frame["period"].replace({"current": "Bieżący", "previous": "Poprzedni"})
    st.dataframe(frame, hide_index=True, use_container_width=True, column_config={column: COLUMN_LABELS.get(column, column) for column in frame})
    st.download_button("Pobierz CSV", frame.to_csv(index=False).encode("utf-8-sig"), file_name=filename, mime="text/csv", key="download:" + filename)


def _chart(daily, request, sites, metric, show_sum, show_previous):
    if daily.empty:
        return
    data = daily.groupby(["site_key", "period", "date", "day"], as_index=False, sort=False)[metric].sum(min_count=1)
    if not show_previous:
        data = data[data["period"] == "current"].copy()
    data = data.dropna(subset=[metric])
    if data.empty:
        return
    labels = {key: f"{sites[key].name} ({key})" for key in request.site_keys}
    data["Serwis"] = data["site_key"].map(labels)
    if show_sum:
        summed = data.groupby(["period", "date", "day"], as_index=False)[metric].sum(min_count=1)
        summed["Serwis"] = "Suma serwisów"
        data = pd.concat([data, summed], ignore_index=True)
    data["Data"] = pd.to_datetime(data["date"])
    data["Oś czasu"] = pd.Timestamp(request.current.start) + pd.to_timedelta(data["day"] - 1, unit="D")
    data["Okres"] = data["period"].replace({"current": "Bieżący", "previous": "Poprzedni"})
    domain = list(labels.values()) + (["Suma serwisów"] if show_sum else [])
    colors = [COLORS[list(sites).index(key) % len(COLORS)] for key in request.site_keys] + (["#8b8b8b"] if show_sum else [])
    metric_label = "Kliknięcia" if metric == "clicks" else "Wyświetlenia"
    chart = alt.Chart(data).mark_line().encode(
        x=alt.X("Oś czasu:T", title="Data"),
        y=alt.Y(f"{metric}:Q", title=metric_label),
        color=alt.Color("Serwis:N", scale=alt.Scale(domain=domain, range=colors), legend=alt.Legend(orient="bottom")),
        strokeDash=alt.StrokeDash("Okres:N", scale=alt.Scale(domain=["Bieżący", "Poprzedni"], range=[[1, 0], [6, 4]]), legend=alt.Legend(orient="bottom")),
        tooltip=["Serwis:N", "Okres:N", alt.Tooltip("Data:T", format="%Y-%m-%d"), alt.Tooltip(f"{metric}:Q", title=metric_label, format=",.0f")],
    ).properties(height=340)
    st.altair_chart(chart, use_container_width=True)
    if show_previous:
        st.caption("Poprzedni okres wyrównany po numerze dnia. Dymek pokazuje jego rzeczywistą datę.")


def _kpis(daily, has_previous):
    if daily.empty:
        return
    current = daily[daily["period"] == "current"]
    previous = daily[daily["period"] == "previous"]
    columns = st.columns(2)
    for column, metric, label in zip(columns, ("clicks", "impressions"), ("Kliknięcia", "Wyświetlenia")):
        value = current[metric].sum(min_count=1)
        if pd.isna(value):
            continue
        delta = None
        if has_previous:
            before = previous[metric].sum(min_count=1)
            if pd.notna(before):
                delta = f"{value - before:+,.0f}".replace(",", " ")
                if before:
                    delta += f" ({(value - before) / before * 100:+.1f}%)"
        column.metric(label, f"{value:,.0f}".replace(",", " "), delta)


def _category_details(bundle, selected_sources, sites_config, suffix):
    request = bundle["request"]
    pages = select_sources(bundle["pages"], selected_sources)
    trees = select_sources(bundle["trees"], selected_sources)
    if pages.empty:
        return
    if suffix == "total":
        pages = combine_sources(pages, ["site_key", "site_name", "page", "category_path"])
        frames = []
        for key in request.site_keys:
            subset = pages[pages["site_key"] == key]
            if not subset.empty:
                site = sites_config.sites[key]
                tree_input = subset if request.previous else subset.assign(previous_clicks=0.0, previous_impressions=0.0)
                tree = tag_ymyl(build_category_tree(tree_input), site.ymyl_paths, site.ymyl_exclude_paths)
                if request.previous is None:
                    tree = tree.drop(columns=[column for column in tree if column.startswith("previous_") or "_change" in column])
                frames.append(tree.assign(site_key=key, site_name=site.name))
        trees = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not trees.empty:
        if request.previous:
            metric = "clicks_change"
            _table(trees[trees[metric] > 0].sort_values(metric, ascending=False).head(20), "Kategorie rosnące", f"{suffix}_growing.csv")
            _table(trees[trees[metric] < 0].sort_values(metric).head(20), "Kategorie spadające", f"{suffix}_declining.csv")
        else:
            _table(trees.sort_values("current_clicks", ascending=False).head(20), "Kategorie z największym ruchem", f"{suffix}_top_categories.csv")
        _table(trees, "Wszystkie kategorie", f"{suffix}_categories.csv")
    _table(pages, "Wszystkie URL-e", f"{suffix}_pages.csv")
    ymyl_frames = []
    for key in request.site_keys:
        subset = pages[pages["site_key"] == key]
        if subset.empty:
            continue
        site = sites_config.sites[key]
        tagged = tag_ymyl(subset, site.ymyl_paths, site.ymyl_exclude_paths)
        grouped = combine_sources(tagged, ["site_key", "site_name", "ymyl"] + (["source"] if "source" in tagged else []))
        ymyl_frames.append(grouped)
    if ymyl_frames:
        _table(pd.concat(ymyl_frames, ignore_index=True), "YMYL i pozostałe treści", f"{suffix}_ymyl.csv")
    if "web" in selected_sources:
        _table(select_sources(bundle["devices"], ("web",)), "Urządzenia — Search", f"{suffix}_devices.csv")
    if request.previous and len(request.site_keys) == 2:
        pair = []
        for key in request.site_keys:
            tree = trees[trees["site_key"] == key]
            if tree.empty:
                break
            pair.append(SimpleNamespace(site_key=key, site_name=sites_config.sites[key].name, tree=tree))
        if len(pair) == 2:
            groups = app._build_group_comparisons(sites_config, *pair)
            if groups:
                _table(app._group_comparison_df(groups), "Porównanie grup kategorii", f"{suffix}_groups.csv")


def _url_details(bundle, selected_sources, suffix):
    pages = select_sources(bundle["pages"], selected_sources).dropna(axis=1, how="all")
    if pages.empty:
        return
    if suffix == "total":
        pages = combine_sources(pages, ["site_key", "site_name", "url"])
    _table(pages, "Metryki URL-i", f"{suffix}_urls.csv")
    totals = select_sources(bundle["totals"], selected_sources)
    if not totals.empty:
        selected = pages.groupby(["site_key", "site_name"], as_index=False)[["current_clicks", "current_impressions"]].sum()
        whole = totals[totals["period"] == "current"].groupby("site_key")[["clicks", "impressions"]].sum()
        summary = selected.join(whole, on="site_key")
        for metric in ("clicks", "impressions"):
            summary[f"share_{metric}_pct"] = (summary[f"current_{metric}"] / summary[metric] * 100).where(summary[metric] != 0)
        summary["current_ctr"] = (summary["current_clicks"] / summary["current_impressions"]).where(summary["current_impressions"] != 0)
        _table(summary, "Udział wskazanych URL-i w ruchu serwisów", f"{suffix}_url_share.csv")


def _daily_details(daily, threshold, suffix):
    current = daily[daily["period"] == "current"].set_index(IDENTITY + ["day"])
    previous = daily[daily["period"] == "previous"].set_index(IDENTITY + ["day"])
    if not previous.empty:
        aligned = current[["date", "clicks", "impressions"]].add_prefix("current_").join(previous[["date", "clicks", "impressions"]].add_prefix("previous_"), how="outer")
        aligned["clicks_change"] = aligned["current_clicks"].fillna(0) - aligned["previous_clicks"].fillna(0)
        aligned["clicks_change_pct"] = (aligned["clicks_change"] / aligned["previous_clicks"] * 100).where(aligned["previous_clicks"] != 0)
        anomalies = aligned[(aligned["clicks_change_pct"].abs() >= threshold) | ((aligned["previous_clicks"] == 0) & (aligned["current_clicks"] > 0))]
        _table(anomalies.reset_index(), f"Odchylenia dzienne ≥ {threshold}% lub wzrost od zera", f"{suffix}_anomalies.csv")
    _table(daily, "Dane dzienne wszystkich serwisów", f"{suffix}_daily.csv")


def _download_bundle(bundle):
    buffer = io.BytesIO()
    request = bundle["request"]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("daily", "pages", "trees", "devices", "totals", "statuses", "url_daily"):
            archive.writestr(f"{name}.csv", bundle[name].to_csv(index=False).encode("utf-8-sig"))
        metadata = pd.DataFrame([{
            "view": request.view, "sites": ",".join(request.site_keys), "sources": ",".join(request.sources),
            "current_start": request.current.start, "current_end": request.current.end,
            "previous_start": request.previous.start if request.previous else "",
            "previous_end": request.previous.end if request.previous else "",
            "export_url_daily": request.export_url_daily,
        }])
        archive.writestr("parameters.csv", metadata.to_csv(index=False).encode("utf-8-sig"))
    st.download_button("Pobierz wszystkie wyniki (ZIP)", buffer.getvalue(), "gsc_dashboard.zip", "application/zip")


def _render_results(bundle, sites_config, threshold):
    request = bundle["request"]
    for notice in bundle["notices"]:
        st.warning(notice)
    statuses = bundle["statuses"]
    for row in statuses.itertuples():
        if row.status not in ("ok", "no_data"):
            st.warning(f"{row.site_name} / {SOURCE_LABELS[row.source]}: {row.error}")
    _download_bundle(bundle)
    if request.export_url_daily:
        if bundle["url_daily"].empty:
            st.info("Brak dziennych danych URL-i do eksportu.")
        else:
            st.download_button(
                "Pobierz URL-e z datami i ruchem (CSV)",
                bundle["url_daily"].to_csv(index=False).encode("utf-8-sig"),
                file_name="url_daily.csv", mime="text/csv",
            )
    if bundle["daily"].empty:
        st.info("Brak danych dla wybranych serwisów i źródeł.")
        _table(statuses, "Status pobierania", "statuses.csv")
        return
    controls = st.columns(3)
    show_sum = controls[0].checkbox("Suma serwisów", key=request.view + ":show_sum")
    show_previous = controls[1].checkbox("Pokaż poprzedni okres", value=True, key=request.view + ":show_previous") if request.previous else False
    metric = "clicks"
    if request.view in (VIEWS[0], VIEWS[3]):
        metric = "impressions" if controls[2].radio("Metryka", ["Kliknięcia", "Wyświetlenia"], horizontal=True, key=request.view + ":metric") == "Wyświetlenia" else "clicks"
    if properties_overlap({key: sites_config.sites[key] for key in request.site_keys}):
        st.warning("Zakresy wybranych properties nakładają się. Suma może liczyć część ruchu więcej niż raz.")
    if request.view == VIEWS[2]:
        tabs, subsets = [st.container()], [("brand", ("web",))]
        _table(pd.DataFrame([{"site_name": sites_config.sites[key].name, "frazy": ", ".join(sites_config.sites[key].brand_terms)} for key in request.site_keys]), "Frazy brandowe", "brand_terms.csv")
    else:
        subsets = [("total", request.sources)] + [(source, (source,)) for source in request.sources]
        tabs = st.tabs(["Łącznie"] + [SOURCE_LABELS[source] for source in request.sources])
    for tab, (suffix, sources) in zip(tabs, subsets):
        with tab:
            status_subset = select_sources(statuses, sources)
            if (~status_subset["status"].isin(["ok", "no_data"])).any():
                st.warning("Wyniki częściowe. Podsumowania i suma obejmują tylko poprawnie pobrane dane.")
            daily = select_sources(bundle["daily"], sources)
            if daily.empty:
                st.info("Brak danych dla tego źródła.")
                continue
            _kpis(daily, request.previous is not None)
            st.subheader("Ruch wskazanych URL-i" if request.view == VIEWS[3] else "Kliknięcia brandowe" if request.view == VIEWS[2] else "Ruch wybranych serwisów")
            _chart(daily, request, sites_config.sites, metric, show_sum, show_previous)
            overview = daily_overview(daily)
            if suffix == "total":
                overview = combine_sources(overview, ["site_key", "site_name"])
            _table(overview, "Podsumowanie serwisów", f"{suffix}_overview.csv")
            if request.previous and len(request.site_keys) == 2 and suffix == "total":
                pair = overview.set_index("site_key").reindex(request.site_keys)
                dynamics = pair["clicks_change_pct"]
                if dynamics.notna().all():
                    st.metric("Różnica dynamiki kliknięć", f"{dynamics.iloc[0] - dynamics.iloc[1]:+.1f} pp")
            if request.view == VIEWS[0]:
                _daily_details(daily, threshold, suffix)
            elif request.view == VIEWS[1]:
                _category_details(bundle, sources, sites_config, suffix)
            elif request.view == VIEWS[3]:
                _url_details(bundle, sources, suffix)
            else:
                _table(daily, "Dane dzienne wszystkich serwisów", "brand_daily.csv")
    _table(statuses, "Status pobierania", "statuses.csv")


def main():
    st.set_page_config(page_title="Artemida", layout="wide")
    st.title("Artemida")
    for key in ("GOOGLE_CREDENTIALS_JSON", "SITES_YAML", "APP_PASSWORD"):
        try:
            if key in st.secrets:
                os.environ[key] = str(st.secrets[key])
        except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
            pass
    _password_gate()
    try:
        global_config, sites_config = load_global_config(), load_sites()
    except ConfigError as exc:
        st.error(str(exc))
        return
    sites = sites_config.sites
    view = st.sidebar.radio("Widok", VIEWS, key="view")
    actions = st.sidebar.columns(2)
    actions[0].button("Wszystkie", on_click=_set_sites, args=(tuple(sites),))
    actions[1].button("Wyczyść", on_click=_set_sites, args=((),))
    selected = st.sidebar.multiselect("Serwisy", list(sites), format_func=lambda key: sites[key].name, key="selected_sites")
    selected = tuple(key for key in sites if key in selected)
    if view == VIEWS[2]:
        sources = ("web",)
    else:
        if "selected_sources" not in st.session_state:
            st.session_state["selected_sources"] = st.session_state.get("preferred_sources", ["web"])
        sources = tuple(st.sidebar.multiselect("Źródła", list(SOURCE_LABELS), format_func=SOURCE_LABELS.get, key="selected_sources"))
        st.session_state["preferred_sources"] = list(sources)
    preference_prefix = "settings:" + view + ":"
    for key, value in st.session_state.get("period_preferences", {}).items():
        if key.startswith(preference_prefix) and key not in st.session_state:
            st.session_state[key] = value
    current, previous = _period_controls(view)
    preferences = dict(st.session_state.get("period_preferences", {}))
    preferences.update({key: value for key, value in st.session_state.items() if key.startswith(preference_prefix)})
    st.session_state["period_preferences"] = preferences
    current_days = (date.fromisoformat(current.end) - date.fromisoformat(current.start)).days + 1
    st.sidebar.caption(f"Bieżący: {current.start} — {current.end} ({current_days} dni)")
    if previous:
        previous_days = (date.fromisoformat(previous.end) - date.fromisoformat(previous.start)).days + 1
        st.sidebar.caption(f"Poprzedni: {previous.start} — {previous.end} ({previous_days} dni)")
    threshold = st.sidebar.slider("Próg dziennego odchylenia (%)", 10, 200, 30, step=10) if view == VIEWS[0] and previous else 30
    with st.sidebar.expander("Zaawansowane"):
        use_cache = st.checkbox("Użyj cache", value=True, key="use_cache")
    export_url_daily = False
    if view != VIEWS[2]:
        export_url_daily = st.sidebar.checkbox(
            "Eksport URL-i z datami i ruchem", key=preference_prefix + "export_url_daily",
            help="Dodatkowe pobieranie dzień po dniu. Długie okresy i duże serwisy mogą wymagać wielu zapytań i dużego pliku. GSC zwraca najważniejsze wiersze, do 50 000 dziennie na serwis i źródło; eksport może nie zawierać wszystkich URL-i.",
        )
    st.header(view)
    assignments, urls, unresolved = (), [], []
    if view == VIEWS[3]:
        assignments, urls, unresolved = _url_controls({key: sites[key] for key in selected})
    valid_dates = current.start <= current.end and (previous is None or previous.start <= previous.end)
    if not valid_dates:
        st.error("Data początkowa nie może być późniejsza niż końcowa.")
    if previous and (pd.Timestamp(current.end) - pd.Timestamp(current.start)) != (pd.Timestamp(previous.end) - pd.Timestamp(previous.start)):
        st.warning("Okresy mają różną długość. Porównanie obejmuje wszystkie dni obu okresów.")
    valid = bool(selected and sources and valid_dates and (view != VIEWS[3] or (urls and not unresolved)))
    run = st.sidebar.button("Uruchom analizę", type="primary", disabled=not valid)
    request = AnalysisRequest(view, selected, sources, current, previous, assignments, use_cache, export_url_daily)
    if run:
        progress = st.progress(0.0, text="Pobieranie danych")
        try:
            bundle = collect_dashboard(request, sites, global_config.google_credentials_path, lambda fraction, name: progress.progress(fraction, text=name))
            st.session_state["result:" + view] = bundle
        finally:
            progress.empty()
    saved = st.session_state.get("result:" + view)
    if saved and saved["request"] == request:
        _render_results(saved, sites_config, threshold)
    elif saved:
        st.info("Parametry zostały zmienione. Wyniki wymagają ponownego uruchomienia analizy.")
    elif not selected:
        st.info("Nie wybrano serwisów.")
    elif not sources:
        st.info("Nie wybrano źródeł.")
    elif view == VIEWS[3] and unresolved:
        st.warning("Część URL-i nie ma jednoznacznego przypisania do wybranych serwisów.")


if __name__ == "__main__":
    main()