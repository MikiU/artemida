from pathlib import Path
from unittest.mock import patch

import json
import re

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from config import CategoryGroup, GlobalConfig, SiteConfig, SitesConfig


VIEWS = ["Przegląd ruchu", "Kategorie i treści", "Branded search", "Lista URL-i"]


def dashboard_test():
    sites = {
        key: SiteConfig(key, key.upper(), f"sc-domain:{key}.pl", f"https://{key}.pl/", [], brand_terms=[key])
        for key in ("a", "b", "c")
    }
    return AppTest.from_file(str(Path(__file__).parents[1] / "dashboard.py"), default_timeout=60), sites


def test_four_views_share_one_site_selector(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    app_test, sites = dashboard_test()
    with patch("config.load_global_config", return_value=GlobalConfig("unused")), patch(
        "config.load_sites", return_value=SitesConfig(sites, {})
    ):
        app_test.run()
        assert not app_test.exception
        assert app_test.sidebar.radio[0].options == VIEWS
        for view in VIEWS:
            app_test.sidebar.radio[0].set_value(view).run()
            assert not app_test.exception
            assert len([widget for widget in app_test.multiselect if widget.label == "Serwisy"]) == 1
            assert not [widget for widget in app_test.selectbox if widget.label in ("Serwis", "Wybierz serwis")]
            assert len(app_test.date_input) == (2 if view in ("Branded search", "Lista URL-i") else 0)
            if view == "Branded search":
                assert not [widget for widget in app_test.multiselect if widget.label == "Źródła"]


@pytest.fixture
def dashboard_env(monkeypatch):
    import analysis
    import analytics.dashboard as data

    monkeypatch.delenv("APP_PASSWORD", raising=False)
    app_test, sites = dashboard_test()
    calls = []

    def fake_gsc(start, end, prop, credentials, dimensions=("page",), **kwargs):
        calls.append((prop, dimensions, {**kwargs, "start_date": start, "end_date": end}))
        key = prop.split(":")[-1].split(".")[0]
        dates = pd.date_range(start, end).strftime("%Y-%m-%d")
        clicks = 3 if kwargs.get("page_filter") else 10
        if dimensions == ("date",):
            return pd.DataFrame({"date": dates, "clicks": clicks, "impressions": clicks * 10, "ctr": 0.1, "position": 2.0})
        if dimensions == ("device",):
            return pd.DataFrame({"device": ["MOBILE"], "clicks": [clicks * len(dates)], "impressions": [clicks * len(dates) * 10]})
        urls = [f"https://{key}.pl/news/article", f"https://{key}.pl/other"]
        if kwargs.get("search_type") == "discover" and not kwargs.get("page_filter"):
            urls.append(f"https://{key}.pl/news/discover")
        if kwargs.get("page_filter"):
            urls = [url for url in urls if re.search(kwargs["page_filter"], url)]
        if dimensions == ("date", "page"):
            return pd.DataFrame([
                {"date": stamp, "page": url, "clicks": clicks, "impressions": clicks * 10, "ctr": 0.1, "position": 2.0}
                for stamp in dates for url in urls
            ], columns=["date", "page", "clicks", "impressions", "ctr", "position"])
        return pd.DataFrame({"page": urls, "clicks": clicks * len(dates), "impressions": clicks * len(dates) * 10, "ctr": 0.1, "position": 2.0})

    monkeypatch.setattr("config.load_global_config", lambda: GlobalConfig("unused"))
    groups = {"news": CategoryGroup("news", "Wiadomości", {key: ["News"] for key in sites})}
    monkeypatch.setattr("config.load_sites", lambda: SitesConfig(sites, groups))
    monkeypatch.setattr(analysis, "get_gsc_data", fake_gsc)
    monkeypatch.setattr(data, "get_gsc_data", fake_gsc)
    monkeypatch.setattr(data, "_collect_category_urls", lambda site: [site.base_url + "news/"])
    return app_test, sites, calls


def run_analysis(app_test):
    next(widget for widget in app_test.button if widget.label == "Uruchom analizę").click().run()


@pytest.mark.parametrize("view", VIEWS)
@pytest.mark.parametrize("site_count", [1, 2, 3])
def test_all_views_render_selected_sites(dashboard_env, view, site_count):
    app_test, sites, calls = dashboard_env
    selected = list(sites)[:site_count]
    app_test.run()
    app_test.sidebar.radio[0].set_value(view).run()
    app_test.multiselect(key="selected_sites").set_value(selected).run()
    if view == "Lista URL-i":
        app_test.text_area[0].set_value("\n".join(f"https://{key}.pl/news/article" for key in selected)).run()
    run_analysis(app_test)
    assert not app_test.exception
    assert calls
    assert {prop for prop, _, _ in calls} == {sites[key].gsc_property for key in selected}
    charts = app_test.get("vega_lite_chart")
    assert len(charts) == (1 if view == "Branded search" else 2)
    specification = json.loads(charts[0].proto.spec)
    assert specification["encoding"]["color"]["scale"]["domain"] == [f"{sites[key].name} ({key})" for key in selected]
    assert not [widget for widget in app_test.selectbox if widget.label in ("Serwis", "Wybierz serwis", "Wykres dzienny serwisu")]
    saved = app_test.session_state["result:" + view]
    assert set(saved["daily"]["site_key"]) == set(selected)
    if view == "Lista URL-i":
        assert saved["pages"]["current_clicks"].sum() == saved["daily"]["clicks"].sum()
        assert all(kwargs.get("page_filter") for _, dims, kwargs in calls if dims == ("page",))


def test_presentation_does_not_refetch_and_filters_hide_old_results(dashboard_env):
    app_test, _, calls = dashboard_env
    app_test.run()
    app_test.multiselect(key="selected_sites").set_value(["a", "b", "c"]).run()
    run_analysis(app_test)
    assert not app_test.exception
    count = len(calls)
    app_test.checkbox(key="Przegląd ruchu:show_sum").check().run()
    app_test.checkbox(key="Przegląd ruchu:show_previous").check().run()
    app_test.radio(key="Przegląd ruchu:metric").set_value("Wyświetlenia").run()
    assert not app_test.exception
    assert len(calls) == count
    specification = json.loads(app_test.get("vega_lite_chart")[0].proto.spec)
    assert specification["encoding"]["color"]["scale"]["domain"][-1] == "Suma serwisów"
    app_test.multiselect(key="selected_sites").set_value(["a"]).run()
    assert not app_test.exception
    assert len(calls) == count
    assert not app_test.get("vega_lite_chart")
    assert any("Parametry zostały zmienione" in message.value for message in app_test.info)


def test_dates_history_and_empty_selection(dashboard_env):
    app_test, _, calls = dashboard_env
    app_test.run()
    assert next(widget for widget in app_test.button if widget.label == "Uruchom analizę").disabled
    app_test.selectbox[0].set_value("Własne daty").run()
    assert len(app_test.date_input) == 4
    app_test.sidebar.radio[0].set_value("Branded search").run()
    app_test.selectbox(key="settings:Branded search:preset").set_value("Maksymalny zakres (16 miesięcy)").run()
    assert not app_test.exception
    assert not app_test.date_input
    assert len(app_test.selectbox) == 1
    assert not calls


def test_collector_preserves_partial_errors_and_duplicate_names(monkeypatch):
    import analytics.dashboard as data
    from analysis import Period
    from services.gsc_service import GSCError

    _, sites = dashboard_test()
    sites["b"].name = sites["a"].name
    calls = []

    def fake_daily(site, current, previous, credentials, sources, use_cache):
        calls.append((site.key, sources[0]))
        if sources == ["news"]:
            raise GSCError("No access")
        return {sources[0]: pd.DataFrame({
            "current_date": [current.start], "previous_date": [previous.start],
            "current_clicks": [10], "previous_clicks": [5],
            "current_impressions": [100], "previous_impressions": [50],
        })}

    monkeypatch.setattr(data, "analyze_site_daily_sources", fake_daily)
    request = data.AnalysisRequest(VIEWS[0], ("a", "b"), ("web", "news"), Period("2026-07-01", "2026-07-01"), Period("2026-06-01", "2026-06-01"))
    bundle = data.collect_dashboard(request, sites, "unused")
    assert calls == [("a", "web"), ("a", "news"), ("b", "web"), ("b", "news")]
    assert set(bundle["daily"]["site_key"]) == {"a", "b"}
    assert set(bundle["daily"]["source"]) == {"web"}
    assert list(bundle["statuses"]["status"]) == ["ok", "error", "ok", "error"]
    overview = data.daily_overview(bundle["daily"])
    assert len(overview) == 2
    assert overview["current_clicks"].sum() == 20


@pytest.mark.parametrize("view", [VIEWS[0], VIEWS[1], VIEWS[3]])
def test_multiple_sources_render_total_and_individual_tabs(dashboard_env, view):
    app_test, _, _ = dashboard_env
    app_test.run()
    app_test.sidebar.radio[0].set_value(view).run()
    app_test.multiselect(key="selected_sites").set_value(["a", "b"]).run()
    app_test.multiselect(key="selected_sources").set_value(["web", "discover", "news"]).run()
    if view == VIEWS[3]:
        app_test.text_area[0].set_value("https://a.pl/news/article\nhttps://b.pl/news/article").run()
    run_analysis(app_test)
    assert not app_test.exception
    assert [tab.label for tab in app_test.tabs] == ["Łącznie", "Search", "Discover", "News"]
    assert len(app_test.get("vega_lite_chart")) == 4
    if view == VIEWS[1]:
        assert any(header.value == "Porównanie grup kategorii" for header in app_test.subheader)
        tree = next(frame.value for frame in app_test.tabs[0].dataframe if "number_of_urls" in frame.value and len(frame.value) > 0)
        assert set(tree[tree["category_path"] == "News"]["number_of_urls"]) == {2}
    if view == VIEWS[3]:
        total_table = next(frame.value for frame in app_test.tabs[0].dataframe if "url" in frame.value)
        assert not any("position" in column for column in total_table)


def test_view_switch_restores_dates_and_sources(dashboard_env):
    app_test, _, _ = dashboard_env
    app_test.run()
    app_test.selectbox[0].set_value("Własne daty").run()
    app_test.multiselect(key="selected_sources").set_value(["web", "news"]).run()
    original_dates = [widget.value for widget in app_test.date_input]
    app_test.sidebar.radio[0].set_value(VIEWS[2]).run()
    app_test.sidebar.radio[0].set_value(VIEWS[0]).run()
    assert not app_test.exception
    assert [widget.value for widget in app_test.date_input] == original_dates
    assert app_test.multiselect(key="selected_sources").value == ["web", "news"]


def test_unassigned_url_blocks_api_requests(dashboard_env):
    app_test, _, calls = dashboard_env
    app_test.run()
    app_test.sidebar.radio[0].set_value(VIEWS[3]).run()
    app_test.multiselect(key="selected_sites").set_value(["a"]).run()
    app_test.text_area[0].set_value("https://other.pl/article").run()
    assert not app_test.exception
    assert next(widget for widget in app_test.button if widget.label == "Uruchom analizę").disabled
    assert not calls


@pytest.mark.parametrize("view", VIEWS)
def test_all_views_offer_year_and_full_history_without_comparison(dashboard_env, view):
    app_test, _, calls = dashboard_env
    app_test.run()
    app_test.sidebar.radio[0].set_value(view).run()
    app_test.checkbox(key=f"settings:{view}:compare").uncheck().run()
    period = app_test.selectbox(key=f"settings:{view}:preset")
    assert "Ostatni rok (365 dni)" in period.options
    assert "Maksymalny zakres (16 miesięcy)" in period.options
    period.set_value("Maksymalny zakres (16 miesięcy)").run()
    assert not app_test.exception
    assert not app_test.date_input
    assert not any("Poprzedni:" in caption.value for caption in app_test.caption)
    assert not calls


@pytest.mark.parametrize("view", VIEWS)
def test_full_history_fetches_only_one_period_in_every_view(dashboard_env, view):
    from dashboard import _single_period
    from datetime import date

    app_test, _, calls = dashboard_env
    app_test.run()
    app_test.sidebar.radio[0].set_value(view).run()
    app_test.multiselect(key="selected_sites").set_value(["a", "b"]).run()
    if view != VIEWS[2]:
        app_test.multiselect(key="selected_sources").set_value(["web", "discover"]).run()
    app_test.checkbox(key=f"settings:{view}:compare").uncheck().run()
    app_test.selectbox(key=f"settings:{view}:preset").set_value("Maksymalny zakres (16 miesięcy)").run()
    if view == VIEWS[3]:
        app_test.text_area[0].set_value("https://a.pl/news/article\nhttps://b.pl/news/article").run()
    run_analysis(app_test)
    assert not app_test.exception
    assert calls
    saved = app_test.session_state["result:" + view]
    assert saved["request"].previous is None
    assert saved["request"].current == _single_period("Maksymalny zakres (16 miesięcy)", date.today())
    assert all(kwargs["start_date"] == saved["request"].current.start and kwargs["end_date"] == saved["request"].current.end for _, _, kwargs in calls)
    assert set(saved["daily"]["period"]) == {"current"}
    assert not [widget for widget in app_test.checkbox if widget.label == "Pokaż poprzedni okres"]
    for frame in app_test.dataframe:
        assert not any(column.startswith("previous_") or "_change" in column for column in frame.value)
    if view == VIEWS[1]:
        assert any(header.value == "Kategorie z największym ruchem" for header in app_test.subheader)
        assert not any(header.value == "Kategorie rosnące" for header in app_test.subheader)


@pytest.mark.parametrize("label,length", [("Ostatnie 7 dni", 7), ("Ostatnie 30 dni", 30), ("Ostatni rok (365 dni)", 365)])
def test_single_period_preset_lengths(label, length):
    from dashboard import _single_period
    from datetime import date

    result = _single_period(label, date(2026, 10, 29))
    assert result.end == "2026-10-26"
    assert (date.fromisoformat(result.end) - date.fromisoformat(result.start)).days + 1 == length


def test_full_history_clamps_month_end():
    from dashboard import _single_period
    from datetime import date

    result = _single_period("Maksymalny zakres (16 miesięcy)", date(2024, 6, 30))
    assert result.start == "2023-02-28"
    assert result.end == "2024-06-27"


@pytest.mark.parametrize("today", ["2026-10-30", "2024-06-30", "2024-02-29", "2025-03-31"])
@pytest.mark.parametrize("options", [{}, {"days": 1}, {"days": 90}, {"months": 7}])
def test_equal_periods_fit_history_and_have_identical_lengths(today, options):
    from datetime import date, timedelta
    from dashboard import _equal_periods, _single_period

    anchor = date.fromisoformat(today)
    current, previous = _equal_periods(anchor, **options)
    history = _single_period("Maksymalny zakres (16 miesięcy)", anchor)
    length = (date.fromisoformat(current.end) - date.fromisoformat(current.start)).days + 1
    assert length == (date.fromisoformat(previous.end) - date.fromisoformat(previous.start)).days + 1
    assert date.fromisoformat(previous.end) + timedelta(days=1) == date.fromisoformat(current.start)
    assert previous.start >= history.start
    assert current.end == history.end
    if not options:
        assert (date.fromisoformat(previous.start) - date.fromisoformat(history.start)).days <= 1


def test_equal_periods_reject_out_of_history():
    from datetime import date
    from dashboard import _equal_periods

    for options in ({"days": 0}, {"days": 365}, {"months": 16}):
        with pytest.raises(ValueError):
            _equal_periods(date(2026, 10, 30), **options)


def test_equal_period_controls_overlay_by_default(dashboard_env):
    app_test, _, calls = dashboard_env
    app_test.run()
    app_test.multiselect(key="selected_sites").set_value(["a"]).run()
    app_test.selectbox[0].set_value("Równe okresy (do 16 miesięcy)").run()
    app_test.radio(key="settings:Przegląd ruchu:unit").set_value("Dni").run()
    app_test.slider(key="settings:Przegląd ruchu:days").set_value(90).run()
    run_analysis(app_test)
    assert not app_test.exception
    assert app_test.checkbox(key="Przegląd ruchu:show_previous").value
    saved = app_test.session_state["result:Przegląd ruchu"]
    assert saved["daily"].groupby("period").size().to_dict() == {"current": 90, "previous": 90}
    assert calls


@pytest.mark.parametrize("view", VIEWS)
def test_equal_period_month_slider_in_every_view(dashboard_env, view):
    from datetime import date
    from dashboard import _equal_periods

    app_test, _, calls = dashboard_env
    app_test.run()
    app_test.sidebar.radio[0].set_value(view).run()
    app_test.checkbox(key=f"settings:{view}:compare").check().run()
    app_test.selectbox(key=f"settings:{view}:preset").set_value("Równe okresy (do 16 miesięcy)").run()
    app_test.radio(key=f"settings:{view}:unit").set_value("Miesiące").run()
    app_test.slider(key=f"settings:{view}:months").set_value(3).run()
    assert not app_test.exception
    current, previous = _equal_periods(date.today(), months=3)
    captions = [widget.value for widget in app_test.caption]
    assert any(current.start in text and current.end in text for text in captions)
    assert any(previous.start in text and previous.end in text for text in captions)
    assert not app_test.date_input
    assert not calls


@pytest.mark.parametrize("view", [VIEWS[0], VIEWS[1], VIEWS[3]])
def test_daily_url_export_contains_dates_traffic_and_periods(dashboard_env, view):
    import analytics.dashboard as data
    from analysis import Period

    _, sites, calls = dashboard_env
    request = data.AnalysisRequest(
        view, ("a",), ("web", "discover"), Period("2026-10-25", "2026-10-26"),
        Period("2026-10-23", "2026-10-24"), (("https://a.pl/news/article", "a"),),
        export_url_daily=True,
    )
    bundle = data.collect_dashboard(request, sites, "unused")
    exported = bundle["url_daily"]
    assert not any("eksport dzienny" in notice for notice in bundle["notices"])
    assert set(exported["period"]) == {"current", "previous"}
    assert set(exported["date"]) == {"2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"}
    assert set(exported["source"]) == {"web", "discover"}
    assert exported["clicks"].gt(0).all()
    assert exported["impressions"].gt(0).all()
    assert exported.loc[exported["source"] == "discover", "position"].isna().all()
    export_calls = [kwargs for _, dims, kwargs in calls if dims == ("date", "page")]
    assert len(export_calls) == 8
    assert all(kwargs["start_date"] == kwargs["end_date"] for kwargs in export_calls)
    if view == VIEWS[3]:
        assert set(exported["page"]) == {"https://a.pl/news/article"}
        assert all(kwargs.get("page_filter") for kwargs in export_calls)


def test_daily_url_export_failure_preserves_report(dashboard_env, monkeypatch):
    import analytics.dashboard as data
    from analysis import Period
    from services.gsc_service import GSCError

    _, sites, _ = dashboard_env
    def fail_export(*args, **kwargs):
        raise GSCError("Limit API")

    monkeypatch.setattr(data, "_url_daily_export", fail_export)
    request = data.AnalysisRequest(VIEWS[0], ("a",), ("web",), Period("2026-10-25", "2026-10-26"), export_url_daily=True)
    bundle = data.collect_dashboard(request, sites, "unused")
    assert not bundle["daily"].empty
    assert bundle["url_daily"].empty
    assert bundle["statuses"]["status"].tolist() == ["ok"]
    assert "Limit API" in bundle["notices"][0]


def test_daily_url_export_downloads_without_refetch(dashboard_env):
    app_test, _, calls = dashboard_env
    app_test.run()
    app_test.multiselect(key="selected_sites").set_value(["a"]).run()
    app_test.checkbox(key="settings:Przegląd ruchu:export_url_daily").check().run()
    run_analysis(app_test)
    assert not app_test.exception
    saved = app_test.session_state["result:Przegląd ruchu"]
    assert not saved["url_daily"].empty
    assert any(widget.proto.label == "Pobierz URL-e z datami i ruchem (CSV)" for widget in app_test.get("download_button"))
    count = len(calls)
    app_test.checkbox(key="Przegląd ruchu:show_sum").check().run()
    assert not app_test.exception
    assert len(calls) == count