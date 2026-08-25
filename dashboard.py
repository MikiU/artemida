"""Lokalny dashboard Streamlit dla GSC Analyzer (tryby 1/2/3).

Uruchomienie:
    streamlit run dashboard.py

Interfejs jest cienką warstwą nad istniejącą logiką (analysis.py, analytics/*),
więc cała matematyka to te same, przetestowane funkcje – dashboard je tylko wywołuje.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import date, timedelta

import streamlit as st

import app  # reużycie funkcji budujących DataFrame (bez duplikacji logiki)
from analysis import (
    Period,
    analyze_site,
    analyze_site_multi_source,
    analyze_site_sources,
    build_url_metrics,
)
from config import ConfigError, load_global_config, load_sites
from services.gsc_service import GSCError, get_gsc_data
from services.sitemap_service import SitemapError

st.set_page_config(page_title="GSC Analyzer", layout="wide")
st.title("GSC Analyzer – dashboard")

# Hosting (Streamlit Cloud): przenieś sekrety do środowiska, by config je zobaczył.
for _secret_key in ("GOOGLE_CREDENTIALS_JSON", "SITES_YAML", "APP_PASSWORD"):
    try:
        if _secret_key in st.secrets:
            os.environ[_secret_key] = str(st.secrets[_secret_key])
    except Exception:
        pass


def _password_gate() -> None:
    """Prosta bramka hasłem, jeśli ustawiono APP_PASSWORD (sekret/env)."""
    password = os.environ.get("APP_PASSWORD", "").strip()
    if not password:
        st.sidebar.caption("⚠️ Dostęp otwarty (brak APP_PASSWORD).")
        return
    if st.session_state.get("_auth_ok"):
        return
    entered = st.text_input("Hasło dostępu", type="password")
    if entered and entered == password:
        st.session_state["_auth_ok"] = True
        st.rerun()
    if entered:
        st.error("Błędne hasło.")
    st.stop()


_password_gate()


@st.cache_resource
def _load_config():
    return load_global_config(), load_sites()


try:
    global_config, sites_config = _load_config()
except ConfigError as exc:
    st.error(str(exc))
    st.stop()

sites = sites_config.sites
site_keys = list(sites.keys())

st.sidebar.header("Ustawienia")
mode = st.sidebar.radio(
    "Tryb",
    [
        "1 – Porównaj DWA serwisy",
        "2 – Ranking kategorii (wiele)",
        "3 – Search/Discover/YMYL",
        "4 – Jeden serwis (Search/Discover/News)",
        "5 – Lista URL-i → dane",
    ],
)
selected = st.sidebar.multiselect(
    "Serwisy", options=site_keys, format_func=lambda k: sites[k].name
)

PRESETS = {
    "Tydzień do tygodnia": 1,
    "Dwa tygodnie do dwóch tygodni": 2,
    "Miesiąc do miesiąca (30 dni)": 3,
    "Tydzień rok do roku (YoY)": 4,
    "Miesiąc rok do roku (YoY)": 5,
    "Miesiąc kalendarzowy": 6,
    "Własne daty": 7,
}

single_site = None
preset_id = 7
sources_sel: list[str] = []
url_sources: list[str] = []
compare_urls = False
if mode.startswith("4"):
    single_site = st.sidebar.selectbox(
        "Serwis", site_keys, format_func=lambda k: sites[k].name
    )
    preset_label = st.sidebar.selectbox("Okres", list(PRESETS.keys()))
    preset_id = PRESETS[preset_label]
    sources_sel = st.sidebar.multiselect(
        "Źródła",
        ["web", "discover", "news"],
        default=["web", "discover", "news"],
        format_func=lambda t: app.SOURCE_LABELS[t],
    )
elif mode.startswith("5"):
    single_site = st.sidebar.selectbox(
        "Serwis", site_keys, format_func=lambda k: sites[k].name, key="m5_site"
    )
    url_sources = st.sidebar.multiselect(
        "Źródła",
        ["web", "discover", "news"],
        default=["web"],
        format_func=lambda t: app.SOURCE_LABELS[t],
        key="m5_src",
    )
    compare_urls = st.sidebar.checkbox("Porównaj dwa okresy", value=False)

today = date.today()

# Kalendarze pokazujemy tam, gdzie są używane. W trybie 5 "Previous" tylko przy
# włączonym porównaniu – inaczej wystarczy jeden okres (start–koniec).
_need_current = (not mode.startswith("4")) or (preset_id == 7)
_need_previous = _need_current and not (mode.startswith("5") and not compare_urls)
cur_start = cur_end = prev_start = prev_end = None
if _need_current:
    st.sidebar.subheader("CURRENT PERIOD")
    cur_start = st.sidebar.date_input("Current start", today - timedelta(days=31))
    cur_end = st.sidebar.date_input("Current end", today - timedelta(days=2))
    if _need_previous:
        st.sidebar.subheader("PREVIOUS PERIOD")
        prev_start = st.sidebar.date_input("Previous start", today - timedelta(days=62))
        prev_end = st.sidebar.date_input("Previous end", today - timedelta(days=33))
    _cd = (cur_end - cur_start).days + 1
    if _need_previous:
        _pd = (prev_end - prev_start).days + 1
        st.sidebar.caption(f"Wybrano — Current: {_cd} dni · Previous: {_pd} dni")
        if cur_start > cur_end or prev_start > prev_end:
            st.sidebar.error("Data startowa jest późniejsza niż końcowa.")
        elif _cd != _pd:
            st.sidebar.warning(f"Różna długość okresów: {_cd} vs {_pd} dni.")
    else:
        st.sidebar.caption(f"Wybrano — okres: {_cd} dni")
        if cur_start > cur_end:
            st.sidebar.error("Data startowa jest późniejsza niż końcowa.")
elif mode.startswith("4"):
    _c, _p = app._compute_preset_periods(preset_id)
    _cd = (date.fromisoformat(_c.end) - date.fromisoformat(_c.start)).days + 1
    _pd = (date.fromisoformat(_p.end) - date.fromisoformat(_p.start)).days + 1
    st.sidebar.info(
        f"CURRENT: {_c.start} … {_c.end} ({_cd} dni)\n\n"
        f"PREVIOUS: {_p.start} … {_p.end} ({_pd} dni)"
    )

use_cache = st.sidebar.checkbox("Użyj cache", value=True)
run = st.sidebar.button("Uruchom analizę", type="primary")


def _period(s, e):
    return Period(s.isoformat(), e.isoformat())


def _check_periods():
    if cur_start is None or cur_end is None:
        st.error("Brak wybranego okresu.")
        return False
    if cur_start > cur_end:
        st.error("Data startowa jest późniejsza niż końcowa.")
        return False
    cur_days = (cur_end - cur_start).days + 1
    if prev_start is None or prev_end is None:
        st.caption(f"Okres: {cur_days} dni")
        return True
    prev_days = (prev_end - prev_start).days + 1
    st.caption(f"Current: {cur_days} dni · Previous: {prev_days} dni")
    if prev_start > prev_end:
        st.error("Data startowa jest późniejsza niż końcowa.")
        return False
    if cur_days != prev_days:
        st.warning(
            f"Okresy mają różną długość ({cur_days} vs {prev_days}) – "
            "porównanie może być zaburzone."
        )
    return True


def _dl(df, name):
    st.download_button(
        f"Pobierz {name}", df.to_csv(index=False).encode("utf-8"), name, "text/csv"
    )


def _analyze_sites(chosen):
    out = []
    for key in chosen:
        s = sites[key]
        try:
            with st.spinner(f"Analizuję {s.name}…"):
                out.append(
                    analyze_site(
                        s,
                        _period(cur_start, cur_end),
                        _period(prev_start, prev_end),
                        global_config.google_credentials_path,
                        use_cache,
                    )
                )
        except (GSCError, SitemapError) as exc:
            st.warning(f"[POMINIĘTO] {s.name}: {exc}")
    return out


def _analyze_sources(chosen):
    out = []
    for key in chosen:
        s = sites[key]
        try:
            with st.spinner(f"Analizuję {s.name} (Search+Discover)…"):
                out.append(
                    analyze_site_sources(
                        s,
                        _period(cur_start, cur_end),
                        _period(prev_start, prev_end),
                        global_config.google_credentials_path,
                        use_cache,
                    )
                )
        except (GSCError, SitemapError) as exc:
            st.warning(f"[POMINIĘTO] {s.name}: {exc}")
    return out


CAT_COLS = ["site_name", "category_path", "depth", "clicks_change", "clicks_change_pct"]
SRC_COLS = [
    "site_name",
    "category_path",
    "depth",
    "ymyl",
    "clicks_change",
    "clicks_change_pct",
]


def render_mode1(chosen):
    if len(chosen) != 2:
        st.info("Wybierz dokładnie DWA serwisy.")
        return
    analyses = _analyze_sites(chosen)
    if len(analyses) < 2:
        st.error("Potrzebne dwa serwisy z danymi.")
        return
    a, b = analyses[0], analyses[1]
    ta, tb = app._site_totals(a), app._site_totals(b)
    st.subheader("SITE COMPARISON")
    st.dataframe(
        app._site_comparison_df(ta, tb).set_index("site_name").T,
        use_container_width=True,
    )
    st.metric(
        "Różnica dynamiki (clicks %)",
        f"{(ta['clicks_change_pct'] - tb['clicks_change_pct']):+.1f} pp",
    )
    groups = app._build_group_comparisons(sites_config, a, b)
    if groups:
        st.subheader("CATEGORY GROUP COMPARISON")
        gdf = app._group_comparison_df(groups)
        st.dataframe(gdf, use_container_width=True)
        _dl(gdf, "category_groups_comparison.csv")
    for an in (a, b):
        with st.expander(f"{an.site_name} – kategorie top-level"):
            top = an.tree[an.tree["depth"] == 1].sort_values("clicks_change")
            st.dataframe(
                top[
                    [
                        "category_path",
                        "current_clicks",
                        "previous_clicks",
                        "clicks_change",
                        "clicks_change_pct",
                    ]
                ],
                use_container_width=True,
            )
            _dl(an.tree, f"{an.site_key}_category_tree.csv")


def render_mode2(chosen):
    analyses = _analyze_sites(chosen)
    if len(analyses) < 2:
        st.error("Potrzebne co najmniej dwa serwisy z danymi.")
        return
    combined = app._combined_categories_df(analyses)
    grow = combined[combined["clicks_change"] > 0].sort_values(
        "clicks_change", ascending=False
    ).head(20)
    decl = combined[combined["clicks_change"] < 0].sort_values("clicks_change").head(20)
    c1, c2 = st.columns(2)
    c1.subheader("TOP rosnące")
    c1.dataframe(grow[CAT_COLS], use_container_width=True)
    c2.subheader("TOP spadające")
    c2.dataframe(decl[CAT_COLS], use_container_width=True)
    if not grow.empty:
        chart = (
            grow.head(15)
            .assign(label=lambda d: d["site_name"] + " · " + d["category_path"])
            .set_index("label")["clicks_change"]
        )
        st.bar_chart(chart)
    _dl(combined, "categories_all_sites.csv")


def render_mode3(chosen):
    sources = _analyze_sources(chosen)
    if not sources:
        st.error("Żaden serwis nie zwrócił danych.")
        return
    st.subheader("SOURCE OVERVIEW")
    ov = app._source_overview_df(sources)
    st.dataframe(ov, use_container_width=True)
    _dl(ov, "source_overview.csv")

    st.subheader("YMYL vs non-YMYL")
    ym = app._ymyl_summary_df(sources)
    st.dataframe(ym, use_container_width=True)
    _dl(ym, "ymyl_summary.csv")

    st.subheader("DEVICE BREAKDOWN")
    dev = app._device_breakdown_df(sources)
    st.dataframe(dev, use_container_width=True)
    _dl(dev, "device_breakdown.csv")

    combined = app._combined_source_categories_df(sources)
    tab_s, tab_d = st.tabs(["Search", "Discover"])
    for tab, label in ((tab_s, "Search"), (tab_d, "Discover")):
        with tab:
            sub = combined[combined["source"] == label]
            grow = sub[sub["clicks_change"] > 0].sort_values(
                "clicks_change", ascending=False
            ).head(20)
            decl = sub[sub["clicks_change"] < 0].sort_values("clicks_change").head(20)
            st.write("**TOP rosnące**")
            st.dataframe(grow[SRC_COLS], use_container_width=True)
            st.write("**TOP spadające**")
            st.dataframe(decl[SRC_COLS], use_container_width=True)
    _dl(combined, "categories_by_source.csv")


M4_COLS = [
    "category_path",
    "depth",
    "current_clicks",
    "previous_clicks",
    "clicks_change",
    "clicks_change_pct",
    "number_of_urls",
    "ymyl",
]


def _compute_mode4(site_key, preset, sources_types):
    """Pobiera dane dla trybu 4 i zwraca komplet wyników (bundle) lub None."""
    if not sources_types:
        st.info("Wybierz co najmniej jedno źródło (Search/Discover/News).")
        return None
    site = sites[site_key]
    if preset == 7:
        if not _check_periods():
            return None
        current, previous = _period(cur_start, cur_end), _period(prev_start, prev_end)
    else:
        current, previous = app._compute_preset_periods(preset)
    try:
        with st.spinner("Pobieram dane GSC…"):
            results = analyze_site_multi_source(
                site, current, previous, global_config.google_credentials_path,
                sources_types, use_cache,
            )
    except (GSCError, SitemapError) as exc:
        st.error(f"{site.name}: {exc}")
        return None
    return {
        "site_key": site_key,
        "site_name": site.name,
        "current": current,
        "previous": previous,
        "sources": list(sources_types),
        "results": results,
    }


def _mode4_zip(bundle) -> bytes:
    """Buduje ZIP z osobnymi CSV (tree/pages/daily) dla każdego źródła."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for source_type in bundle["sources"]:
            sa = bundle["results"][source_type]
            zf.writestr(f"{source_type}_category_tree.csv", sa.tree.to_csv(index=False))
            zf.writestr(f"{source_type}_pages.csv", sa.pages.to_csv(index=False))
            if not sa.daily.empty:
                zf.writestr(f"{source_type}_daily.csv", sa.daily.to_csv(index=False))
    return buffer.getvalue()


