"""Konfiguracja aplikacji.

Sekrety (klucz Service Account) wczytywane są z .env.
Konfiguracja serwisów i grup kategorii z sites.yaml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Błąd konfiguracji aplikacji (brak .env, brak credentials, błędny sites.yaml)."""


@dataclass
class GlobalConfig:
    google_credentials_path: str


@dataclass
class SiteConfig:
    key: str
    name: str
    gsc_property: str
    base_url: str
    category_sitemaps: list[str]
    # Top-level category_path uznawane za YMYL (your money your life).
    ymyl_paths: list[str] = field(default_factory=list)
    # Ścieżki wykluczone z YMYL mimo dopasowania do ymyl_paths (np. Pogoda).
    ymyl_exclude_paths: list[str] = field(default_factory=list)


@dataclass
class CategoryGroup:
    key: str
    label: str
    # site_key -> lista category_path do zsumowania dla tego serwisu.
    site_paths: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class SitesConfig:
    sites: dict[str, SiteConfig]
    category_groups: dict[str, CategoryGroup]


def load_global_config() -> GlobalConfig:
    """Wczytuje globalną konfigurację: credentials z pliku lub z JSON-a w środowisku."""
    load_dotenv()

    # Hosting: pełny JSON klucza w zmiennej (np. Streamlit secrets) zamiast pliku.
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if credentials_json:
        return GlobalConfig(google_credentials_path=credentials_json)

    credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "").strip()
    if not credentials_path:
        raise ConfigError(
            "Brak GOOGLE_CREDENTIALS_JSON lub GOOGLE_CREDENTIALS_PATH.\n"
            "Lokalnie: skopiuj .env.example do .env i wskaż plik klucza Service Account.\n"
            "Hosting: ustaw sekret GOOGLE_CREDENTIALS_JSON z pełną treścią klucza JSON."
        )
    if not os.path.exists(credentials_path):
        raise ConfigError(
            f"Nie znaleziono pliku credentials: '{credentials_path}'.\n"
            "Pobierz klucz Service Account (JSON) i wskaż go w GOOGLE_CREDENTIALS_PATH."
        )
    return GlobalConfig(google_credentials_path=credentials_path)


def load_sites(path: str = "sites.yaml") -> SitesConfig:
    """Wczytuje konfigurację serwisów z pliku YAML lub ze zmiennej SITES_YAML."""
    raw_yaml = os.getenv("SITES_YAML", "").strip()
    if raw_yaml:
        source = "SITES_YAML"
        try:
            raw = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Błędny YAML w SITES_YAML: {exc}") from exc
    else:
        source = path
        if not os.path.exists(path):
            raise ConfigError(
                f"Nie znaleziono pliku konfiguracji serwisów: '{path}'.\n"
                "Lokalnie: skopiuj sites.example.yaml do sites.yaml.\n"
                "Hosting: ustaw sekret SITES_YAML z treścią konfiguracji."
            )
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Błędny plik YAML '{path}': {exc}") from exc

    raw_sites = raw.get("sites")
    if not isinstance(raw_sites, dict) or not raw_sites:
        raise ConfigError(
            f"'{source}' nie zawiera sekcji 'sites' z co najmniej jednym serwisem."
        )

    # Globalna lista YMYL stosowana dla serwisów bez własnej.
    global_ymyl = raw.get("ymyl_paths") or []
    if isinstance(global_ymyl, str):
        global_ymyl = [global_ymyl]
    global_ymyl = [str(p) for p in global_ymyl]

    # Globalna lista wykluczeń YMYL.
    global_ymyl_exclude = raw.get("ymyl_exclude_paths") or []
    if isinstance(global_ymyl_exclude, str):
        global_ymyl_exclude = [global_ymyl_exclude]
    global_ymyl_exclude = [str(p) for p in global_ymyl_exclude]

    sites: dict[str, SiteConfig] = {}
    for key, cfg in raw_sites.items():
        if not isinstance(cfg, dict):
            raise ConfigError(f"Serwis '{key}' ma nieprawidłową konfigurację.")
        sitemaps = cfg.get("category_sitemaps") or []
        if isinstance(sitemaps, str):
            sitemaps = [sitemaps]
        missing = [f for f in ("name", "gsc_property", "base_url") if not cfg.get(f)]
        if missing:
            raise ConfigError(f"Serwis '{key}' – brak pól: {', '.join(missing)}.")
        ymyl = cfg.get("ymyl_paths")
        if ymyl is None:
            ymyl = global_ymyl
        elif isinstance(ymyl, str):
            ymyl = [ymyl]
        ymyl_exclude = cfg.get("ymyl_exclude_paths")
        if ymyl_exclude is None:
            ymyl_exclude = global_ymyl_exclude
        elif isinstance(ymyl_exclude, str):
            ymyl_exclude = [ymyl_exclude]
        sites[key] = SiteConfig(
            key=key,
            name=str(cfg["name"]),
            gsc_property=str(cfg["gsc_property"]),
            base_url=str(cfg["base_url"]),
            category_sitemaps=[str(s) for s in sitemaps],
            ymyl_paths=[str(p) for p in ymyl],
            ymyl_exclude_paths=[str(p) for p in ymyl_exclude],
        )

    category_groups = _parse_category_groups(raw.get("category_groups"), sites)
    return SitesConfig(sites=sites, category_groups=category_groups)


def _parse_category_groups(
    raw_groups, sites: dict[str, SiteConfig]
) -> dict[str, CategoryGroup]:
    """Parsuje opcjonalną sekcję category_groups."""
    if not raw_groups:
        return {}
    if not isinstance(raw_groups, dict):
        raise ConfigError("Sekcja 'category_groups' musi być mapą grup.")

    groups: dict[str, CategoryGroup] = {}
    for group_key, cfg in raw_groups.items():
        if not isinstance(cfg, dict):
            raise ConfigError(f"Grupa '{group_key}' ma nieprawidłową konfigurację.")
        label = str(cfg.get("label", group_key))
        site_paths: dict[str, list[str]] = {}
        for site_key, site_cfg in cfg.items():
            if site_key == "label":
                continue
            if not isinstance(site_cfg, dict):
                continue
            paths = site_cfg.get("paths") or []
            if isinstance(paths, str):
                paths = [paths]
            site_paths[site_key] = [str(p) for p in paths]
        groups[group_key] = CategoryGroup(
            key=group_key, label=label, site_paths=site_paths
        )
    return groups

