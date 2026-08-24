"""Lokalny dashboard Streamlit dla GSC Analyzer (tryby 1/2/3).

Uruchomienie:
    streamlit run dashboard.py

Interfejs jest cienką warstwą nad istniejącą logiką (analysis.py, analytics/*),
więc cała matematyka to te same, przetestowane funkcje – dashboard je tylko wywołuje.
"""
from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

import app  # reużycie funkcji budujących DataFrame (bez duplikacji logiki)
from analysis import Period, analyze_site, analyze_site_multi_source, analyze_site_sources
from config import ConfigError, load_global_config, load_sites
from services.gsc_service import GSCError
from services.sitemap_service import SitemapError

st.set_page_config(page_title="GSC Analyzer", layout="wide")
st.title("GSC Analyzer – dashboard")


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

today = date.today()

# Kalendarze pokazujemy tylko tam, gdzie są używane: tryby 1-3 oraz tryb 4 z
# opcją "Własne daty". Dla presetów daty są wyliczane, więc kalendarze byłyby mylące.
_need_pickers = (not mode.startswith("4")) or (preset_id == 7)
cur_start = cur_end = prev_start = prev_end = None
if _need_pickers:
    st.sidebar.subheader("CURRENT PERIOD")
    cur_start = st.sidebar.date_input("Current start", today - timedelta(days=31))
    cur_end = st.sidebar.date_input("Current end", today - timedelta(days=2))
    st.sidebar.subheader("PREVIOUS PERIOD")
    prev_start = st.sidebar.date_input("Previous start", today - timedelta(days=62))
    prev_end = st.sidebar.date_input("Previous end", today - timedelta(days=33))
    _cd = (cur_end - cur_start).days + 1
    _pd = (prev_end - prev_start).days + 1
    st.sidebar.caption(f"Wybrano — Current: {_cd} dni · Previous: {_pd} dni")
    if cur_start > cur_end or prev_start > prev_end:
        st.sidebar.error("Data startowa jest późniejsza niż końcowa.")
    elif _cd != _pd:
        st.sidebar.warning(f"Różna długość okresów: {_cd} vs {_pd} dni.")
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
    cur_days = (cur_end - cur_start).days + 1
    prev_days = (prev_end - prev_start).days + 1
    st.caption(f"Current: {cur_days} dni · Previous: {prev_days} dni")
    if cur_start > cur_end or prev_start > prev_end:
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


def render_mode4(site_key, preset, sources_types):
    if not sources_types:
        st.info("Wybierz co najmniej jedno źródło (Search/Discover/News).")
        return
    site = sites[site_key]
    if preset == 7:
        if not _check_periods():
            return
        current, previous = _period(cur_start, cur_end), _period(prev_start, prev_end)
    else:
        current, previous = app._compute_preset_periods(preset)
    cd = (date.fromisoformat(current.end) - date.fromisoformat(current.start)).days + 1
    pd_ = (date.fromisoformat(previous.end) - date.fromisoformat(previous.start)).days + 1
    if cd != pd_:
        st.warning(f"Okresy mają różną długość ({cd} vs {pd_} dni) – porównanie może być zaburzone.")
    st.info(
        f"**{site.name}** — CURRENT {current.start}…{current.end} ({cd} dni)  "
        f"vs  PREVIOUS {previous.start}…{previous.end} ({pd_} dni)"
    )
    try:
        with st.spinner("Pobieram dane GSC…"):
            results = analyze_site_multi_source(
                site, current, previous, global_config.google_credentials_path,
                sources_types, use_cache,
            )
    except (GSCError, SitemapError) as exc:
        st.error(f"{site.name}: {exc}")
        return
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


if run:
    if mode.startswith("4"):
        render_mode4(single_site, preset_id, sources_sel)
    elif not selected:
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
