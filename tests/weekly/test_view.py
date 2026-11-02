from pathlib import Path
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from config import GlobalConfig, SitesConfig
from weekly import monitor
from tests.weekly.test_detect_anomalies import document
from tests.weekly.test_monitor import configured


def setup_app(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setattr(monitor, "STORE", tmp_path)
    monkeypatch.setattr("config.load_sites", lambda: SitesConfig({"fakt": configured()}, {}))
    credentials = Mock(return_value=GlobalConfig("unused"))
    monkeypatch.setattr("config.load_global_config", credentials)
    monkeypatch.setattr(monitor, "_build_service", Mock())
    fetch = Mock(return_value=document())
    monkeypatch.setattr(monitor, "fetch_site", fetch)
    app = AppTest.from_file(str(Path(__file__).parents[2] / "dashboard.py"), default_timeout=60).run()
    app.sidebar.radio(key="view").set_value("Monitoring tygodniowy SEO").run()
    app.multiselect(key="selected_sites").set_value(["fakt"]).run()
    app.text_input(key="weekly:week").set_value("2026-W43").run()
    return app, fetch, credentials


def test_new_measurement_reruns_and_exports_do_not_refetch(monkeypatch, tmp_path):
    app, fetch, _ = setup_app(monkeypatch, tmp_path)
    assert not app.exception
    assert not fetch.called
    app.button(key="weekly:run").click().run()
    assert not app.exception
    assert fetch.call_count == 1
    app.run()
    assert not app.exception
    assert fetch.call_count == 1
    assert len(app.get("download_button")) == 3
    app.button(key="weekly:save_report").click().run()
    assert not app.exception
    assert list((tmp_path / "reports" / "2026-W43").glob("*.json"))
    assert fetch.call_count == 1


def test_offline_history_does_not_require_credentials(monkeypatch, tmp_path):
    app, fetch, credentials = setup_app(monkeypatch, tmp_path)
    app.button(key="weekly:run").click().run()
    count = fetch.call_count
    credentials.side_effect = RuntimeError("credentials unavailable")
    credentials.reset_mock()
    app.radio(key="weekly:mode").set_value("Odczyt zapisanych wyników").run()
    app.button(key="weekly:load").click().run()
    assert not app.exception
    app.run()
    assert not app.exception
    assert not credentials.called
    assert fetch.call_count == count


def test_unavailable_checks_are_distinct_from_real_problems():
    from weekly.view import check_state
    assert check_state({"status": "OK"}) == "OK"
    assert check_state({"status": "FAIL", "reason": "http_500"}) == "Problem"
    assert check_state({"status": "WARN", "urls": [{"status": "WARN", "reason": "pagespeed_api_key_missing"}]}) == "Brak danych"
    assert check_state({"status": "WARN", "reason": "inspection_disabled"}) == "Nie wykonano"


def test_incomplete_measurement_is_explicitly_a_draft_in_ui(monkeypatch, tmp_path):
    app, fetch, _ = setup_app(monkeypatch, tmp_path)
    payload = document()
    payload["periods"]["T"]["missing_dates"] = ["2026-10-25"]
    fetch.return_value = payload
    app.button(key="weekly:run").click().run()
    assert not app.exception
    assert any("SZKIC" in item.value for item in app.warning)
    assert not any("gotowy po weryfikacji" in item.value for item in app.caption)
    app.run()
    assert fetch.call_count == 1