def _render_mode4(bundle):
    current, previous = bundle["current"], bundle["previous"]
    cd = (date.fromisoformat(current.end) - date.fromisoformat(current.start)).days + 1
    pd_ = (date.fromisoformat(previous.end) - date.fromisoformat(previous.start)).days + 1
    if cd != pd_:
        st.warning(f"Okresy mają różną długość ({cd} vs {pd_} dni) – porównanie może być zaburzone.")
    st.info(
        f"**{bundle['site_name']}** — CURRENT {current.start}…{current.end} ({cd} dni)  "
        f"vs  PREVIOUS {previous.start}…{previous.end} ({pd_} dni)"
    )

    st.download_button(
        "⬇️ Pobierz WSZYSTKIE raporty (ZIP: Search + Discover + News)",
        _mode4_zip(bundle),
        file_name=f"{bundle['site_key']}_{current.start}_{current.end}.zip",
        mime="application/zip",
        type="primary",
    )
    st.caption("Jedno pobranie = osobne CSV dla każdego źródła (bez ponownej analizy).")

    results = bundle["results"]
    sources_types = bundle["sources"]
    tabs = st.tabs([app.SOURCE_LABELS[t] for t in sources_types])
    for tab, t in zip(tabs, sources_types):
        with tab:
            sa = results[t]
            cur = int(sa.pages["current_clicks"].sum()) if not sa.pages.empty else 0
            prev = int(sa.pages["previous_clicks"].sum()) if not sa.pages.empty else 0
            c1, c2, c3 = st.columns(3)
            c1.metric("Current clicks", f"{cur:,}".replace(",", " "))
            c2.metric("Previous clicks", f"{prev:,}".replace(",", " "))
            c3.metric("Zmiana", f"{cur - prev:+,}".replace(",", " "))
            if not sa.daily.empty:
                st.subheader("Ruch dzień po dniu (Current vs Previous)")
                st.line_chart(sa.daily.set_index("day")[["Current", "Previous"]])
                st.caption("Dzień 1 = początek okresu; linie nałożone dla porównania.")
            tree = sa.tree
            if tree.empty:
                st.write("(brak danych kategorii)")
                continue
            grow = tree[tree["clicks_change"] > 0].sort_values("clicks_change", ascending=False).head(20)
            decl = tree[tree["clicks_change"] < 0].sort_values("clicks_change").head(20)
            st.subheader("TOP rosnące kategorie")
            st.dataframe(grow[M4_COLS], use_container_width=True)
            if not grow.empty:
                st.bar_chart(grow.head(15).set_index("category_path")["clicks_change"])
            st.subheader("TOP spadające kategorie")
            st.dataframe(decl[M4_COLS], use_container_width=True)
            if not decl.empty:
                st.bar_chart(decl.head(15).set_index("category_path")["clicks_change"])
            _dl(tree, f"{t}_category_tree.csv")


