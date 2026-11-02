"""Bounded, robots-aware technical checks with weekly change tracking."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from defusedxml.ElementTree import fromstring

from weekly.common import (
    ROOT,
    Settings,
    Site,
    load_settings,
    parser,
    read_json,
    setup_logging,
    today_gsc,
    week_id,
    week_start,
    worst_status,
    write_json,
)
from weekly.fetch_gsc import GscClient, create_service

LOGGER = logging.getLogger(__name__)
USER_AGENT = "SeoWeekly/1.0 (local weekly SEO monitoring)"
PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
MAX_XML_BYTES = 50 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_SITEMAPS = 50
MAX_URLS = 100_000
SAMPLE_SIZE = 20
REDIRECT_CODES = {301, 302, 303, 307, 308}


class CheckError(RuntimeError):
    """A safe error code that never contains request credentials."""


class PoliteHttp:
    """Stream bounded responses and throttle every attempt, including retries."""

    def __init__(
        self,
        client: httpx.Client,
        interval: float = 1,
        retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.interval = interval
        self.retries = retries
        self.sleep = sleep
        self.clock = clock
        self.last_request: float | None = None
        self.request_count = 0

    def get(
        self,
        url: str,
        limit: int = MAX_HTML_BYTES,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> httpx.Response:
        """Retry transient HTTP failures without logging URLs or API keys."""
        for attempt in range(self.retries + 1):
            if self.last_request is not None:
                delay = self.interval - (self.clock() - self.last_request)
                if delay > 0:
                    self.sleep(delay)
            self.last_request = self.clock()
            self.request_count += 1
            try:
                with self.client.stream(
                    "GET", url, params=params, headers=headers,
                    timeout=timeout, follow_redirects=False,
                ) as response:
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise CheckError("response_size_limit")
                        chunks.append(chunk)
                    result = httpx.Response(
                        response.status_code,
                        content=b"".join(chunks),
                        headers={
                            key: value for key, value in response.headers.items()
                            if key.lower() not in {"content-encoding", "content-length"}
                        },
                        request=response.request,
                    )
                if result.status_code not in {429, 500, 502, 503, 504}:
                    return result
                if attempt == self.retries:
                    return result
                retry_after = result.headers.get("retry-after", "")
                wait = min(float(retry_after), 900) if retry_after.isdigit() else 0
                self.sleep(max(wait, min(60, 2**attempt)))
            except httpx.RequestError:
                if attempt == self.retries:
                    raise CheckError("network_error") from None
                self.sleep(min(60, 2**attempt))
        raise CheckError("retry_exhausted")


def same_origin(url: str, base: str) -> bool:
    """Reject foreign origins, credentials and non-HTTP destinations."""
    try:
        target, source = urlsplit(url), urlsplit(base)
        return (
            target.scheme in {"http", "https"}
            and target.scheme == source.scheme
            and target.hostname == source.hostname
            and target.port == source.port
            and target.username is None and target.password is None
        )
    except ValueError:
        return False


class Crawler:
    """Follow only same-origin redirects after checking each robots rule."""

    def __init__(self, http: PoliteHttp, base_url: str, user_agent: str = USER_AGENT) -> None:
        self.http = http
        self.base_url = base_url
        self.user_agent = user_agent
        self.robot_parser = RobotFileParser()
        self.robots_loaded = False
        self.robots_result: dict[str, Any] = {}

    def allowed(self, url: str) -> bool:
        """Require both an allowed origin and a loaded robots policy."""
        return (
            same_origin(url, self.base_url) and self.robots_loaded
            and self.robot_parser.can_fetch(self.user_agent, url)
        )

    def get(self, url: str, limit: int = MAX_HTML_BYTES, robots: bool = True) -> httpx.Response:
        """Check every redirect target before performing the next request."""
        visited: set[str] = set()
        target = urldefrag(url)[0]
        for _ in range(6):
            if not same_origin(target, self.base_url):
                raise CheckError("foreign_origin")
            if robots and not self.allowed(target):
                raise CheckError("robots_disallowed")
            if target in visited:
                raise CheckError("redirect_loop")
            visited.add(target)
            response = self.http.get(target, limit=limit)
            if response.status_code not in REDIRECT_CODES:
                return response
            location = response.headers.get("location")
            if not location:
                raise CheckError("redirect_without_location")
            target = urldefrag(urljoin(target, location))[0]
        raise CheckError("redirect_limit")

    def load_robots(self) -> dict[str, Any]:
        """Fail closed on uncertain robots access; absent robots allow crawling."""
        url = urljoin(self.base_url, "/robots.txt")
        self.robot_parser = RobotFileParser()
        self.robot_parser.parse(["User-agent: *", "Disallow: /"])
        result: dict[str, Any] = {
            "url": url, "status": "FAIL", "http_status": None,
            "sha256": None, "sitemap_urls": [], "reason": None,
        }
        try:
            response = self.get(url, limit=512_000, robots=False)
            result["http_status"] = response.status_code
            if response.status_code == 200:
                if "<html" in response.text[:512].lower():
                    raise CheckError("robots_is_html")
                self.robot_parser = RobotFileParser()
                self.robot_parser.parse(response.text.splitlines())
                result["sha256"] = hashlib.sha256(response.content).hexdigest()
                result["sitemap_urls"] = self.robot_parser.site_maps() or []
                delay = self.robot_parser.crawl_delay(self.user_agent)
                rate = self.robot_parser.request_rate(self.user_agent)
                minimum = rate.seconds / rate.requests if rate and rate.requests else 0
                crawl_delay = float(delay) if isinstance(delay, (int, float)) else 0
                self.http.interval = max(self.http.interval, crawl_delay, minimum)
                result["status"] = "OK"
            elif response.status_code in {404, 410}:
                self.robot_parser = RobotFileParser()
                self.robot_parser.parse(["User-agent: *", "Disallow:"])
                result.update(status="WARN", reason="robots_not_found")
            else:
                result["reason"] = "robots_unavailable_crawling_disabled"
        except CheckError as error:
            result["reason"] = str(error)
        self.robots_loaded = True
        result["minimum_request_interval_seconds"] = self.http.interval
        self.robots_result = result
        return result


def timestamp(value: str | None) -> str | None:
    """Normalize an ISO lastmod date/time for stable chronological sorting."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except ValueError:
        return None


