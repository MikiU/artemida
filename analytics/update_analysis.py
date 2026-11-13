"""Traffic and page comparisons around a Google update."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from analysis import Period
from analytics.comparison import compare_periods
from services.gsc_service import GSCError, get_gsc_data


@dataclass(frozen=True)
class UpdateRequest:
    site_keys: tuple[str, ...]
    update_start: date
    update_end: date
    days: int
    use_cache: bool = False


def update_periods(request: UpdateRequest) -> tuple[Period, Period]:
    if request.update_end < request.update_start or request.days <= 0:
        raise ValueError("Nieprawidłowe daty aktualizacji lub długość okresu.")
    before_end = request.update_start - timedelta(days=1)
    after_start = request.update_end + timedelta(days=1)
    return (
        Period((before_end - timedelta(days=request.days - 1)).isoformat(), before_end.isoformat()),
        Period(after_start.isoformat(), (after_start + timedelta(days=request.days - 1)).isoformat()),
    )


def collect_update(request, sites, credentials, progress=None):
    before, after = update_periods(request)
    daily_frames, page_frames, statuses = [], [], []
    sources = ("web", "discover")
    for index, key in enumerate(request.site_keys):
        site = sites[key]
        if progress:
            progress(index / len(request.site_keys), site.name)
        for source in sources:
            identity = {"site_key": key, "site_name": site.name, "source": source}
            try:
                daily = get_gsc_data(
                    before.start, after.end, site.gsc_property, credentials,
                    dimensions=("date",), search_type=source, use_cache=request.use_cache,
                )
                daily_frames.append(daily.assign(**identity))
                dates = set(daily["date"].astype(str))
                expected = set(pd.date_range(before.start, before.end).strftime("%Y-%m-%d"))
                expected.update(pd.date_range(after.start, after.end).strftime("%Y-%m-%d"))
                missing = sorted(expected - dates)
                if missing:
                    statuses.append({**identity, "status": "incomplete", "error": f"Brak {len(missing)} dat w odpowiedzi GSC; porównanie niedostępne."})
                    continue
                before_pages = get_gsc_data(
                    before.start, before.end, site.gsc_property, credentials,
                    dimensions=("page",), search_type=source, use_cache=request.use_cache,
                )
                after_pages = get_gsc_data(
                    after.start, after.end, site.gsc_property, credentials,
                    dimensions=("page",), search_type=source, use_cache=request.use_cache,
                )
                page_frames.append(compare_periods(after_pages, before_pages).assign(**identity))
                statuses.append({**identity, "status": "ok", "error": ""})
            except GSCError as exc:
                statuses.append({**identity, "status": "error", "error": str(exc)})
    return {
        "request": request,
        "before": before,
        "after": after,
        "daily": pd.concat(daily_frames, ignore_index=True) if daily_frames else pd.DataFrame(),
        "pages": pd.concat(page_frames, ignore_index=True) if page_frames else pd.DataFrame(),
        "statuses": pd.DataFrame(statuses),
    }