def _parse_urls(pasted, uploaded):
    text = pasted or ""
    if uploaded is not None:
        try:
            text += "\n" + uploaded.getvalue().decode("utf-8", errors="ignore")
        except Exception:
            pass
    found = re.findall(r"https?://\S+", text)
    cleaned = [u.strip().strip('",;)') for u in found]
    return list(dict.fromkeys(cleaned))


def _compute_mode5(site_key, urls, sources_types, compare):
    site = sites[site_key]
    cred = global_config.google_credentials_path
    current = _period(cur_start, cur_end)
    previous = _period(prev_start, prev_end) if compare else None
    results = {}
    totals = {}
    for t in sources_types:
        try:
            with st.spinner(f"Pobieram {app.SOURCE_LABELS[t]}…"):
                cur_df = get_gsc_data(
                    current.start, current.end, site.gsc_property, cred,
                    dimensions=("page",), search_type=t, use_cache=use_cache,
                )
                prev_df = None
                if compare:
                    prev_df = get_gsc_data(
                        previous.start, previous.end, site.gsc_property, cred,
                        dimensions=("page",), search_type=t, use_cache=use_cache,
                    )
        except GSCError as exc:
            st.warning(f"{site.name} [{app.SOURCE_LABELS[t]}]: {exc}")
            continue
        results[t] = build_url_metrics(urls, cur_df, prev_df, with_position=(t == "web"))
        # Suma całego serwisu w okresie – do policzenia udziału %.
        totals[t] = {
            "clicks": float(cur_df["clicks"].sum()) if not cur_df.empty else 0.0,
            "impressions": float(cur_df["impressions"].sum()) if not cur_df.empty else 0.0,
        }
    if not results:
        return None
    return {
        "site_key": site_key,
        "site_name": site.name,
        "current": current,
        "previous": previous,
        "sources": list(sources_types),
        "results": results,
        "totals": totals,
    }


