"""Wspólny pipeline analizy pojedynczego serwisu.

CLI wywołuje analyze_site() dla Site A i Site B – bez duplikacji logiki.
Pipeline jest identyczny jak w analizie jednodomenowej (Etap 1/1.5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

import pandas as pd

from analytics.categories import OTHER, assign_categories
from analytics.comparison import (
    aggregate_categories,
    build_category_tree,
    check_totals,
    compare_periods,
    coverage_report,
)
from analytics.ymyl import tag_ymyl
from config import SiteConfig
from services.gsc_service import get_gsc_data
from services.sitemap_service import get_category_urls


@dataclass
class Period:
    start: str
    end: str


@dataclass
class SiteAnalysis:
    site_key: str
    site_name: str
    gsc_property: str
    pages: pd.DataFrame
    categories: pd.DataFrame
    tree: pd.DataFrame
    coverage: dict
    sum_warnings: list[dict]
    other_urls: pd.DataFrame
    warnings: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return not self.pages.empty


def _validate_sitemap_hosts(site: SiteConfig) -> list[str]:
    """Ostrzega, gdy host sitemapy różni się od hosta base_url."""
    warnings: list[str] = []
    base_host = urlparse(site.base_url).netloc
    for sitemap in site.category_sitemaps:
        host = urlparse(sitemap).netloc
        if host and base_host and host != base_host:
            warnings.append(
                f"Sitemap '{sitemap}' ma inny host niż base_url "
                f"('{host}' != '{base_host}') dla serwisu {site.name}."
            )
    return warnings


def _collect_category_urls(site: SiteConfig) -> list[str]:
    """Pobiera i łączy URL-e kategorii ze wszystkich sitemap serwisu."""
    urls: list[str] = []
    for sitemap in site.category_sitemaps:
        urls.extend(get_category_urls(sitemap))
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def analyze_site(
    site_config: SiteConfig,
    current_period: Period,
    previous_period: Period,
    credentials_path: str,
    use_cache: bool = True,
) -> SiteAnalysis:
    """Uruchamia pełny pipeline analizy dla jednego serwisu i zwraca wyniki."""
    warnings = _validate_sitemap_hosts(site_config)

    print(f"\n[{site_config.name}] GSC – CURRENT PERIOD...")
    current_df = get_gsc_data(
        current_period.start,
        current_period.end,
        site_config.gsc_property,
        credentials_path,
        use_cache=use_cache,
    )
    print(f"[{site_config.name}] GSC – PREVIOUS PERIOD...")
    previous_df = get_gsc_data(
        previous_period.start,
        previous_period.end,
        site_config.gsc_property,
        credentials_path,
        use_cache=use_cache,
    )

    if current_df.empty and previous_df.empty:
        warnings.append(
            f"{site_config.name}: GSC nie zwróciło danych dla wybranych okresów."
        )

    print(f"[{site_config.name}] Pobieranie sitemap kategorii...")
    category_urls = _collect_category_urls(site_config)
    print(f"[{site_config.name}] Znaleziono {len(category_urls)} URL-i kategorii.")

    pages = compare_periods(current_df, previous_df)
    pages = assign_categories(pages, category_urls, url_column="page")
    categories = aggregate_categories(pages)
    tree = build_category_tree(pages)
    coverage = coverage_report(pages)
    sum_warnings = check_totals(pages)
    other_urls = pages[pages["category_path"] == OTHER].sort_values(
        "current_clicks", ascending=False
    )

    return SiteAnalysis(
        site_key=site_config.key,
        site_name=site_config.name,
        gsc_property=site_config.gsc_property,
        pages=pages,
        categories=categories,
        tree=tree,
        coverage=coverage,
        sum_warnings=sum_warnings,
        other_urls=other_urls,
        warnings=warnings,
    )


# --- analiza per źródło danych: Search (web) vs Discover -------------------


@dataclass
class SourceAnalysis:
    search_type: str  # "web", "discover" lub "news"
    pages: pd.DataFrame
    tree: pd.DataFrame  # drzewo kategorii z kolumną `ymyl`
    device: pd.DataFrame  # device, current_clicks, previous_clicks, clicks_change
    current_clicks: float
    previous_clicks: float
    daily: pd.DataFrame = field(default_factory=pd.DataFrame)  # day, Current, Previous


@dataclass
class SiteSources:
    site_key: str
    site_name: str
    gsc_property: str
    ymyl_paths: list[str]
    search: SourceAnalysis
    discover: SourceAnalysis
    warnings: list[str] = field(default_factory=list)


def _device_table(current_df: pd.DataFrame, previous_df: pd.DataFrame) -> pd.DataFrame:
    """Buduje rozbicie clicks po device dla jednego źródła."""
    def _agg(df: pd.DataFrame, name: str) -> pd.DataFrame:
        if df.empty or "device" not in df.columns:
            return pd.DataFrame({"device": [], name: []})
        return (
            df.groupby("device", as_index=False)["clicks"].sum().rename(
                columns={"clicks": name}
            )
        )

    current = _agg(current_df, "current_clicks")
    previous = _agg(previous_df, "previous_clicks")
    merged = pd.merge(current, previous, on="device", how="outer")
    for col in ("current_clicks", "previous_clicks"):
        if col not in merged.columns:
            merged[col] = 0
    merged = merged.fillna(0)
    merged["clicks_change"] = merged["current_clicks"] - merged["previous_clicks"]
    return merged.sort_values("current_clicks", ascending=False).reset_index(drop=True)


def _daily_series(
    current_df: pd.DataFrame, previous_df: pd.DataFrame
) -> pd.DataFrame:
    """Nakłada dzienny ruch obu okresów wg indeksu dnia (1..N): kolumny Current/Previous."""
    def _prep(df: pd.DataFrame, name: str) -> pd.DataFrame:
        if df.empty or "date" not in df.columns:
            return pd.DataFrame({"day": [], name: []})
        ordered = df.sort_values("date").reset_index(drop=True)
        ordered["day"] = ordered.index + 1
        return ordered[["day", "clicks"]].rename(columns={"clicks": name})

    merged = pd.merge(
        _prep(current_df, "Current"),
        _prep(previous_df, "Previous"),
        on="day",
        how="outer",
    ).sort_values("day")
    for col in ("Current", "Previous"):
        if col not in merged.columns:
            merged[col] = 0
    return merged.fillna(0).reset_index(drop=True)


def analyze_source(
    site_config: SiteConfig,
    current_period: Period,
    previous_period: Period,
    credentials_path: str,
    category_urls: list[str],
    search_type: str,
    use_cache: bool = True,
    with_timeseries: bool = False,
) -> SourceAnalysis:
    """Analizuje jedno źródło danych (web/discover/news) dla serwisu."""
    print(f"[{site_config.name}] GSC [{search_type}] – pages...")
    current_pages = get_gsc_data(
        current_period.start,
        current_period.end,
        site_config.gsc_property,
        credentials_path,
        dimensions=("page",),
        search_type=search_type,
        use_cache=use_cache,
    )
    previous_pages = get_gsc_data(
        previous_period.start,
        previous_period.end,
        site_config.gsc_property,
        credentials_path,
        dimensions=("page",),
        search_type=search_type,
        use_cache=use_cache,
    )

    print(f"[{site_config.name}] GSC [{search_type}] – device...")
    if search_type != "web":
        # Discover/News nie obsługują grupowania po device (API zwraca 400).
        device = pd.DataFrame(
            columns=["device", "current_clicks", "previous_clicks", "clicks_change"]
        )
    else:
        current_device = get_gsc_data(
            current_period.start,
            current_period.end,
            site_config.gsc_property,
            credentials_path,
            dimensions=("device",),
            search_type=search_type,
            use_cache=use_cache,
        )
        previous_device = get_gsc_data(
            previous_period.start,
            previous_period.end,
            site_config.gsc_property,
            credentials_path,
            dimensions=("device",),
            search_type=search_type,
            use_cache=use_cache,
        )
        device = _device_table(current_device, previous_device)

    pages = compare_periods(current_pages, previous_pages)
    pages = assign_categories(pages, category_urls, url_column="page")
    tree = tag_ymyl(
        build_category_tree(pages),
        site_config.ymyl_paths,
        site_config.ymyl_exclude_paths,
    )

    if with_timeseries:
        current_daily = get_gsc_data(
            current_period.start,
            current_period.end,
            site_config.gsc_property,
            credentials_path,
            dimensions=("date",),
            search_type=search_type,
            use_cache=use_cache,
        )
        previous_daily = get_gsc_data(
            previous_period.start,
            previous_period.end,
            site_config.gsc_property,
            credentials_path,
            dimensions=("date",),
            search_type=search_type,
            use_cache=use_cache,
        )
        daily = _daily_series(current_daily, previous_daily)
    else:
        daily = pd.DataFrame()

    return SourceAnalysis(
        search_type=search_type,
        pages=pages,
        tree=tree,
        device=device,
        current_clicks=float(pages["current_clicks"].sum()),
        previous_clicks=float(pages["previous_clicks"].sum()),
        daily=daily,
    )


def analyze_site_sources(
    site_config: SiteConfig,
    current_period: Period,
    previous_period: Period,
    credentials_path: str,
    use_cache: bool = True,
) -> SiteSources:
    """Analizuje serwis w rozbiciu na Search (web) i Discover, z YMYL i device."""
    warnings = _validate_sitemap_hosts(site_config)
    print(f"\n[{site_config.name}] Pobieranie sitemap kategorii...")
    category_urls = _collect_category_urls(site_config)
    print(f"[{site_config.name}] Znaleziono {len(category_urls)} URL-i kategorii.")

    search = analyze_source(
        site_config,
        current_period,
        previous_period,
        credentials_path,
        category_urls,
        "web",
        use_cache,
    )
    discover = analyze_source(
        site_config,
        current_period,
        previous_period,
        credentials_path,
        category_urls,
        "discover",
        use_cache,
    )

    if not site_config.ymyl_paths:
        warnings.append(
            f"{site_config.name}: brak kategorii YMYL – cały ruch liczony jako non-YMYL."
        )
    if search.pages.empty and discover.pages.empty:
        warnings.append(
            f"{site_config.name}: brak danych Search i Discover dla okresów."
        )

    return SiteSources(
        site_key=site_config.key,
        site_name=site_config.name,
        gsc_property=site_config.gsc_property,
        ymyl_paths=site_config.ymyl_paths,
        search=search,
        discover=discover,
        warnings=warnings,
    )


def analyze_site_multi_source(
    site_config: SiteConfig,
    current_period: Period,
    previous_period: Period,
    credentials_path: str,
    search_types: list[str],
    use_cache: bool = True,
) -> dict[str, SourceAnalysis]:
    """Analizuje jeden serwis dla wybranych źródeł (web/discover/news).

    Zwraca mapę search_type -> SourceAnalysis. Sitemapy pobierane raz i
    współdzielone między źródłami.
    """
    print(f"\n[{site_config.name}] Pobieranie sitemap kategorii...")
    category_urls = _collect_category_urls(site_config)
    print(f"[{site_config.name}] Znaleziono {len(category_urls)} URL-i kategorii.")

    results: dict[str, SourceAnalysis] = {}
    for search_type in search_types:
        results[search_type] = analyze_source(
            site_config,
            current_period,
            previous_period,
            credentials_path,
            category_urls,
            search_type,
            use_cache,
            with_timeseries=True,
        )
    return results


def build_url_metrics(
    urls: list[str],
    current_df: pd.DataFrame,
    previous_df: pd.DataFrame | None = None,
    with_position: bool = True,
) -> pd.DataFrame:
    """Zwraca metryki GSC per URL z listy (clicks/impressions/ctr/position).

    Dane pochodzą z wcześniej pobranego DataFrame `page`. URL-e spoza danych mają
    0 klików/wyświetleń i puste CTR/pozycję. Gdy podano previous_df, dolicza zmiany.
    """
    base = pd.DataFrame({"url": list(dict.fromkeys(u for u in urls if u))})

    def _prep(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
        renamed = df.rename(
            columns={
                "page": "url",
                "clicks": f"{prefix}_clicks",
                "impressions": f"{prefix}_impressions",
                "ctr": f"{prefix}_ctr",
                "position": f"{prefix}_position",
            }
        )
        cols = [
            "url",
            f"{prefix}_clicks",
            f"{prefix}_impressions",
            f"{prefix}_ctr",
            f"{prefix}_position",
        ]
        return renamed[[c for c in cols if c in renamed.columns]]

    result = base.merge(_prep(current_df, "current"), on="url", how="left")
    if previous_df is not None:
        result = result.merge(_prep(previous_df, "previous"), on="url", how="left")
        result["clicks_change"] = (
            result["current_clicks"].fillna(0) - result["previous_clicks"].fillna(0)
        )
        result["impressions_change"] = (
            result["current_impressions"].fillna(0)
            - result["previous_impressions"].fillna(0)
        )
        if with_position and {"current_position", "previous_position"} <= set(
            result.columns
        ):
            result["position_change"] = (
                result["current_position"] - result["previous_position"]
            )

    for col in result.columns:
        if "clicks" in col or "impressions" in col:
            result[col] = result[col].fillna(0)

    if not with_position:
        result = result.drop(
            columns=[c for c in result.columns if "position" in c], errors="ignore"
        )
    return result


