"""Calculate weekly SEO changes and anomalies without language models."""

from __future__ import annotations

import logging
import math
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from weekly.common import (
    METRICS,
    ROOT,
    Settings,
    Site,
    Threshold,
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
PERIOD_KEYS = ("T", "T-1", "T-2", "T-3", "T-4")


class Metrics(BaseModel):
    """Finite GSC metrics; CTR is a fraction, not a percentage."""

    model_config = ConfigDict(extra="allow")
    clicks: float = Field(ge=0, allow_inf_nan=False)
    impressions: float = Field(ge=0, allow_inf_nan=False)
    ctr: float = Field(ge=0, le=1, allow_inf_nan=False)
    position: float = Field(ge=0, allow_inf_nan=False)


class DimensionRow(Metrics):
    """A row grouped by exactly one page or query."""

    keys: list[str] = Field(min_length=1, max_length=1)


class DimensionTable(BaseModel):
    """Available dimension rows, not a complete traffic census."""

    model_config = ConfigDict(extra="allow")
    rows: list[DimensionRow]
    pagination_exhausted: bool


class Period(BaseModel):
    """One weekly snapshot produced by fetch_gsc."""

    model_config = ConfigDict(extra="allow")
    week: str
    start: date
    end: date
    totals: Metrics | None
    totals_aggregation: Literal["byProperty"]
    pages: DimensionTable
    queries: DimensionTable
    missing_dates: list[date]


class GscDocument(BaseModel):
    """Validate the numeric inputs before calculating any conclusions."""

    model_config = ConfigDict(extra="allow")
    schema_version: Literal[1]
    site: str
    property: str
    week: str
    search_type: Literal["web"]
    data_state: Literal["final"]
    periods: dict[str, Period]


def change(current: float | None, previous: float | None) -> dict[str, Any]:
    """Compute an absolute and percentage delta, guarding a zero base."""
    delta = current - previous if current is not None and previous is not None else None
    relative = delta / previous if delta is not None and previous else None
    return {
        "current": current,
        "previous": previous,
        "wow_delta": delta,
        "wow_change_pct": relative * 100 if relative is not None else None,
        "wow_reason": "missing_data" if delta is None else "zero_previous" if not previous else None,
    }


def metric_summary(
    metric: str,
    current: float | None,
    history: list[float | None],
    threshold: Threshold,
) -> dict[str, Any]:
    """Require both a WoW threshold breach and a two-sigma deviation."""
    previous = history[0]
    summary = change(current, previous)
    baseline = [value for value in history if value is not None]
    complete = len(baseline) == 4
    average = mean(baseline) if complete else None
    deviation = stdev(baseline) if complete else None
    from_mean = current - average if current is not None and average is not None else None
    z_score = from_mean / deviation if from_mean is not None and deviation else None
    higher_is_worse = metric == "position"
    relative = summary["wow_change_pct"]
    deterioration = (relative if higher_is_worse else -relative) if relative is not None else None
    boundary = (
        average + (2 if higher_is_worse else -2) * deviation
        if average is not None and deviation is not None else None
    )
    outside_band = (
        (current > boundary if higher_is_worse else current < boundary)
        if current is not None and boundary is not None else None
    )
    evaluable = current is not None and complete and deterioration is not None
    status: str | None = "OK" if evaluable else None
    breaches = {
        severity: (
            deterioration is not None
            and deterioration > limit * 100
            and not math.isclose(deterioration, limit * 100, rel_tol=1e-12, abs_tol=1e-12)
        )
        for severity, limit in (("warn", threshold.warn), ("fail", threshold.fail))
    }
    if evaluable and outside_band:
        if breaches["fail"]:
            status = "FAIL"
        elif breaches["warn"]:
            status = "WARN"
    summary.update({
        "direction": "higher_is_worse" if higher_is_worse else "lower_is_worse",
        "wow_change_pp": summary["wow_delta"] * 100 if metric == "ctr" and summary["wow_delta"] is not None else None,
        "baseline_count": len(baseline),
        "baseline_mean": average,
        "baseline_stddev": deviation,
        "delta_from_mean": from_mean,
        "change_from_mean_pct": from_mean / average * 100 if from_mean is not None and average else None,
        "z_score": z_score,
        "z_score_reason": (
            "missing_data" if from_mean is None else "zero_stddev" if not deviation else None
        ),
        "adverse_boundary": boundary,
        "outside_adverse_band": outside_band,
        "deterioration_pct": deterioration,
        "thresholds_pct": {"warn": threshold.warn * 100, "fail": threshold.fail * 100},
        "threshold_breaches": breaches,
        "status": status,
        "evaluation_reason": (
            "missing_data" if current is None or not complete
            else "zero_previous" if previous == 0 else None
        ),
    })
    return summary


def page_changes(current: DimensionTable, previous: DimensionTable) -> dict[str, Any]:
    """Rank available pages by absolute click delta, not by percentage."""
    current_rows = {row.keys[0]: row for row in current.rows}
    previous_rows = {row.keys[0]: row for row in previous.rows}
    changes = []
    for url in sorted(current_rows.keys() | previous_rows.keys()):
        current_row = current_rows.get(url)
        previous_row = previous_rows.get(url)
        summary = change(
            current_row.clicks if current_row else 0,
            previous_row.clicks if previous_row else 0,
        )
        changes.append({
            "url": url,
            **summary,
            "present_in_current_rows": current_row is not None,
            "present_in_previous_rows": previous_row is not None,
        })
    gains = sorted(
        (item for item in changes if item["wow_delta"] > 0),
        key=lambda item: (-item["wow_delta"], item["url"]),
    )
    losses = sorted(
        (item for item in changes if item["wow_delta"] < 0),
        key=lambda item: (item["wow_delta"], item["url"]),
    )
    return {
        "scope": "returned_page_rows; absent rows use zero for this ranking only",
        "growing_page_count": len(gains),
        "declining_page_count": len(losses),
        "top_gains": gains[:10],
        "top_losses": losses[:10],
    }


def normalize_brand(value: str) -> str:
    """Normalize Unicode, case and whitespace for substring matching."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def aggregate_queries(rows: list[DimensionRow]) -> dict[str, Any]:
    """Recalculate CTR and impression-weighted position for returned rows."""
    clicks = math.fsum(row.clicks for row in rows)
    impressions = math.fsum(row.impressions for row in rows)
    return {
        "query_count": len(rows),
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions if impressions else None,
        "position": (
            math.fsum(row.position * row.impressions for row in rows) / impressions
            if impressions else None
        ),
    }


def brand_breakdown(periods: dict[str, Period], terms: list[str]) -> dict[str, Any]:
    """Compare brand and non-brand only within observed query rows."""
    if not terms:
        return {"available": False, "reason": "brand_terms_missing", "periods": {}, "wow": {}}
    normalized_terms = [normalize_brand(term) for term in terms]
    grouped: dict[str, Any] = {}
    for label in PERIOD_KEYS:
        buckets: dict[str, list[DimensionRow]] = {"brand": [], "non_brand": []}
        for row in periods[label].queries.rows:
            query = normalize_brand(row.keys[0])
            bucket = "brand" if any(term in query for term in normalized_terms) else "non_brand"
            buckets[bucket].append(row)
        grouped[label] = {bucket: aggregate_queries(rows) for bucket, rows in buckets.items()}
    comparisons = {
        bucket: {
            metric: change(grouped["T"][bucket][metric], grouped["T-1"][bucket][metric])
            for metric in METRICS
        }
        for bucket in ("brand", "non_brand")
    }
    return {
        "scope": "returned_query_rows_only; anonymized and omitted queries are not included",
        "matching": "NFKC + casefold + collapsed whitespace; substring match",
        "brand_terms": terms,
        "periods": grouped,
        "wow": comparisons,
    }


def new_queries(periods: dict[str, Period]) -> list[dict[str, Any]]:
    """Find exact query keys absent from all four observed prior periods."""
    historical = {
        row.keys[0]
        for label in PERIOD_KEYS[1:]
        for row in periods[label].queries.rows
    }
    rows = [row for row in periods["T"].queries.rows if row.keys[0] not in historical]
    rows.sort(key=lambda row: (-row.clicks, -row.impressions, row.keys[0]))
    return [
        {"query": row.keys[0], **{metric: getattr(row, metric) for metric in METRICS}}
        for row in rows
    ]


def validate_document(payload: dict[str, Any], site: Site) -> GscDocument:
    """Reject wrong properties, mismatched weeks and ambiguous duplicate rows."""
    document = GscDocument.model_validate(payload)
    if document.site != site.id or document.property != site.gsc_property:
        raise ValueError("GSC site/property does not match configuration")
    if set(document.periods) != set(PERIOD_KEYS):
        raise ValueError("GSC document must contain T and T-1 through T-4")
    start = document.periods["T"].start
    if start.weekday() != 0 or document.week != week_id(start):
        raise ValueError("GSC week must match its Monday start")
    for offset, label in enumerate(PERIOD_KEYS):
        period = document.periods[label]
        expected = start - timedelta(weeks=offset)
        if period.start != expected or period.end != expected + timedelta(days=6) or period.week != week_id(expected):
            raise ValueError(f"Invalid weekly boundaries in {label}")
        for table in (period.pages, period.queries):
            if not table.pagination_exhausted:
                raise ValueError(f"Unfinished pagination in {label}")
            if len({row.keys[0] for row in table.rows}) != len(table.rows):
                raise ValueError(f"Duplicate dimension keys in {label}")
    return document


def analyze(payload: dict[str, Any], site: Site) -> dict[str, Any]:
    """Produce evidence, deltas and statuses without mutating source data."""
    document = validate_document(payload, site)
    periods = document.periods
    summaries = {}
    for metric in METRICS:
        current = getattr(periods["T"].totals, metric) if periods["T"].totals else None
        history = [
            getattr(periods[label].totals, metric) if periods[label].totals else None
            for label in PERIOD_KEYS[1:]
        ]
        summaries[metric] = metric_summary(metric, current, history, site.thresholds[metric])
    issues = [
        {"period": label, "missing_dates": [day.isoformat() for day in periods[label].missing_dates]}
        for label in PERIOD_KEYS if periods[label].missing_dates
    ]
    metric_statuses = [summary["status"] for summary in summaries.values()]
    aggregate_status: str | None = worst_status([status for status in metric_statuses if status is not None])
    incomplete = any(status is None for status in metric_statuses) or bool(issues)
    if incomplete and aggregate_status == "OK":
        aggregate_status = None
    discovered = new_queries(periods)
    return {
        "schema_version": 1,
        "method": {
            "baseline_periods": list(PERIOD_KEYS[1:]),
            "stddev": "sample (ddof=1)",
            "sigma_multiplier": 2,
            "threshold_operator": ">",
            "threshold_unit": "relative deterioration (percentage in output)",
            "zero_stddev": "z_score=null; compare current directly with the zero-width band",
            "seasonality": "no annual seasonality adjustment; four weekly observations only",
        },
        "status": aggregate_status,
        "data_quality": "INCOMPLETE" if incomplete else "AVAILABLE_WITH_GSC_LIMITATIONS",
        "missing_day_issues": issues,
        "metrics": summaries,
        "flagged_metrics": [metric for metric, summary in summaries.items() if summary["status"] in {"WARN", "FAIL"}],
        "pages": page_changes(periods["T"].pages, periods["T-1"].pages),
        "brand_non_brand": brand_breakdown(periods, site.brand_terms),
        "new_query_scope": "absent from returned T-1..T-4 rows, not proof of first-ever search",
        "new_query_count": len(discovered),
        "new_queries": discovered,
    }


def run(settings: Settings, start: date, root: Path = ROOT) -> list[Path]:
    """Validate every site before adding anomaly results to source JSONs."""
    week = week_id(start)
    pending = []
    for site in settings.sites:
        path = root / "data" / week / f"gsc_{site.id}.json"
        payload = read_json(path)
        if payload.get("week") != week:
            raise ValueError(f"Week mismatch in {path.name}")
        payload["anomalies"] = analyze(payload, site)
        pending.append((path, payload))
    for path, payload in pending:
        write_json(path, payload)
        LOGGER.info("Updated %s; anomaly status: %s", path, payload["anomalies"]["status"] or "NOT_EVALUATED")
    return [path for path, _ in pending]


def main() -> int:
    """Run the offline anomaly stage for a completed ISO week."""
    setup_logging()
    args = parser(__doc__ or "Detect anomalies").parse_args()
    try:
        run(load_settings(args.config), week_start(args.week))
    except Exception as error:
        LOGGER.error("Anomaly stage failed (%s): %s", type(error).__name__, error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())