def _render_mode5_results(bundle):
    cur, prev = bundle["current"], bundle["previous"]
    period_txt = f"CURRENT {cur.start}…{cur.end}"
    if prev:
        period_txt += f"  vs PREVIOUS {prev.start}…{prev.end}"
    st.info(f"**{bundle['site_name']}** — {period_txt}")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for t, df in bundle["results"].items():
            zf.writestr(f"{t}_urls.csv", df.to_csv(index=False))
    st.download_button(
        "⬇️ Pobierz WSZYSTKIE (ZIP)", buffer.getvalue(),
        file_name=f"{bundle['site_key']}_urls.zip", mime="application/zip", type="primary",
    )

    tabs = st.tabs([app.SOURCE_LABELS[t] for t in bundle["sources"]])
    for tab, t in zip(tabs, bundle["sources"]):
        with tab:
            df = bundle["results"][t]
            found = int((df["current_clicks"] > 0).sum())
            st.caption(f"{len(df)} URL-i · z ruchem w bieżącym okresie: {found}")
            st.dataframe(df, use_container_width=True)
            _dl(df, f"{t}_urls.csv")
            _render_mode5_summary(df, bundle["totals"].get(t, {}))


def _render_mode5_summary(df, totals):
    """Podsumowanie pod tabelą: sumy clicks/impressions, średni CTR i udział %."""
    import math

    sel_clicks = float(df["current_clicks"].sum())
    sel_impr = float(df["current_impressions"].sum())
    mean_ctr = df["current_ctr"].mean() if "current_ctr" in df.columns else math.nan
    total_clicks = totals.get("clicks", 0.0)
    total_impr = totals.get("impressions", 0.0)
    pct_clicks = (sel_clicks / total_clicks * 100.0) if total_clicks else math.nan
    pct_impr = (sel_impr / total_impr * 100.0) if total_impr else math.nan

    def _num(v):
        return f"{int(v):,}".replace(",", " ")

    def _pct(v):
        return "—" if math.isnan(v) else f"{v:.1f}% całości"

    st.markdown("**Podsumowanie (wybrane URL-e)**")
    c1, c2, c3 = st.columns(3)
    c1.metric("Suma clicks", _num(sel_clicks), _pct(pct_clicks))
    c2.metric("Suma impressions", _num(sel_impr), _pct(pct_impr))
    c3.metric(
        "Średni CTR",
        "—" if (isinstance(mean_ctr, float) and math.isnan(mean_ctr)) else f"{mean_ctr * 100:.2f}%",
    )
    st.caption(
        f"Cały serwis w okresie: {_num(total_clicks)} clicks · {_num(total_impr)} impressions."
    )


