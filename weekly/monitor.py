"""Application adapter, explicit collection and immutable local measurements."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from config import SiteConfig, load_sites
from services.gsc_service import _build_service
from weekly.common import METRICS, ROOT, Settings, Site, read_json, week_id, week_start, write_json
from weekly.detect_anomalies import analyze
from weekly.fetch_gsc import GscClient, fetch_site
from weekly.report_context import aggregate_status
from weekly.tech_checks import InspectionBudget, PoliteHttp, USER_AGENT, collect_site

STORE = ROOT / "output" / "weekly"


def adapt_site(site: SiteConfig) -> Site:
    options = site.weekly_monitoring
    if not isinstance(options, dict):
        raise ValueError("weekly_monitoring musi byc mapa ustawien")
    allowed = {"thresholds", "brand_terms", "sitemap_url", "representative_urls", "inspection_limit"}
    if set(options) - allowed:
        raise ValueError("Nieznane ustawienie weekly_monitoring")
    thresholds = {metric: {"warn": 0.15, "fail": 0.25} for metric in METRICS}
    thresholds.update(options.get("thresholds", {}))
    return Site(
        id=site.key, name=site.name, placeholder=False,
        gsc_property=site.gsc_property, base_url=site.base_url,
        category_sitemaps=site.category_sitemaps,
        brand_terms=options.get("brand_terms", site.brand_terms), thresholds=thresholds,
        sitemap_url=options.get("sitemap_url"),
        representative_urls={"home": site.base_url, "article": None, "category": None,
                             **options.get("representative_urls", {})},
        inspection_limit=options.get("inspection_limit", 3),
    )


def settings_from_app() -> Settings:
    return Settings(sites=[adapt_site(site) for site in load_sites().sites.values()])


def source_status(gsc, tech):
    anomalies = (gsc or {}).get("anomalies", {})
    gsc_status = anomalies.get("status")
    quality = anomalies.get("data_quality", "INCOMPLETE")
    reasons = []
    if quality == "INCOMPLETE" or gsc_status is None:
        reasons.append("gsc_not_fully_evaluated")
    tech_status = tech.get("status")
    if tech_status is None:
        reasons.append("tech_" + tech["state"])
    return {
        "status": aggregate_status(gsc_status, quality, tech_status),
        "gsc_status": gsc_status, "tech_status": tech_status,
        "gsc_data_quality": quality, "status_reasons": reasons,
        "gsc_state": "no_data" if not gsc or not gsc["periods"]["T"]["totals"] else "incomplete" if quality == "INCOMPLETE" else "measured",
        "tech_state": tech["state"],
    }


def history(root: Path = STORE, week: str | None = None):
    if week is not None:
        week_start(week)
    paths = (root / "data" / week).glob("*/*.json") if week else (root / "data").glob("????-W??/*/*.json")
    return sorted(paths, reverse=True)


def load_measurement(path: Path, site: SiteConfig, week: str, root: Path = STORE):
    validated = adapt_site(site)
    week_start(week)
    expected = (root / "data" / week / validated.id).resolve()
    if path.resolve().parent != expected:
        raise ValueError("Pomiar spoza wybranego serwisu lub tygodnia")
    document = read_json(path)
    if (document.get("schema_version") != 1 or document.get("site") != validated.id
            or document.get("property") != validated.gsc_property or document.get("week") != week):
        raise ValueError("Niezgodna tozsamosc zapisanego pomiaru")
    gsc, tech = document.get("gsc"), document["tech"]
    for source in (gsc, tech):
        if source is not None and (source.get("site") != validated.id or source.get("property") != validated.gsc_property or source.get("week") != week):
            raise ValueError("Niezgodna tozsamosc zrodla pomiaru")
    if gsc:
        snapshot_site = Site.model_validate(document["configuration"])
        if snapshot_site.id != validated.id or snapshot_site.gsc_property != validated.gsc_property:
            raise ValueError("Niezgodna konfiguracja pomiaru")
        if analyze(gsc, snapshot_site) != gsc["anomalies"]:
            raise ValueError("Wyniki nie zgadzaja sie z danymi zrodlowymi")
    if source_status(gsc, tech) != document["summary"]:
        raise ValueError("Niezgodny status zapisanego pomiaru")
    return document


def previous_technical(site: SiteConfig, root: Path):
    observations = []
    for path in history(root):
        if path.parent.name != site.key:
            continue
        document = load_measurement(path, site, path.parent.parent.name, root)
        if document["tech"]["state"] == "measured":
            observations.append(document["tech"])
    return max(observations, key=lambda item: item["checked_at"]) if observations else None


def collect(sites, selected, week, credentials_ref, technical=False, root: Path = STORE, progress=None):
    start = week_start(week)
    results = []
    client = None
    for index, key in enumerate(selected):
        site_config = sites[key]
        if progress:
            progress(index / len(selected), f"{site_config.name}: GSC")
        errors = []
        identity = {"schema_version": 1, "site": key, "name": site_config.name,
                    "property": site_config.gsc_property, "week": week_id(start)}
        document = {**identity, "measured_at": datetime.now(timezone.utc).isoformat(), "gsc": None}
        try:
            site = adapt_site(site_config)
        except Exception:
            results.append({**document, "configuration_error": True,
                            "errors": ["Nieprawidlowa konfiguracja monitoringu serwisu."]})
            continue
        document["configuration"] = site.model_dump(mode="json")
        try:
            if client is None:
                client = GscClient(_build_service(credentials_ref))
            gsc = fetch_site(client, site, start)
            gsc["anomalies"] = analyze(gsc, site)
            document["gsc"] = gsc
        except Exception as error:
            errors.append(f"GSC: {type(error).__name__}; pomiar niedostepny.")
        tech = {**identity, "status": None, "state": "not_run", "checked_at": None}
        if technical:
            if progress:
                progress((index + 0.5) / len(selected), f"{site.name}: technika")
            try:
                previous = previous_technical(site_config, root)
                with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=False) as http:
                    tech = collect_site(site, start, PoliteHttp(http), client,
                                        InspectionBudget(root, site.inspection_limit),
                                        api_key=os.environ.get("PAGESPEED_API_KEY"), previous=previous)
                tech["state"] = "measured"
                tech["comparison_measurement_at"] = previous.get("checked_at") if previous else None
                if previous:
                    tech["comparison"] = "previous_saved_measurement"
            except Exception as error:
                tech = {**identity, "status": None, "state": "no_data", "checked_at": None}
                errors.append(f"Technika: {type(error).__name__}; pomiar niedostepny.")
        document.update(tech=tech, errors=errors)
        document["summary"] = source_status(document["gsc"], tech)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid4().hex[:8]
        path = root / "data" / week_id(start) / site.id / f"{run_id}.json"
        try:
            write_json(path, document)
            document["saved_path"] = str(path)
        except OSError:
            document["errors"].append("Nie udalo sie zapisac pomiaru na dysku.")
        results.append(document)
    if progress:
        progress(1.0, "Gotowe")
    return results