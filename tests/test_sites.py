"""Testy ładowania sites.yaml oraz walidacji wyboru serwisów."""
import pytest

from config import ConfigError, load_sites


def _write_sites(tmp_path, text):
    path = tmp_path / "sites.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


VALID_YAML = """
sites:
  fakt:
    name: "Fakt"
    gsc_property: "https://www.fakt.pl/"
    base_url: "https://www.fakt.pl/"
    category_sitemaps:
      - "https://www.fakt.pl/sitemap-categories.xml"
  onet_wiadomosci:
    name: "Onet Wiadomości"
    gsc_property: "sc-domain:onet.pl"
    base_url: "https://wiadomosci.onet.pl/"
    category_sitemaps:
      - "https://wiadomosci.onet.pl/sitemap.xml"

category_groups:
  news:
    label: "Wiadomości"
    fakt:
      paths:
        - "Wydarzenia"
        - "Polityka"
    onet_wiadomosci:
      paths:
        - "Wiadomosci"
"""


def test_loads_two_sites(tmp_path):
    config = load_sites(_write_sites(tmp_path, VALID_YAML))
    assert set(config.sites.keys()) == {"fakt", "onet_wiadomosci"}
    assert config.sites["fakt"].name == "Fakt"
    assert config.sites["onet_wiadomosci"].gsc_property == "sc-domain:onet.pl"
    # property przekazywane dokładnie jak w konfiguracji
    assert config.sites["fakt"].gsc_property == "https://www.fakt.pl/"


def test_loads_category_groups(tmp_path):
    config = load_sites(_write_sites(tmp_path, VALID_YAML))
    group = config.category_groups["news"]
    assert group.label == "Wiadomości"
    assert group.site_paths["fakt"] == ["Wydarzenia", "Polityka"]
    assert group.site_paths["onet_wiadomosci"] == ["Wiadomosci"]


def test_missing_sites_section_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_sites(_write_sites(tmp_path, "category_groups: {}\n"))


def test_missing_field_raises(tmp_path):
    bad = """
sites:
  x:
    name: "X"
    base_url: "https://x/"
"""
    with pytest.raises(ConfigError):
        load_sites(_write_sites(tmp_path, bad))


def test_two_different_sites_selectable(tmp_path):
    config = load_sites(_write_sites(tmp_path, VALID_YAML))
    keys = list(config.sites.keys())
    first, second = 0, 1
    assert keys[first] != keys[second]
