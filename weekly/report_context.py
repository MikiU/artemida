"""Prepare deterministic cross-source statuses for interpretation-only agents."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from weekly.common import (
    METRICS,
    ROOT,
    Settings,
    Site,
    load_settings,
    parser,
    read_json,
    setup_logging,
    week_id,
    week_start,
    worst_status,
    write_json,
)

LOGGER = logging.getLogger(__name__)
COLORS = {"OK": "zielony", "WARN": "\u017c\u00f3\u0142ty", "FAIL": "czerwony"}


def aggregate_status(gsc_status, quality, tech_status):
    if gsc_status not in {None, "OK", "WARN", "FAIL"} or tech_status not in {None, "OK", "WARN", "FAIL"}:
        raise ValueError("Unknown source status")
    if quality not in {"INCOMPLETE", "AVAILABLE_WITH_GSC_LIMITATIONS"}:
        raise ValueError("Unknown GSC data quality")
    normalized = gsc_status or "WARN"
    if quality == "INCOMPLETE":
        normalized = worst_status([normalized, "WARN"])
    return worst_status([normalized, tech_status or "WARN"])


def site_context(site: Site, start: date, root: Path) -> dict[str, Any]:
    """Validate source identity and aggregate severity without inventing evidence."""
    week = week_id(start)
    sources = {
        "gsc": f"data/{week}/gsc_{site.id}.json",
        "tech": f"data/{week}/tech_{site.id}.json",
    }
    documents = {kind: read_json(root / path) for kind, path in sources.items()}
    for kind, document in documents.items():
        if (
            document.get("schema_version") != 1 or document.get("site") != site.id
            or document.get("property") != site.gsc_property or document.get("week") != week
        ):
            raise ValueError(f"Invalid source identity: {kind}/{site.id}")
    anomalies = documents["gsc"].get("anomalies")
    if not isinstance(anomalies, dict) or set(anomalies.get("metrics", {})) != set(METRICS):
        raise ValueError(f"Run detect_anomalies before preparing context: {site.id}")
    gsc_status = anomalies.get("status")
    tech_status = documents["tech"].get("status")
    quality = anomalies.get("data_quality")
    if gsc_status not in {None, "OK", "WARN", "FAIL"} or tech_status not in COLORS:
        raise ValueError(f"Unknown source status: {site.id}")
    if quality not in {"INCOMPLETE", "AVAILABLE_WITH_GSC_LIMITATIONS"}:
        raise ValueError(f"Unknown GSC data quality: {site.id}")
    reasons = []
    normalized_gsc: str = gsc_status or "WARN"
    if gsc_status is None or quality == "INCOMPLETE":
        normalized_gsc = worst_status([normalized_gsc, "WARN"])
        reasons.append("gsc_not_fully_evaluated")
    status = aggregate_status(gsc_status, quality, tech_status)
    return {
        "id": site.id, "name": site.name,
        "status": status, "color": COLORS[status],
        "gsc_status": gsc_status, "tech_status": tech_status,
        "gsc_data_quality": quality, "status_reasons": reasons,
        "sources": sources,
        "reports": {
            "insights": f"out/{week}/{site.id}_insights.md",
            "tech": f"out/{week}/{site.id}_tech.md",
            "followups": f"out/{week}/{site.id}_followups.md",
        },
    }


def run(settings: Settings, start: date, root: Path = ROOT) -> Path:
    """Write one report manifest only after every configured source is valid."""
    sites = [site_context(site, start, root) for site in settings.sites]
    status = worst_status([site["status"] for site in sites])
    week = week_id(start)
    context = {
        "schema_version": 1,
        "week": week,
        "generated_at": datetime.now(UTC).isoformat(),
        "period": {"start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat()},
        "site_count": len(sites),
        "status_counts": {severity: sum(site["status"] == severity for site in sites) for severity in COLORS},
        "status": status,
        "color": COLORS[status],
        "status_rule": "worst GSC/technical status; incomplete GSC is at least WARN",
        "sites": sites,
        "reports": {
            "draft": f"out/{week}/raport_draft.md",
            "final": f"out/{week}/raport.md",
            "review": f"out/{week}/review.md",
        },
    }
    path = root / "data" / week / "report_context.json"
    write_json(path, context)
    LOGGER.info("Generated %s", path)
    return path


def main() -> int:
    """Build report context from existing JSONs; no API calls are made."""
    setup_logging()
    args = parser(__doc__ or "Report context").parse_args()
    try:
        run(load_settings(args.config), week_start(args.week))
    except Exception as error:
        LOGGER.error("Report context failed (%s): %s", type(error).__name__, error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())