"""Fetch five weekly GSC datasets using a read-only service account."""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httplib2
from googleapiclient.errors import HttpError

from weekly.common import (
    METRICS,
    ROOT,
    Settings,
    Site,
    load_settings,
    parser,
    setup_logging,
    today_gsc,
    week_id,
    week_start,
    write_json,
)

LOGGER = logging.getLogger(__name__)
ROW_LIMIT = 25_000


def create_service() -> Any:
    """Reuse the application's credentials and Search Console service builder."""
    from config import load_global_config
    from services.gsc_service import _build_service

    return _build_service(load_global_config().google_credentials_path)


class GscClient:
    """Bound API traffic and retry transient errors without logging secrets."""

    def __init__(
        self,
        service: Any,
        interval: float = 1,
        retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.service = service
        self.interval = interval
        self.retries = retries
        self.sleep = sleep
        self.clock = clock
        self.last_request: float | None = None

    def execute(self, make_request: Callable[[], Any]) -> dict[str, Any]:
        """Retry quota/transient errors using capped exponential backoff."""
        for attempt in range(self.retries + 1):
            if self.last_request is not None:
                delay = self.interval - (self.clock() - self.last_request)
                if delay > 0:
                    self.sleep(delay)
            self.last_request = self.clock()
            try:
                return dict(make_request().execute(num_retries=0))
            except HttpError as error:
                try:
                    reasons = {
                        item.get("reason")
                        for item in json.loads(error.content).get("error", {}).get("errors", [])
                    }
                except (ValueError, AttributeError, TypeError):
                    reasons = set()
                retryable = error.resp.status in {429, 500, 502, 503, 504} or (
                    error.resp.status == 403
                    and bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"})
                )
                if not retryable or attempt == self.retries:
                    raise RuntimeError(f"GSC HTTP {error.resp.status}; request failed") from None
                wait = min(60.0, 2.0**attempt + random.uniform(0, 1))
                retry_after = error.resp.get("retry-after", "")
                if str(retry_after).isdigit():
                    wait = max(wait, min(float(retry_after), 900))
                LOGGER.warning("GSC HTTP %s; retry %s/%s", error.resp.status, attempt + 1, self.retries)
                self.sleep(wait)
            except (OSError, httplib2.HttpLib2Error):
                if attempt == self.retries:
                    raise RuntimeError("GSC transport failed after retries") from None
                self.sleep(min(60, 2**attempt))
        raise RuntimeError("Unreachable retry state")

    def query(self, property_id: str, start: date, dimensions: list[str], *, end: date | None = None) -> dict[str, Any]:
        """Fetch every available page for a single dimension or totals."""
        period_end = end if end is not None else start + timedelta(days=6)
        if not start <= period_end <= start + timedelta(days=6):
            raise ValueError("Query end must fall within the requested ISO week")
        rows: list[dict[str, Any]] = []
        offset = 0
        aggregation = "auto" if dimensions == ["page"] else "byProperty"
        while True:
            body = {
                "startDate": start.isoformat(),
                "endDate": period_end.isoformat(),
                "dimensions": dimensions,
                "type": "web",
                "dataState": "final",
                "aggregationType": aggregation,
                "rowLimit": ROW_LIMIT,
                "startRow": offset,
            }
            response = self.execute(
                lambda: self.service.searchanalytics().query(siteUrl=property_id, body=body)
            )
            batch = response.get("rows", [])
            rows.extend(batch)
            if len(batch) < ROW_LIMIT:
                break
            offset += ROW_LIMIT
        return {
            "rows": rows,
            "row_count": len(rows),
            "pagination_exhausted": True,
            "all_search_rows_guaranteed": not bool(dimensions),
            "aggregation": response.get("responseAggregationType", aggregation),
        }


def fetch_site(client: GscClient, site: Site, start: date, today: date | None = None) -> dict[str, Any]:
    """Collect totals, pages, queries and available days for five weeks."""
    periods: dict[str, Any] = {}
    warnings = ["GSC returns top rows; query data excludes anonymized queries."]
    current = today or today_gsc()
    if start + timedelta(days=6) >= current - timedelta(days=2):
        warnings.append("Last two days may be incomplete or absent due to GSC processing delay.")
    for offset in range(5):
        period_start = start - timedelta(weeks=offset)
        period_end = period_start + timedelta(days=6)
        daily = client.query(site.gsc_property, period_start, ["date"])
        expected_dates = [(period_start + timedelta(days=day)).isoformat() for day in range(7)]
        available_dates = sorted({row["keys"][0] for row in daily["rows"]} & set(expected_dates))
        effective_end = date.fromisoformat(available_dates[-1]) if available_dates else None
        missing_dates = [
            day for day in expected_dates if day not in available_dates
        ]
        if effective_end is not None:
            total_data = client.query(site.gsc_property, period_start, [], end=effective_end)
            total_rows = total_data["rows"]
            totals = {metric: total_rows[0][metric] for metric in METRICS} if total_rows else None
            pages = client.query(site.gsc_property, period_start, ["page"], end=effective_end)
            queries = client.query(site.gsc_property, period_start, ["query"], end=effective_end)
        else:
            totals = None
            total_data = {"aggregation": "byProperty"}
            pages = {"rows": [], "row_count": 0, "pagination_exhausted": True,
                     "all_search_rows_guaranteed": False, "aggregation": "auto"}
            queries = {**pages, "aggregation": "byProperty"}
        periods[f"T-{offset}" if offset else "T"] = {
            "week": week_id(period_start),
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
            "totals": totals,
            "totals_aggregation": total_data["aggregation"],
            "pages": pages,
            "queries": queries,
            "available_dates": available_dates,
            "missing_dates": missing_dates,
            "coverage": {
                "requested_start": period_start.isoformat(), "requested_end": period_end.isoformat(),
                "effective_end": effective_end.isoformat() if effective_end else None,
                "available_day_count": len(available_dates), "expected_day_count": 7,
                "complete": not missing_dates and totals is not None,
            },
        }
        if missing_dates:
            warnings.append(f"{week_id(period_start)}: {len(missing_dates)} days without rows (zero traffic or missing data).")
            warnings.append(
                f"{week_id(period_start)}: dane do {effective_end.isoformat() if effective_end else 'brak'}, "
                f"{len(available_dates)}/7 dni. Niepełny tydzień; porównania z pełnymi tygodniami są wstępne."
            )
        if totals is None:
            warnings.append(f"{week_id(period_start)}: totals unavailable; do not treat as zero.")
    return {
        "schema_version": 1,
        "site": site.id,
        "name": site.name,
        "property": site.gsc_property,
        "week": week_id(start),
        "search_type": "web",
        "data_state": "final",
        "date_timezone": "America/Los_Angeles",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "warnings": warnings,
        "periods": periods,
    }


def run(settings: Settings, start: date, root: Path = ROOT) -> list[Path]:
    """Fetch configured real properties; never query placeholder sites."""
    if any(site.placeholder for site in settings.sites):
        raise ValueError("Replace both demo sites and set placeholder: false before using live APIs")
    client = GscClient(create_service(), settings.request_interval_seconds, settings.max_retries)
    paths = []
    for site in settings.sites:
        destination = root / "data" / week_id(start) / f"gsc_{site.id}.json"
        write_json(destination, fetch_site(client, site, start))
        LOGGER.info("Generated %s", destination)
        paths.append(destination)
    return paths


def main() -> int:
    """Run the standalone GSC stage."""
    setup_logging()
    args = parser(__doc__ or "Fetch GSC").parse_args()
    try:
        run(load_settings(args.config), week_start(args.week))
    except Exception as error:
        LOGGER.error("Fetch failed (%s). Check configuration, credentials and API access.", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())