def sitemap_entries(content: bytes) -> tuple[str, list[dict[str, Any]]]:
    """Parse namespace-independent sitemap XML, including bounded gzip files."""
    if content.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as archive:
            content = archive.read(MAX_XML_BYTES + 1)
    if len(content) > MAX_XML_BYTES:
        raise CheckError("sitemap_uncompressed_size_limit")
    root = fromstring(content)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"urlset", "sitemapindex"}:
        raise CheckError("not_a_sitemap")
    entries = []
    for element in root:
        expected = "url" if kind == "urlset" else "sitemap"
        if element.tag.rsplit("}", 1)[-1] != expected:
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: child.text for child in element}
        location = (fields.get("loc") or "").strip()
        if not location:
            raise CheckError("sitemap_missing_loc")
        raw_lastmod = fields.get("lastmod")
        entries.append({"url": location, "lastmod": timestamp(raw_lastmod), "lastmod_raw": raw_lastmod})
    return kind, entries


def check_sitemaps(
    crawler: Crawler,
    site: Site,
    max_documents: int = MAX_SITEMAPS,
    max_urls: int = MAX_URLS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Discover primary sitemaps and collect a bounded, deduplicated URL sample."""
    declared = crawler.robots_result.get("sitemap_urls", [])
    seeds = [site.sitemap_url] if site.sitemap_url else declared
    source = "configured" if site.sitemap_url else "robots"
    if not seeds:
        seeds = site.category_sitemaps
        source = "category_fallback" if seeds else "none"
    queue = [{"url": url, "lastmod": None} for url in dict.fromkeys(seeds)]
    visited: set[str] = set()
    urls: dict[str, dict[str, Any]] = {}
    documents: list[dict[str, Any]] = []
    rejected = 0
    truncated = False
    while queue and len(documents) < max_documents:
        queue.sort(key=lambda entry: (entry["lastmod"] or "", entry["url"]), reverse=True)
        pending = queue.pop(0)
        url = pending["url"]
        if url in visited:
            continue
        visited.add(url)
        document: dict[str, Any] = {"url": url, "status": "OK", "http_status": None}
        try:
            response = crawler.get(url, limit=MAX_XML_BYTES)
            document["http_status"] = response.status_code
            if response.status_code != 200:
                raise CheckError(f"http_{response.status_code}")
            kind, entries = sitemap_entries(response.content)
            document.update(kind=kind, entry_count=len(entries))
            for entry in entries:
                location = urldefrag(entry["url"])[0]
                entry["url"] = location
                if not same_origin(location, site.base_url):
                    rejected += 1
                    continue
                if kind == "sitemapindex":
                    if location not in visited:
                        queue.append(entry)
                elif location in urls:
                    if (entry["lastmod"] or "") > (urls[location]["lastmod"] or ""):
                        urls[location] = entry
                elif len(urls) < max_urls:
                    urls[location] = entry
                else:
                    truncated = True
        except Exception as error:
            document.update(
                status="WARN" if isinstance(error, CheckError) and str(error) in {"robots_disallowed", "foreign_origin"} else "FAIL",
                reason=str(error) if isinstance(error, CheckError) else "invalid_or_unsafe_sitemap",
            )
        documents.append(document)
        if truncated:
            break
    truncated = truncated or any(entry["url"] not in visited for entry in queue)
    entries = sorted(urls.values(), key=lambda entry: (entry["lastmod"] or "", entry["url"]), reverse=True)
    lastmods = [entry["lastmod"] for entry in entries if entry["lastmod"]]
    missing_lastmod = len(entries) - len(lastmods)
    warnings = []
    if not seeds:
        warnings.append("no_sitemap_configured_or_declared")
    if source == "category_fallback":
        warnings.append("category_sitemaps_may_not_contain_recent_articles")
    if truncated:
        warnings.append("sitemap_budget_reached_counts_and_sample_are_partial")
    if missing_lastmod:
        warnings.append("latest_sample_uncertain_without_valid_lastmod")
    if not entries:
        warnings.append("no_sample_urls_available")
    if rejected:
        warnings.append("foreign_origin_entries_excluded")
    return {
        "status": worst_status([doc["status"] for doc in documents] + (["WARN"] if warnings else [])),
        "source": source,
        "documents": documents,
        "url_count": len(urls),
        "url_count_complete": not truncated and not rejected and all(doc["status"] == "OK" for doc in documents) and bool(documents),
        "lastmod": {"oldest": min(lastmods) if lastmods else None, "newest": max(lastmods) if lastmods else None, "missing_or_invalid_count": missing_lastmod},
        "sample_order": "valid_lastmod_desc_then_url; missing_lastmod_last",
        "truncated": truncated,
        "rejected_url_count": rejected,
        "warnings": warnings,
        "budgets": {"max_documents": max_documents, "max_urls": max_urls},
    }, entries[:SAMPLE_SIZE]


def json_nodes(value: Any) -> Iterator[dict[str, Any]]:
    """Walk nested JSON-LD graphs without evaluating script contents."""
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from json_nodes(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from json_nodes(nested)


def news_article_schema(soup: BeautifulSoup) -> dict[str, Any]:
    """Check NewsArticle fields in JSON-LD graphs and HTML microdata."""
    nodes: list[dict[str, Any]] = []
    invalid_scripts = 0
    for script in soup.find_all("script", attrs={"type": re.compile(r"^application/ld\+json\s*$", re.I)}):
        try:
            nodes.extend(json_nodes(json.loads(script.get_text())))
        except (ValueError, RecursionError):
            invalid_scripts += 1
    references = {node["@id"]: node for node in nodes if isinstance(node.get("@id"), str) and len(node) > 1}
    articles: list[dict[str, Any]] = []

    def field_present(value: Any, field: str, depth: int = 0) -> bool:
        if depth > 4:
            return False
        if isinstance(value, list):
            return any(field_present(item, field, depth + 1) for item in value)
        if isinstance(value, str):
            return bool(timestamp(value)) if field == "datePublished" else bool(value.strip())
        if isinstance(value, dict):
            reference = value.get("@id")
            resolved = references.get(reference, value) if isinstance(reference, str) else value
            keys = ("name",) if field == "author" else ("url", "contentUrl")
            return any(field_present(resolved.get(key), field, depth + 1) for key in keys)
        return False

    for node in nodes:
        types = node.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if not isinstance(types, list) or not any(
            isinstance(kind, str) and kind.rsplit("/", 1)[-1].rsplit("#", 1)[-1] == "NewsArticle"
            for kind in types
        ):
            continue
        articles.append({"format": "JSON-LD", "fields": {field: field_present(node.get(field), field) for field in ("author", "datePublished", "image")}})
    for element in soup.find_all(attrs={"itemscope": True, "itemtype": re.compile(r"(?:/|#)NewsArticle(?:\s|$)")}):
        fields = {}
        for field in ("author", "datePublished", "image"):
            values = []
            for tag in element.find_all(attrs={"itemprop": re.compile(rf"(?:^|\s){field}(?:\s|$)")}):
                values.append(tag.get("content") or tag.get("datetime") or tag.get("src") or tag.get("href") or tag.get_text(strip=True))
            fields[field] = any(field_present(value, field) for value in values)
        articles.append({"format": "microdata", "fields": fields})
    complete = any(all(article["fields"].values()) for article in articles)
    return {
        "status": "OK" if complete and not invalid_scripts else "WARN",
        "present": bool(articles), "article_count": len(articles),
        "articles": articles, "invalid_jsonld_script_count": invalid_scripts,
        "supported_formats": ["JSON-LD", "microdata"],
    }


def check_page(crawler: Crawler, entry: dict[str, Any]) -> dict[str, Any]:
    """Inspect HTML indexing signals without fetching linked resources."""
    result: dict[str, Any] = {
        "url": entry["url"], "lastmod": entry.get("lastmod"),
        "http_status": None, "status": "WARN", "issues": [],
    }
    try:
        response = crawler.get(entry["url"])
        result.update(http_status=response.status_code, final_url=str(response.url))
        if response.status_code != 200:
            result.update(status="FAIL", issues=[f"http_{response.status_code}"])
            return result
        if not any(kind in response.headers.get("content-type", "").lower() for kind in ("text/html", "application/xhtml+xml")):
            result["issues"] = ["not_html"]
            return result
        soup = BeautifulSoup(response.text, "html.parser")
        canonicals = [
            urljoin(str(response.url), str(tag["href"]))
            for tag in soup.find_all("link", rel=lambda rel: rel and "canonical" in rel)
            if tag.get("href")
        ]
        directives = [
            {"agent": str(tag.get("name", "")).lower(), "content": str(tag.get("content", ""))}
            for tag in soup.find_all("meta")
            if str(tag.get("name", "")).lower() in {"robots", "googlebot"}
        ]
        headers = response.headers.get_list("x-robots-tag")
        noindex = any(
            re.search(r"(?:^|[\s,:;])(?:noindex|none)(?:$|[\s,;])", content, re.I)
            for content in [item["content"] for item in directives] + headers
        )
        schema = news_article_schema(soup)
        issues = []
        if len(canonicals) != 1:
            issues.append("canonical_missing_or_multiple")
        elif urldefrag(canonicals[0])[0] != urldefrag(str(response.url))[0]:
            issues.append("canonical_points_elsewhere")
        if str(response.url) != entry["url"]:
            issues.append("redirected")
        if noindex:
            issues.append("noindex")
        if schema["status"] != "OK":
            issues.append("newsarticle_missing_incomplete_or_invalid")
        result.update(
            status="FAIL" if noindex else "WARN" if issues else "OK",
            canonical=canonicals, meta_robots=directives, x_robots_tag=headers,
            noindex=noindex, newsarticle=schema, issues=issues,
        )
    except CheckError as error:
        result["issues"] = [str(error)]
    return result


def parse_cwv(payload: dict[str, Any]) -> dict[str, Any]:
    """Use field percentiles and API categories; never substitute lab metrics."""
    definitions = {
        "LCP": ("LARGEST_CONTENTFUL_PAINT_MS", "ms"),
        "INP": ("INTERACTION_TO_NEXT_PAINT", "ms"),
        "CLS": ("CUMULATIVE_LAYOUT_SHIFT_SCORE", "PSI encoded score (raw)"),
    }
    metrics: dict[str, Any] = {}
    for name, (key, unit) in definitions.items():
        for scope, field in (("url", "loadingExperience"), ("origin", "originLoadingExperience")):
            experience = payload.get(field) or {}
            data = (experience.get("metrics") or {}).get(key) or {}
            percentile = data.get("percentile")
            category = data.get("category")
            if (
                isinstance(percentile, (int, float)) and not isinstance(percentile, bool)
                and math.isfinite(percentile) and percentile >= 0
                and category in {"FAST", "AVERAGE", "SLOW"}
            ):
                metrics[name] = {
                    "percentile_raw": percentile, "unit": unit,
                    "category": category, "scope": scope, "source_id": experience.get("id"),
                    "status": {"FAST": "OK", "AVERAGE": "WARN", "SLOW": "FAIL"}[category],
                }
                break
        else:
            metrics[name] = None
    statuses = [value["status"] if value else "WARN" for value in metrics.values()]
    return {
        "status": worst_status(statuses), "metrics": metrics,
        "source": "PSI field data; origin fallback explicitly labelled per metric",
        "complete": all(metrics.values()),
        "reason": None if all(metrics.values()) else "field_metrics_missing",
    }


def check_pagespeed(crawler: Crawler, site: Site, api_key: str | None) -> dict[str, Any]:
    """Audit only configured, robots-allowed URLs; keep API keys out of URLs."""
    results = []
    cache: dict[str, dict[str, Any]] = {}
    for role in ("article", "home", "category"):
        url = site.representative_urls[role]
        result: dict[str, Any] = {"role": role, "url": url, "status": "WARN"}
        if not url:
            result["reason"] = "url_not_configured"
        elif not crawler.allowed(url):
            result["reason"] = "robots_disallowed_or_foreign_origin"
        elif not api_key:
            result["reason"] = "pagespeed_api_key_missing"
        else:
            if url not in cache:
                try:
                    response = crawler.http.get(
                        PSI_ENDPOINT, limit=10 * 1024 * 1024,
                        params={"url": url, "strategy": "mobile", "category": "performance"},
                        headers={"X-Goog-Api-Key": api_key}, timeout=90,
                    )
                    if response.status_code != 200:
                        raise CheckError(f"pagespeed_http_{response.status_code}")
                    payload = response.json()
                    cwv = parse_cwv(payload)
                    cache[url] = {
                        "status": cwv["status"], "cwv": cwv,
                        "strategy": "mobile", "analysis_utc_timestamp": payload.get("analysisUTCTimestamp"),
                    }
                except Exception as error:
                    cache[url] = {"status": "WARN", "reason": str(error) if isinstance(error, CheckError) else "pagespeed_invalid_response"}
            result.update(cache[url])
        results.append(result)
    return {"status": worst_status([result["status"] for result in results]), "urls": results}


class InspectionBudget:
    """Persist a conservative per-property daily cap, counting failed attempts."""

    def __init__(self, root: Path, limit: int, day: date | None = None) -> None:
        self.directory = root / "data" / "_inspection_usage" / (day or today_gsc()).isoformat()
        self.limit = limit

    def reserve(self, property_id: str) -> None:
        """Reserve before sending; an exclusive lock fails closed on concurrency."""
        self.directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(property_id.encode()).hexdigest()
        path = self.directory / f"{key}.json"
        lock = self.directory / f"{key}.lock"
        try:
            handle = lock.open("x", encoding="utf-8")
        except FileExistsError:
            raise CheckError("inspection_budget_locked") from None
        try:
            with handle:
                count = read_json(path)["attempts"] if path.exists() else 0
                if not isinstance(count, int) or count < 0:
                    raise CheckError("inspection_budget_invalid")
                if count >= self.limit:
                    raise CheckError("inspection_daily_local_budget_exhausted")
                write_json(path, {"attempts": count + 1, "limit": self.limit})
        finally:
            lock.unlink(missing_ok=True)


def in_property(url: str, property_id: str) -> bool:
    """Match a Domain property or an exact URL-prefix property."""
    if property_id.startswith("sc-domain:"):
        domain = property_id.removeprefix("sc-domain:").lower()
        host = (urlsplit(url).hostname or "").lower()
        return host == domain or host.endswith("." + domain)
    return url.startswith(property_id)


def check_inspection(
    crawler: Crawler,
    site: Site,
    sample: list[dict[str, Any]],
    client: GscClient | None,
    budget: InspectionBudget,
) -> dict[str, Any]:
    """Inspect only a limited sample of indexed versions, never a full site."""
    if site.inspection_limit == 0:
        return {"status": "WARN", "reason": "inspection_disabled", "urls": [], "api_attempts": 0}
    if client is None:
        return {"status": "WARN", "reason": "inspection_credentials_unavailable", "urls": [], "api_attempts": 0}
    results = []
    attempts = 0
    for entry in sample[:site.inspection_limit]:
        url = entry["url"]
        result: dict[str, Any] = {"url": url, "status": "WARN"}
        if not crawler.allowed(url) or not in_property(url, site.gsc_property):
            result["reason"] = "robots_disallowed_or_outside_property"
        else:
            try:
                def request() -> Any:
                    nonlocal attempts
                    budget.reserve(site.gsc_property)
                    attempts += 1
                    return client.service.urlInspection().index().inspect(body={
                        "inspectionUrl": url, "siteUrl": site.gsc_property, "languageCode": "pl-PL",
                    })

                payload = client.execute(request)
                index = payload.get("inspectionResult", {}).get("indexStatusResult", {})
                verdict = index.get("verdict")
                status = {"PASS": "OK", "FAIL": "FAIL", "PARTIAL": "WARN", "NEUTRAL": "WARN"}.get(verdict, "WARN")
                if index.get("robotsTxtState") == "DISALLOWED" or index.get("indexingState") in {"BLOCKED_BY_META_TAG", "BLOCKED_BY_HTTP_HEADER"}:
                    status = "FAIL"
                result.update(status=status, index_status=index)
                if not verdict:
                    result["reason"] = "inspection_verdict_missing"
            except Exception as error:
                result["reason"] = str(error) if isinstance(error, CheckError) else "inspection_api_error"
        results.append(result)
    return {
        "status": worst_status([result["status"] for result in results] or ["WARN"]),
        "urls": results, "api_attempts": attempts,
        "sample_limit": site.inspection_limit, "daily_local_limit": budget.limit,
        "scope": "Google indexed version, not a live indexability test",
        "reason": "no_sample_urls" if not results else None,
    }


def stable_observation(value: Any) -> Any:
    """Remove timestamps and comparison metadata from weekly comparisons."""
    if isinstance(value, dict):
        return {
            key: stable_observation(nested) for key, nested in value.items()
            if key not in {"checked_at", "analysis_utc_timestamp", "changed_since_last_week", "comparison", "changes", "api_attempts"}
        }
    if isinstance(value, list):
        return [stable_observation(item) for item in value]
    return value


def compare_observations(current: Any, previous: Any, path: str = "") -> list[dict[str, Any]]:
    """Return explicit before/after evidence for every changed field."""
    if isinstance(current, dict) and isinstance(previous, dict):
        changes = []
        for key in sorted(current.keys() | previous.keys()):
            child_path = f"{path}/{key}"
            if key not in current or key not in previous:
                changes.append({"path": child_path, "before": previous.get(key), "after": current.get(key)})
            else:
                changes.extend(compare_observations(current[key], previous[key], child_path))
        return changes
    return [] if current == previous else [{"path": path, "before": previous, "after": current}]


def add_comparison(document: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Compare stable signals per URL rather than comparing sample positions."""
    if previous is None:
        for section in ("robots", "sitemap", "pagespeed", "inspection"):
            document[section]["changed_since_last_week"] = None
        for row in document["sample"]:
            row["changed_since_last_week"] = None
        document.update(changed_since_last_week=None, comparison="no_previous_week", changes=[])
        return document
    changes = []
    for section in ("robots", "sitemap", "pagespeed"):
        current_section = document[section]
        previous_section = previous.get(section)
        section_changes = compare_observations(stable_observation(current_section), stable_observation(previous_section), f"/{section}")
        current_section["changed_since_last_week"] = bool(section_changes)
        changes.extend(section_changes)
    for section in ("sample", "inspection"):
        current_rows = document["sample"] if section == "sample" else document["inspection"]["urls"]
        previous_rows = previous.get("sample", []) if section == "sample" else previous.get("inspection", {}).get("urls", [])
        old_by_url = {row["url"]: row for row in previous_rows}
        current_urls = {row["url"] for row in current_rows}
        for row in current_rows:
            old = old_by_url.get(row["url"])
            if old is None:
                row.update(changed_since_last_week=None, comparison="not_in_previous_sample")
                continue
            row_changes = compare_observations(stable_observation(row), stable_observation(old), f"/{section}/{row['url']}")
            row["changed_since_last_week"] = bool(row_changes)
            changes.extend(row_changes)
        document[f"{section}_sample_comparison"] = {
            "common_url_count": len(current_urls & old_by_url.keys()),
            "new_sample_url_count": len(current_urls - old_by_url.keys()),
            "removed_sample_url_count": len(old_by_url.keys() - current_urls),
        }
    inspection_changes = compare_observations(
        stable_observation({key: value for key, value in document["inspection"].items() if key != "urls"}),
        stable_observation({key: value for key, value in previous.get("inspection", {}).items() if key != "urls"}),
        "/inspection",
    )
    document["inspection"]["changed_since_last_week"] = bool(inspection_changes) or any(
        row.get("changed_since_last_week") for row in document["inspection"]["urls"]
    )
    changes.extend(inspection_changes)
    document.update(changed_since_last_week=bool(changes), comparison="previous_week", changes=changes)
    return document


def collect_site(
    site: Site,
    start: date,
    http: PoliteHttp,
    inspection_client: GscClient | None,
    budget: InspectionBudget,
    api_key: str | None = None,
    previous: dict[str, Any] | None = None,
    user_agent: str = USER_AGENT,
) -> dict[str, Any]:
    """Collect a current technical snapshot labelled by the report ISO week."""
    crawler = Crawler(http, site.base_url, user_agent)
    robots = crawler.load_robots()
    sitemap, entries = check_sitemaps(crawler, site)
    sample = [check_page(crawler, entry) for entry in entries]
    pagespeed = check_pagespeed(crawler, site, api_key)
    inspection = check_inspection(crawler, site, entries, inspection_client, budget)
    document = {
        "schema_version": 1, "site": site.id, "name": site.name,
        "property": site.gsc_property, "week": week_id(start),
        "checked_at": datetime.now(UTC).isoformat(),
        "measurement_scope": "live snapshot at checked_at, not historical reconstruction",
        "user_agent": user_agent,
        "status": worst_status([robots["status"], sitemap["status"], pagespeed["status"], inspection["status"]] + [page["status"] for page in sample]),
        "robots": robots, "sitemap": sitemap, "sample": sample,
        "sample_size": len(sample), "sample_limit": SAMPLE_SIZE,
        "pagespeed": pagespeed, "inspection": inspection,
    }
    return add_comparison(document, previous)


def run(settings: Settings, start: date, root: Path = ROOT) -> list[Path]:
    """Run bounded live checks and persist one technical JSON per site."""
    if any(site.placeholder for site in settings.sites):
        raise ValueError("Replace placeholder sites before running technical checks")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    inspection_client = None
    try:
        inspection_client = GscClient(create_service(), settings.request_interval_seconds, settings.max_retries)
    except Exception as error:
        LOGGER.warning("URL Inspection unavailable (%s); results will record WARN", type(error).__name__)
    user_agent = os.environ.get("SEO_USER_AGENT", USER_AGENT)
    paths = []
    with httpx.Client(headers={"User-Agent": user_agent}, timeout=30, follow_redirects=False) as client:
        http = PoliteHttp(client, settings.request_interval_seconds, settings.max_retries)
        for site in settings.sites:
            previous_path = root / "data" / week_id(start - timedelta(weeks=1)) / f"tech_{site.id}.json"
            previous = read_json(previous_path) if previous_path.exists() else None
            if previous is not None and (
                previous.get("site") != site.id
                or previous.get("property") != site.gsc_property
                or previous.get("week") != week_id(start - timedelta(weeks=1))
            ):
                raise ValueError("Previous technical snapshot has mismatched site/week")
            document = collect_site(
                site, start, http, inspection_client,
                InspectionBudget(root, site.inspection_limit),
                api_key=os.environ.get("PAGESPEED_API_KEY"), previous=previous, user_agent=user_agent,
            )
            path = root / "data" / week_id(start) / f"tech_{site.id}.json"
            write_json(path, document)
            LOGGER.info("Generated %s; status %s", path, document["status"])
            paths.append(path)
    return paths


def main() -> int:
    """Run the technical stage with shared CLI and environment configuration."""
    setup_logging()
    args = parser(__doc__ or "Technical checks").parse_args()
    try:
        run(load_settings(args.config), week_start(args.week))
    except Exception as error:
        LOGGER.error("Technical stage failed (%s). Check configuration and network access.", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())