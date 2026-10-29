"""Data collection and aggregation shared by the four dashboard views."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import pandas as pd

from analysis import (
    Period,
    _collect_category_urls,
    analyze_branded_search_period,
    analyze_site_daily_sources,
    analyze_source,
    build_url_metrics,
)
from config import SiteConfig
from services.gsc_service import GSCError, get_gsc_data
from services.sitemap_service import SitemapError


VIEWS = ("Przegląd ruchu", "Kategorie i treści", "Branded search", "Lista URL-i")
SOURCE_LABELS = {"web": "Search", "discover": "Discover", "news": "News"}
IDENTITY = ["site_key", "site_name", "source"]
DAILY_COLUMNS = IDENTITY + ["period", "date", "day", "clicks", "impressions"]


@dataclass(frozen=True)
class AnalysisRequest:
    view: str
    site_keys: tuple[str, ...]
    sources: tuple[str, ...]
    current: Period
    previous: Period | None = None
    urls: tuple[tuple[str, str], ...] = ()
    use_cache: bool = True


def parse_urls(text: str) -> list[str]:
    return list(dict.fromkeys(match.rstrip(",;") for match in re.findall(r'https?://[^\s<>"\']+', text)))


def _within_prefix(url: str, prefix: str) -> bool:
    parsed, base = urlsplit(url), urlsplit(prefix)
    path = base.path.rstrip("/")
    return (
        parsed.scheme == base.scheme
        and parsed.netloc.lower() == base.netloc.lower()
        and (not path or parsed.path == path or parsed.path.startswith(path + "/"))
    )


def url_candidates(url: str, sites: dict[str, SiteConfig]) -> list[str]:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            return []
        matches = []
        for key, site in sites.items():
            if not _within_prefix(url, site.base_url):
                continue
            prop = site.gsc_property
            if prop.startswith("sc-domain:"):
                domain = prop.removeprefix("sc-domain:").lower()
                valid = parsed.hostname.lower() == domain or parsed.hostname.lower().endswith("." + domain)
            else:
                valid = _within_prefix(url, prop)
            if valid:
                matches.append(key)
        return matches
    except ValueError:
        return []


def properties_overlap(sites: dict[str, SiteConfig]) -> bool:
    configs = list(sites.values())
    for index, first in enumerate(configs):
        for second in configs[index + 1:]:
            left, right = first.gsc_property, second.gsc_property
            if left.startswith("sc-domain:") and right.startswith("sc-domain:"):
                left, right = left[10:].lower(), right[10:].lower()
                if left == right or left.endswith("." + right) or right.endswith("." + left):
                    return True
            elif left.startswith("sc-domain:") or right.startswith("sc-domain:"):
                domain, prefix = (left[10:], right) if left.startswith("sc-domain:") else (right[10:], left)
                host = (urlsplit(prefix).hostname or "").lower()
                if host == domain.lower() or host.endswith("." + domain.lower()):
                    return True
            elif _within_prefix(left, right) or _within_prefix(right, left):
                return True
    return False


def url_filter_batches(urls: list[str], limit: int = 4000) -> list[str]:
    batches, current = [], []
    for url in dict.fromkeys(urls):
        escaped = re.escape(url)
        if len(escaped) + 6 > limit:
            raise ValueError(f"URL przekracza limit filtra GSC: {url}")
        if current and len("|".join(current + [escaped])) + 6 > limit:
            batches.append("^(?:" + "|".join(current) + ")$")
            current = []
        current.append(escaped)
    if current:
        batches.append("^(?:" + "|".join(current) + ")$")
    return batches


def _concat(frames: list[pd.DataFrame], columns=None) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True) if nonempty else pd.DataFrame(columns=columns)


def _identified(frame, site, source):
    return frame.assign(site_key=site.key, site_name=site.name, source=source)


def _daily_frame(data, period, period_name, site, source):
    dates = pd.date_range(period.start, period.end).strftime("%Y-%m-%d")
    indexed = data.set_index("date").reindex(dates) if not data.empty else pd.DataFrame(index=dates)
    for column in ("clicks", "impressions"):
        if column in data:
            indexed[column] = indexed[column].fillna(0)
        else:
            indexed[column] = float("nan") if column == "impressions" else 0.0
    indexed = indexed[["clicks", "impressions"]].rename_axis("date").reset_index()
    indexed["day"] = range(1, len(indexed) + 1)
    return _identified(indexed.assign(period=period_name), site, source)


def _overview_series(request, site, source, credentials):
    if request.previous:
        aligned = analyze_site_daily_sources(
            site, request.current, request.previous, credentials, [source], request.use_cache
        )[source]
        frames = []
        for name, period in (("current", request.current), ("previous", request.previous)):
            data = aligned[[f"{name}_date", f"{name}_clicks", f"{name}_impressions"]].rename(
                columns={f"{name}_date": "date", f"{name}_clicks": "clicks", f"{name}_impressions": "impressions"}
            ).dropna(subset=["date"])
            frames.append(_daily_frame(data, period, name, site, source))
        return _concat(frames)
    data = get_gsc_data(
        request.current.start, request.current.end, site.gsc_property, credentials,
        dimensions=("date",), search_type=source, use_cache=request.use_cache,
    )
    return _daily_frame(data, request.current, "current", site, source)


def _filtered_data(request, site, source, period, dimensions, credentials, urls):
    frames = [get_gsc_data(
        period.start, period.end, site.gsc_property, credentials,
        dimensions=dimensions, search_type=source, use_cache=request.use_cache, page_filter=expression,
    ) for expression in url_filter_batches(urls)]
    data = _concat(frames, list(dimensions) + ["clicks", "impressions", "ctr", "position"])
    if dimensions == ("date",) and not data.empty:
        return data.groupby("date", as_index=False)[["clicks", "impressions"]].sum()
    return data


def collect_dashboard(request: AnalysisRequest, sites: dict[str, SiteConfig], credentials: str, progress=None) -> dict:
    daily, pages, trees, devices, totals, statuses, notices = [], [], [], [], [], [], []
    analyses = {}
    for index, key in enumerate(request.site_keys):
        site = sites[key]
        if progress:
            progress(index / len(request.site_keys), site.name)
        category_urls = []
        if request.view == VIEWS[1]:
            try:
                category_urls = _collect_category_urls(site)
                if not category_urls:
                    notices.append(f"{site.name}: brak kategorii w sitemapach; adresy pozostaną niesklasyfikowane.")
                if not site.ymyl_paths:
                    notices.append(f"{site.name}: brak konfiguracji YMYL.")
            except (GSCError, SitemapError, ValueError) as exc:
                statuses.extend(dict(site_key=key, site_name=site.name, source=source, status="error", error=str(exc)) for source in request.sources)
                continue
        for source in request.sources:
            try:
                source_daily, source_pages, source_tree, source_device, source_totals = [], None, None, None, []
                if request.view == VIEWS[0]:
                    source_daily.append(_overview_series(request, site, source, credentials))
                elif request.view == VIEWS[1]:
                    result = analyze_source(site, request.current, request.previous, credentials, category_urls, source, request.use_cache, with_timeseries=True)
                    source_pages, source_tree, source_device = result.pages, result.tree, result.device
                    for name, period, column in (("current", request.current, "Current"), ("previous", request.previous, "Previous")):
                        if period is None:
                            continue
                        data = result.daily[["day", column]].copy()
                        data["date"] = (pd.Timestamp(period.start) + pd.to_timedelta(data["day"] - 1, unit="D")).dt.strftime("%Y-%m-%d")
                        source_daily.append(_daily_frame(data.rename(columns={column: "clicks"}), period, name, site, source))
                    analyses[(key, source)] = result
                elif request.view == VIEWS[2]:
                    if not site.brand_terms:
                        statuses.append(dict(site_key=key, site_name=site.name, source=source, status="unavailable", error="Brak skonfigurowanych fraz brandowych."))
                        continue
                    for name, period in (("current", request.current), ("previous", request.previous)):
                        if period:
                            data = analyze_branded_search_period(site, period, credentials, request.use_cache)
                            source_daily.append(_daily_frame(data, period, name, site, source))
                else:
                    urls = [url for url, owner in request.urls if owner == key]
                    if not urls:
                        statuses.append(dict(site_key=key, site_name=site.name, source=source, status="unavailable", error="Brak przypisanych URL-i."))
                        continue
                    period_pages = {}
                    for name, period in (("current", request.current), ("previous", request.previous)):
                        if not period:
                            continue
                        period_pages[name] = _filtered_data(request, site, source, period, ("page",), credentials, urls)
                        data = _filtered_data(request, site, source, period, ("date",), credentials, urls)
                        source_daily.append(_daily_frame(data, period, name, site, source))
                        whole = get_gsc_data(period.start, period.end, site.gsc_property, credentials, dimensions=("date",), search_type=source, use_cache=request.use_cache)
                        source_totals.append(dict(site_key=key, site_name=site.name, source=source, period=name, clicks=float(whole["clicks"].sum()), impressions=float(whole["impressions"].sum())))
                    source_pages = build_url_metrics(urls, period_pages["current"], period_pages.get("previous"), with_position=source == "web")
                daily.extend(source_daily)
                totals.extend(source_totals)
                for frame, target in ((source_pages, pages), (source_tree, trees), (source_device, devices)):
                    if frame is not None and not frame.empty:
                        target.append(_identified(frame, site, source))
                has_data = any(frame["clicks"].sum() > 0 for frame in source_daily)
                statuses.append(dict(site_key=key, site_name=site.name, source=source, status="ok" if has_data else "no_data", error=""))
            except (GSCError, SitemapError, ValueError) as exc:
                statuses.append(dict(site_key=key, site_name=site.name, source=source, status="error", error=str(exc)))
    return dict(
        request=request, daily=_concat(daily, DAILY_COLUMNS), pages=_concat(pages),
        trees=_concat(trees), devices=_concat(devices), totals=pd.DataFrame(totals),
        statuses=pd.DataFrame(statuses), analyses=analyses, notices=notices,
    )


def select_sources(frame: pd.DataFrame, sources: tuple[str, ...]) -> pd.DataFrame:
    return frame[frame["source"].isin(sources)].copy() if "source" in frame else frame.copy()


def daily_overview(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    totals = daily.groupby(IDENTITY + ["period"], sort=False)[["clicks", "impressions"]].sum(min_count=1)
    current = totals.xs("current", level="period").add_prefix("current_")
    if "previous" in daily["period"].values:
        previous = totals.xs("previous", level="period").add_prefix("previous_")
        current = current.join(previous, how="outer")
        for metric in ("clicks", "impressions"):
            current[f"{metric}_change"] = current[f"current_{metric}"] - current[f"previous_{metric}"]
            current[f"{metric}_change_pct"] = (current[f"{metric}_change"] / current[f"previous_{metric}"] * 100).where(current[f"previous_{metric}"] != 0)
    current["current_ctr"] = (current["current_clicks"] / current["current_impressions"]).where(current["current_impressions"] != 0)
    return current.reset_index()


def combine_sources(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    metric_columns = [column for column in frame if column.startswith(("current_", "previous_")) and column.endswith(("_clicks", "_impressions"))]
    combined = frame.groupby(keys, as_index=False, dropna=False, sort=False)[metric_columns].sum(min_count=1)
    for metric in ("clicks", "impressions"):
        if f"previous_{metric}" in combined:
            combined[f"{metric}_change"] = combined[f"current_{metric}"] - combined[f"previous_{metric}"]
            combined[f"{metric}_change_pct"] = (combined[f"{metric}_change"] / combined[f"previous_{metric}"] * 100).where(combined[f"previous_{metric}"] != 0)
    for period in ("current", "previous"):
        if f"{period}_impressions" in combined:
            combined[f"{period}_ctr"] = (combined[f"{period}_clicks"] / combined[f"{period}_impressions"]).where(combined[f"{period}_impressions"] != 0)
    return combined