def render_mode5():
    st.subheader("Lista URL-i → dane GSC")
    pasted = st.text_area("Wklej URL-e (jeden na linię)", height=160, key="m5_text")
    uploaded = st.file_uploader("albo wgraj plik .txt/.csv", type=["txt", "csv"], key="m5_file")
    urls = _parse_urls(pasted, uploaded)
    st.caption(f"Wczytano {len(urls)} unikalnych URL-i.")
    if run:
        if not url_sources:
            st.warning("Wybierz co najmniej jedno źródło.")
        elif not urls:
            st.warning("Wklej albo wgraj listę URL-i.")
        elif _check_periods():
            bundle = _compute_mode5(single_site, urls, url_sources, compare_urls)
            if bundle:
                st.session_state["m5_bundle"] = bundle
    saved = st.session_state.get("m5_bundle")
    if saved:
        _render_mode5_results(saved)


if mode.startswith("4"):
    if run:
        bundle = _compute_mode4(single_site, preset_id, sources_sel)
        if bundle is not None:
            st.session_state["m4_bundle"] = bundle
    saved = st.session_state.get("m4_bundle")
    if saved:
        _render_mode4(saved)
    else:
        st.info("Ustaw parametry po lewej i kliknij **Uruchom analizę**.")
elif mode.startswith("5"):
    render_mode5()
elif run:
    if not selected:
        st.info("Wybierz serwisy w panelu po lewej.")
    elif _check_periods():
        if mode.startswith("1"):
            render_mode1(selected)
        elif mode.startswith("2"):
            render_mode2(selected)
        else:
            render_mode3(selected)
else:
    st.info("Ustaw parametry po lewej i kliknij **Uruchom analizę**.")
