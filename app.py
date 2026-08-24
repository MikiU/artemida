"""GSC Analyzer – porównanie dwóch serwisów Google Search Console (ETAP 2).

Uruchomienie:
    python app.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd

from analysis import (
    Period,
    SiteAnalysis,
    SiteSources,
    SourceAnalysis,
    analyze_site,
    analyze_site_multi_source,
    analyze_site_sources,
)
from analytics.categories import HOMEPAGE, OTHER
from analytics.category_groups import build_group_comparison
from analytics.ymyl import ymyl_summary
from config import (
    CategoryGroup,
    ConfigError,
    GlobalConfig,
    SiteConfig,
    SitesConfig,
    load_global_config,
    load_sites,
)
from services.gsc_service import GSCError
from services.sitemap_service import SitemapError

OUTPUT_DIR = "output"


class DateInputError(Exception):
    """Błąd wprowadzonych dat."""


class SelectionError(Exception):
    """Błąd wyboru serwisu."""



def _parse_date(value: str) -> str:
    """Waliduje datę w formacie YYYY-MM-DD i zwraca ją jako tekst."""
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise DateInputError(
            f"Nieprawidłowa data '{value}'. Użyj formatu YYYY-MM-DD (np. 2026-01-31)."
        ) from exc
    return parsed.isoformat()


def _ask_period(name: str) -> tuple[str, str]:
    """Pyta użytkownika o start i koniec okresu, waliduje kolejność dat."""
    print(f"\n{name}")
    start = _parse_date(input("  Start date (YYYY-MM-DD): "))
    end = _parse_date(input("  End date   (YYYY-MM-DD): "))
    if start > end:
        raise DateInputError(
            f"{name}: start ({start}) jest późniejszy niż end ({end})."
        )
    return start, end


def _fmt_pct(value: float) -> str:
    """Formatuje wartość procentową (NaN jako '—')."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value:+.1f}%"


def _fmt_signed(value: float) -> str:
    """Formatuje liczbę całkowitą ze znakiem i spacją jako separatorem tysięcy."""
    return f"{int(value):+,}".replace(",", " ")


def _period_days(start: str, end: str) -> int:
    """Liczba dni w okresie, licząc start i end włącznie."""
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def _report_period_lengths(
    current_start: str,
    current_end: str,
    previous_start: str,
    previous_end: str,
) -> None:
    current_days = _period_days(current_start, current_end)
    previous_days = _period_days(previous_start, previous_end)
    print(f"\nCurrent period: {current_days} days")
    print(f"Previous period: {previous_days} days")
    if current_days != previous_days:
        print(
            "\nWARNING: periods have different lengths:\n"
            f"Current: {current_days} days\n"
            f"Previous: {previous_days} days\n"
            "Comparison may be biased."
        )




def _print_total(pages: pd.DataFrame) -> None:
    current = int(pages["current_clicks"].sum())
    previous = int(pages["previous_clicks"].sum())
    diff = current - previous
    pct = ((diff / previous) * 100.0) if previous else float("nan")

    print("\n" + "=" * 50)
    print("TOTAL")
    print("=" * 50)
    print(f"Current clicks:  {current}")
    print(f"Previous clicks: {previous}")
    print(f"Difference:      {diff:+d}")
    print(f"Change %:        {_fmt_pct(pct)}")


def _print_category_table(title: str, df: pd.DataFrame) -> None:
    print("\n" + "=" * 50)
    print(title)
    print("=" * 50)
    if df.empty:
        print("(brak danych)")
        return
    header = f"{'Category':<40} {'Current':>9} {'Previous':>9} {'Diff':>8} {'%':>9}"
    print(header)
    print("-" * len(header))
    for _, row in df.iterrows():
        print(
            f"{str(row['category_path'])[:40]:<40} "
            f"{int(row['current_clicks']):>9} "
            f"{int(row['previous_clicks']):>9} "
            f"{int(row['clicks_change']):>+8} "
            f"{_fmt_pct(row['clicks_change_pct']):>9}"
        )


def _print_url_table(title: str, df: pd.DataFrame) -> None:
    print("\n" + "=" * 50)
    print(title)
    print("=" * 50)
    if df.empty:
        print("(brak danych)")
        return
    for _, row in df.iterrows():
        print(
            f"{str(row['page'])}\n"
            f"    {str(row['category_path'])}  |  "
            f"current {int(row['current_clicks'])}, "
            f"previous {int(row['previous_clicks'])}, "
            f"diff {int(row['clicks_change']):+d}, "
            f"{_fmt_pct(row['clicks_change_pct'])}"
        )


def _print_coverage(report: dict) -> None:
    print("\n" + "=" * 50)
    print("CLASSIFICATION COVERAGE")
    print("=" * 50)
    print(f"Total URLs:                    {report['total_urls']}")
    print(f"  Assigned to categories:      {report['category_urls']}")
    print(f"  Homepage:                    {report['homepage_urls']}")
    print(f"  Other:                       {report['other_urls']}")
    print(f"\nTotal current clicks:          {int(report['total_current_clicks'])}")
    print(f"  Category clicks:             {int(report['category_current_clicks'])}")
    print(f"  Homepage clicks:             {int(report['homepage_current_clicks'])}")
    print(f"  Other clicks:                {int(report['other_current_clicks'])}")
    print(
        f"\nCategory coverage (current):   "
        f"{_fmt_pct_plain(report['coverage_current_pct'])}"
    )
    print(
        f"Category coverage (previous):  "
        f"{_fmt_pct_plain(report['coverage_previous_pct'])}"
    )
    _print_traffic_distribution("CURRENT", report, "current")
    _print_traffic_distribution("PREVIOUS", report, "previous")


def _fmt_clicks(value: float) -> str:
    """Liczba całkowita ze spacją jako separatorem tysięcy."""
    return f"{int(value):,}".replace(",", " ")


def _print_traffic_distribution(label: str, report: dict, period: str) -> None:
    homepage = report[f"homepage_{period}_clicks"]
    categories = report[f"category_{period}_clicks"]
    other = report[f"other_{period}_clicks"]
    print(f"\nTOTAL TRAFFIC DISTRIBUTION – {label}")
    print(
        f"  Homepage:    {_fmt_clicks(homepage):>12} | "
        f"{_fmt_pct_plain(report[f'homepage_{period}_pct'])}"
    )
    print(
        f"  Categories:  {_fmt_clicks(categories):>12} | "
        f"{_fmt_pct_plain(report[f'categories_{period}_pct'])}"
    )
    print(
        f"  Other:       {_fmt_clicks(other):>12} | "
        f"{_fmt_pct_plain(report[f'other_{period}_pct'])}"
    )
    print(
        f"  Content category coverage excluding Homepage: "
        f"{_fmt_pct_plain(report[f'content_coverage_{period}_pct'])}"
    )



def _fmt_pct_plain(value: float) -> str:
    """Procent bez znaku +, NaN jako '—'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value:.1f}%"


def _print_sum_control(warnings: list[dict]) -> None:
    print("\n" + "=" * 50)
    print("SUM CONTROL")
    print("=" * 50)
    if not warnings:
        print("OK – total = Homepage + Other + categories (dla wszystkich metryk).")
        return
    for w in warnings:
        print(
            f"WARNING [{w['metric']}]: total={w['total']:.0f}, "
            f"parts={w['parts']:.0f}, diff={w['diff']:+.0f}"
        )


def _print_category_tree(tree: pd.DataFrame, max_depth: int = 3, max_children: int = 10) -> None:
    print("\n" + "=" * 50)
    print("CATEGORY TREE")
    print("=" * 50)
    if tree.empty:
        print("(brak danych)")
        return

    by_path = {row["category_path"]: row for _, row in tree.iterrows()}

    def _children(parent_path: str | None, depth: int) -> list[str]:
        result = []
        for path, row in by_path.items():
            if row["depth"] != depth:
                continue
            if depth == 1:
                if parent_path is None:
                    result.append(path)
            elif path.startswith(f"{parent_path} > "):
                # bezpośrednie dziecko: dokładnie jeden segment więcej
                if path.count(" > ") == depth - 1:
                    result.append(path)
        result.sort(key=lambda p: abs(by_path[p]["clicks_change"]), reverse=True)
        return result[:max_children]

    def _walk(path: str, depth: int) -> None:
        row = by_path[path]
        leaf = path.split(" > ")[-1]
        indent = "    " * (depth - 1)
        print(f"{indent}{leaf} {_fmt_signed(row['clicks_change'])}")
        if depth >= max_depth:
            return
        for child in _children(path, depth + 1):
            _walk(child, depth + 1)

    for top in _children(None, 1):
        print()
        _walk(top, 1)


def _print_site_report(analysis: SiteAnalysis) -> None:
    pages = analysis.pages
    tree = analysis.tree
    print("\n" + "#" * 50)
    print(f"# SITE REPORT: {analysis.site_name}  ({analysis.gsc_property})")
    print("#" * 50)

    _print_total(pages)
    _print_coverage(analysis.coverage)
    _print_sum_control(analysis.sum_warnings)

    top_level = tree[tree["depth"] == 1] if not tree.empty else tree
    declining_cat = top_level[top_level["clicks_change"] < 0].sort_values(
        "clicks_change", ascending=True
    ).head(10)
    growing_cat = top_level[top_level["clicks_change"] > 0].sort_values(
        "clicks_change", ascending=False
    ).head(10)

    _print_category_table("TOP 10 DECLINING CATEGORIES (depth=1)", declining_cat)
    _print_category_table("TOP 10 GROWING CATEGORIES (depth=1)", growing_cat)

    _print_direct_children(top_level)
    _print_category_tree(tree)

    declining_urls = pages[pages["clicks_change"] < 0].sort_values(
        "clicks_change", ascending=True
    ).head(10)
    growing_urls = pages[pages["clicks_change"] > 0].sort_values(
        "clicks_change", ascending=False
    ).head(10)

    _print_url_table(f"{analysis.site_name} – TOP 10 DECLINING URLs", declining_urls)
    _print_url_table(f"{analysis.site_name} – TOP 10 GROWING URLs", growing_urls)

    _print_other_urls(analysis.other_urls.head(20))


def _print_direct_children(top_level: pd.DataFrame) -> None:
    print("\n" + "=" * 50)
    print("DIRECT vs CHILDREN TRAFFIC (TOP 10 depth=1)")
    print("=" * 50)
    if top_level.empty:
        print("(brak danych)")
        return
    ranked = top_level.reindex(
        top_level["clicks_change"].abs().sort_values(ascending=False).index
    ).head(10)
    for _, row in ranked.iterrows():
        print(f"\n{row['category_path']}")
        print(f"  DIRECT change:   {_fmt_signed(row['direct_clicks_change'])}")
        print(f"  CHILDREN change: {_fmt_signed(row['children_clicks_change'])}")
        print(f"  TOTAL change:    {_fmt_signed(row['total_clicks_change'])}")


def _print_other_urls(df: pd.DataFrame) -> None:
    print("\n" + "=" * 50)
    print("TOP 20 URLs IN OTHER")
    print("=" * 50)
    if df.empty:
        print("(brak danych)")
        return
    for _, row in df.iterrows():
        print(
            f"{str(row['page'])}\n"
            f"    current {int(row['current_clicks'])}, "
            f"previous {int(row['previous_clicks'])}, "
            f"diff {int(row['clicks_change']):+d}"
        )


def _save_site_csv(analysis: SiteAnalysis, site_dir: str) -> None:
    os.makedirs(site_dir, exist_ok=True)
    analysis.pages.to_csv(
        os.path.join(site_dir, "pages_comparison.csv"), index=False, encoding="utf-8"
    )
    analysis.categories.to_csv(
        os.path.join(site_dir, "categories_comparison.csv"),
        index=False,
        encoding="utf-8",
    )
    analysis.tree.to_csv(
        os.path.join(site_dir, "category_tree_comparison.csv"),
        index=False,
        encoding="utf-8",
    )
    analysis.other_urls.to_csv(
        os.path.join(site_dir, "other_urls.csv"), index=False, encoding="utf-8"
    )


# --- wybór serwisów -------------------------------------------------------


def _choose_index(prompt: str, count: int, exclude: int | None = None) -> int:
    """Czyta numer serwisu (1-based) i zwraca indeks 0-based."""
    for _ in range(5):
        raw = input(prompt).strip()
        if not raw.isdigit():
            print("  Podaj numer z listy.")
            continue
        idx = int(raw) - 1
        if idx < 0 or idx >= count:
            print(f"  Numer poza zakresem (1-{count}).")
            continue
        if exclude is not None and idx == exclude:
            print("  Nie możesz wybrać tego samego serwisu dwa razy.")
            continue
        return idx
    raise SelectionError("Zbyt wiele nieprawidłowych prób wyboru serwisu.")


def _choose_sites(sites: dict[str, SiteConfig]) -> tuple[SiteConfig, SiteConfig]:
    keys = list(sites.keys())
    print("\n" + "=" * 50)
    print("AVAILABLE SITES")
    print("=" * 50)
    for i, key in enumerate(keys, 1):
        print(f"{i}. {sites[key].name}")
    print("\nChoose sites to compare.")
    first = _choose_index("First site: ", len(keys))
    second = _choose_index("Second site: ", len(keys), exclude=first)
    return sites[keys[first]], sites[keys[second]]


# --- porównanie cross-site ------------------------------------------------


def _site_totals(analysis: SiteAnalysis) -> dict:
    pages = analysis.pages
    cov = analysis.coverage
    current = float(pages["current_clicks"].sum())
    previous = float(pages["previous_clicks"].sum())
    current_impr = float(pages["current_impressions"].sum())
    previous_impr = float(pages["previous_impressions"].sum())
    return {
        "site_key": analysis.site_key,
        "site_name": analysis.site_name,
        "gsc_property": analysis.gsc_property,
        "current_clicks": current,
        "previous_clicks": previous,
        "clicks_change": current - previous,
        "clicks_change_pct": _pct(current, previous),
        "current_impressions": current_impr,
        "previous_impressions": previous_impr,
        "impressions_change": current_impr - previous_impr,
        "impressions_change_pct": _pct(current_impr, previous_impr),
        "homepage_current_clicks": cov["homepage_current_clicks"],
        "homepage_previous_clicks": cov["homepage_previous_clicks"],
        "homepage_change": cov["homepage_current_clicks"]
        - cov["homepage_previous_clicks"],
        "category_current_clicks": cov["category_current_clicks"],
        "category_previous_clicks": cov["category_previous_clicks"],
        "category_change": cov["category_current_clicks"]
        - cov["category_previous_clicks"],
        "other_current_clicks": cov["other_current_clicks"],
        "other_previous_clicks": cov["other_previous_clicks"],
        "other_change": cov["other_current_clicks"] - cov["other_previous_clicks"],
        "content_coverage_current": cov["content_coverage_current_pct"],
        "content_coverage_previous": cov["content_coverage_previous_pct"],
    }


def _pct(current: float, previous: float) -> float:
    return ((current - previous) / previous * 100.0) if previous else float("nan")


def _print_site_comparison(a: dict, b: dict) -> None:
    print("\n" + "=" * 66)
    print("SITE COMPARISON")
    print("=" * 66)
    name_a = a["site_name"][:20]
    name_b = b["site_name"][:20]
    print(f"{'':24}{name_a:>20}{name_b:>22}")

    def row(label: str, va, vb, kind: str = "int") -> None:
        if kind == "int":
            fa, fb = _fmt_clicks(va), _fmt_clicks(vb)
        elif kind == "signed":
            fa, fb = _fmt_signed(va), _fmt_signed(vb)
        else:
            fa, fb = _fmt_pct_plain(va), _fmt_pct_plain(vb)
        print(f"{label:24}{fa:>20}{fb:>22}")

    row("Current clicks", a["current_clicks"], b["current_clicks"])
    row("Previous clicks", a["previous_clicks"], b["previous_clicks"])
    row("Clicks change", a["clicks_change"], b["clicks_change"], "signed")
    row("Clicks change %", a["clicks_change_pct"], b["clicks_change_pct"], "pct")
    print()
    row("Current impressions", a["current_impressions"], b["current_impressions"])
    row("Previous impressions", a["previous_impressions"], b["previous_impressions"])
    row("Impressions change", a["impressions_change"], b["impressions_change"], "signed")
    row(
        "Impressions change %",
        a["impressions_change_pct"],
        b["impressions_change_pct"],
        "pct",
    )
    print()
    row("Homepage change", a["homepage_change"], b["homepage_change"], "signed")
    row("Category traffic change", a["category_change"], b["category_change"], "signed")
    row("Other change", a["other_change"], b["other_change"], "signed")
    print()
    row("Content coverage", a["content_coverage_current"], b["content_coverage_current"], "pct")


def _print_performance_difference(a: dict, b: dict) -> None:
    a_pct = a["clicks_change_pct"]
    b_pct = b["clicks_change_pct"]
    print("\n" + "=" * 66)
    print("SEO PERFORMANCE DIFFERENCE")
    print("=" * 66)
    print(f"{a['site_name']}: {_fmt_pct(a_pct)}")
    print(f"{b['site_name']}: {_fmt_pct(b_pct)}")
    if pd.isna(a_pct) or pd.isna(b_pct):
        print("difference: — (brak danych)")
        return
    diff = a_pct - b_pct
    print(f"\ndifference:\n{abs(diff):.1f} percentage points")
    if diff >= 0:
        winner, loser = a["site_name"], b["site_name"]
    else:
        winner, loser = b["site_name"], a["site_name"]
    print(
        f"\nSEO PERFORMANCE DIFFERENCE:\n"
        f"{winner} outperformed {loser} by {abs(diff):.1f} pp."
    )
    print(
        "(To jest tylko różnica dynamiki w badanych okresach, nie wskazanie przyczyny.)"
    )


def _print_top_level_categories(analysis: SiteAnalysis) -> None:
    print("\n" + "=" * 50)
    print(f"TOP LEVEL CATEGORIES – {analysis.site_name}")
    print("=" * 50)
    tree = analysis.tree
    if tree.empty:
        print("(brak danych)")
        return
    top = tree[tree["depth"] == 1].reindex(
        tree[tree["depth"] == 1]["clicks_change"].abs().sort_values(ascending=False).index
    ).head(10)
    for _, row in top.iterrows():
        print(
            f"{str(row['category_path'])[:24]:<24} "
            f"{_fmt_signed(row['clicks_change']):>12}  "
            f"{_fmt_pct(row['clicks_change_pct']):>8}"
        )


def _print_group_comparisons(groups: list[dict]) -> None:
    if not groups:
        return
    print("\n" + "=" * 66)
    print("CATEGORY GROUP COMPARISON")
    print("=" * 66)
    for g in groups:
        print(f"\n{g['group_label'].upper()}")
        print(f"{'':22}{g['site_a_name'][:18]:>18}{g['site_b_name'][:18]:>20}")
        print(
            f"{'Current clicks':22}"
            f"{_fmt_clicks(g['site_a_current_clicks']):>18}"
            f"{_fmt_clicks(g['site_b_current_clicks']):>20}"
        )
        print(
            f"{'Previous clicks':22}"
            f"{_fmt_clicks(g['site_a_previous_clicks']):>18}"
            f"{_fmt_clicks(g['site_b_previous_clicks']):>20}"
        )
        print(
            f"{'Change':22}"
            f"{_fmt_signed(g['site_a_change']):>18}"
            f"{_fmt_signed(g['site_b_change']):>20}"
        )
        print(
            f"{'Change %':22}"
            f"{_fmt_pct(g['site_a_change_pct']):>18}"
            f"{_fmt_pct(g['site_b_change_pct']):>20}"
        )
        print(f"Difference growth: {_fmt_pp(g['growth_difference_pp'])}")
        print(f"direction_pattern: {g['direction_pattern']}")
        for missing in g["site_a_missing_paths"]:
            print(f'WARNING: Category path "{missing}" not found for {g["site_a_name"]}.')
        for missing in g["site_b_missing_paths"]:
            print(f'WARNING: Category path "{missing}" not found for {g["site_b_name"]}.')


def _print_opposite_directions(groups: list[dict]) -> None:
    opposite = [
        g
        for g in groups
        if g["direction_pattern"]
        in ("site_a_growing_site_b_declining", "site_a_declining_site_b_growing")
    ]
    if not opposite:
        return
    print("\n" + "=" * 66)
    print("OPPOSITE DIRECTION")
    print("=" * 66)
    for g in opposite:
        print(f"\n{g['group_label']}:")
        print(f"{g['site_a_name']} {_fmt_pct(g['site_a_change_pct'])}")
        print(f"{g['site_b_name']} {_fmt_pct(g['site_b_change_pct'])}")


def _print_biggest_differences(groups: list[dict]) -> None:
    if not groups:
        return
    ranked = sorted(
        groups,
        key=lambda g: abs(g["growth_difference_pp"])
        if not pd.isna(g["growth_difference_pp"])
        else -1,
        reverse=True,
    )
    print("\n" + "=" * 66)
    print("BIGGEST DIFFERENCES BETWEEN SITES")
    print("=" * 66)
    for i, g in enumerate(ranked, 1):
        print(f"\n{i}. {g['group_label']}")
        print(f"   {g['site_a_name']}: {_fmt_pct(g['site_a_change_pct'])}")
        print(f"   {g['site_b_name']}: {_fmt_pct(g['site_b_change_pct'])}")
        print(f"   Spread: {_fmt_pp(g['growth_difference_pp'])}")


def _print_group_drilldowns(
    groups_cfg: dict[str, CategoryGroup],
    selected_groups: list[dict],
    a: SiteAnalysis,
    b: SiteAnalysis,
) -> None:
    if not selected_groups:
        return
    for g in selected_groups:
        cfg = groups_cfg[g["group_key"]]
        print("\n" + "=" * 66)
        print(f"CATEGORY GROUP: {g['group_label']}")
        print("=" * 66)
        print(f"\n{a.site_name}:")
        _print_subtree(a.tree, cfg.site_paths.get(a.site_key, []))
        print(f"\n{b.site_name}:")
        _print_subtree(b.tree, cfg.site_paths.get(b.site_key, []))


def _print_subtree(tree: pd.DataFrame, roots: list[str]) -> None:
    """Wypisuje poddrzewa dla wskazanych root category_path."""
    if tree.empty or not roots:
        print("  (brak danych)")
        return
    mask = pd.Series(False, index=tree.index)
    for root in roots:
        mask |= (tree["category_path"] == root) | tree["category_path"].str.startswith(
            f"{root} > "
        )
    subtree = tree[mask]
    if subtree.empty:
        print("  (brak dopasowanych kategorii)")
        return
    _print_category_tree(subtree)


def _fmt_pp(value: float) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "— pp"
    return f"{value:+.1f} pp"


# --- budowa porównań grup i eksport --------------------------------------


def _build_group_comparisons(
    sites_config: SitesConfig, a: SiteAnalysis, b: SiteAnalysis
) -> list[dict]:
    """Buduje porównania tylko dla grup skonfigurowanych dla OBU serwisów."""
    result: list[dict] = []
    for group in sites_config.category_groups.values():
        paths_a = group.site_paths.get(a.site_key)
        paths_b = group.site_paths.get(b.site_key)
        if not paths_a or not paths_b:
            continue
        result.append(
            build_group_comparison(
                group.key,
                group.label,
                a.site_key,
                a.site_name,
                a.tree,
                paths_a,
                b.site_key,
                b.site_name,
                b.tree,
                paths_b,
            )
        )
    return result


def _site_comparison_df(a: dict, b: dict) -> pd.DataFrame:
    columns = [
        "site_key",
        "site_name",
        "gsc_property",
        "current_clicks",
        "previous_clicks",
        "clicks_change",
        "clicks_change_pct",
        "current_impressions",
        "previous_impressions",
        "impressions_change",
        "impressions_change_pct",
        "homepage_current_clicks",
        "homepage_previous_clicks",
        "homepage_change",
        "category_current_clicks",
        "category_previous_clicks",
        "category_change",
        "other_current_clicks",
        "other_previous_clicks",
        "other_change",
        "content_coverage_current",
        "content_coverage_previous",
    ]
    return pd.DataFrame([a, b], columns=columns)


def _group_comparison_df(groups: list[dict]) -> pd.DataFrame:
    columns = [
        "group_key",
        "group_label",
        "site_a_name",
        "site_a_current_clicks",
        "site_a_previous_clicks",
        "site_a_change",
        "site_a_change_pct",
        "site_b_name",
        "site_b_current_clicks",
        "site_b_previous_clicks",
        "site_b_change",
        "site_b_change_pct",
        "growth_difference_pp",
        "direction_pattern",
    ]
    rows = [{col: g[col] for col in columns} for g in groups]
    return pd.DataFrame(rows, columns=columns)


class _Tee(io.StringIO):
    """Zapisuje jednocześnie do konsoli i do wewnętrznego bufora (na summary.txt)."""

    def __init__(self, stream):
        super().__init__()
        self._stream = stream

    def write(self, text):
        self._stream.write(text)
        return super().write(text)


def _make_run_dir(a: SiteConfig, b: SiteConfig) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    name = f"{stamp}_{a.key}_vs_{b.key}"
    run_dir = os.path.join(OUTPUT_DIR, name)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def run(global_config: GlobalConfig, sites_config: SitesConfig) -> None:
    mode = _choose_mode()
    if mode == 1:
        site_a, site_b = _choose_sites(sites_config.sites)
        print(f"\nComparing:\n{site_a.name}\nvs\n{site_b.name}")
        _run_pairwise(global_config, sites_config, site_a, site_b)
    elif mode == 2:
        sites = _choose_multiple_sites(sites_config.sites)
        names = ", ".join(s.name for s in sites)
        print(f"\nRanking kategorii dla: {names}")
        _run_multi_ranking(global_config, sites)
    elif mode == 3:
        sites = _choose_multiple_sites(sites_config.sites)
        names = ", ".join(s.name for s in sites)
        print(f"\nRaport Search/Discover/YMYL dla: {names}")
        _run_sources_report(global_config, sites)
    else:
        site = _choose_single_site(sites_config.sites)
        print(f"\nAnaliza serwisu: {site.name}")
        _run_single_site_sources(global_config, site)


def _choose_mode() -> int:
    print("\n" + "=" * 50)
    print("MODE")
    print("=" * 50)
    print("1. Porównaj DWA serwisy (cross-site + grupy kategorii)")
    print("2. Ranking wzrostów/spadków kategorii dla wielu serwisów (2+)")
    print("3. Search vs Discover + device + YMYL (wiele serwisów)")
    print("4. Jeden serwis po kategoriach: Search/Discover/News + presety dat")
    for _ in range(5):
        raw = input("Wybierz tryb (1/2/3/4): ").strip()
        if raw in ("1", "2", "3", "4"):
            return int(raw)
        print("  Podaj 1, 2, 3 albo 4.")
    raise SelectionError("Zbyt wiele nieprawidłowych prób wyboru trybu.")


def _choose_multiple_sites(sites: dict[str, SiteConfig]) -> list[SiteConfig]:
    keys = list(sites.keys())
    print("\n" + "=" * 50)
    print("AVAILABLE SITES")
    print("=" * 50)
    for i, key in enumerate(keys, 1):
        print(f"{i}. {sites[key].name}")
    print("\nWybierz serwisy do rankingu (numery po przecinku, min. 2).")
    for _ in range(5):
        raw = input("Sites (np. 1,2,3): ").strip()
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts or not all(p.isdigit() for p in parts):
            print("  Podaj numery oddzielone przecinkami, np. 1,2,3.")
            continue
        idxs = [int(p) - 1 for p in parts]
        if any(i < 0 or i >= len(keys) for i in idxs):
            print(f"  Numery poza zakresem (1-{len(keys)}).")
            continue
        unique = list(dict.fromkeys(idxs))
        if len(unique) < 2:
            print("  Wybierz co najmniej dwa różne serwisy.")
            continue
        return [sites[keys[i]] for i in unique]
    raise SelectionError("Zbyt wiele nieprawidłowych prób wyboru serwisów.")


# --- tryb: jeden serwis po kategoriach (Search/Discover/News + presety) ----

# Opóźnienie danych GSC – „ostatni" okres kończymy 3 dni wstecz.
GSC_LAG_DAYS = 3


def _choose_single_site(sites: dict[str, SiteConfig]) -> SiteConfig:
    keys = list(sites.keys())
    print("\n" + "=" * 50)
    print("AVAILABLE SITES")
    print("=" * 50)
    for i, key in enumerate(keys, 1):
        print(f"{i}. {sites[key].name}")
    idx = _choose_index("\nWybierz serwis: ", len(keys))
    return sites[keys[idx]]


def _choose_period_preset() -> int:
    print("\n" + "=" * 50)
    print("OKRES")
    print("=" * 50)
    print("1. Tydzień do tygodnia (7 vs poprzednie 7 dni)")
    print("2. Dwa tygodnie do dwóch tygodni (14 vs 14)")
    print("3. Miesiąc do miesiąca (30 vs poprzednie 30 dni)")
    print("4. Tydzień rok do roku (7 dni vs ten sam tydzień 52 tyg. temu)")
    print("5. Miesiąc rok do roku (30 dni vs 30 dni 52 tyg. temu)")
    print("6. Miesiąc kalendarzowy (ostatni pełny vs poprzedni)")
    print("7. Własne daty")
    for _ in range(5):
        raw = input("Wybierz okres (1-7): ").strip()
        if raw in {"1", "2", "3", "4", "5", "6", "7"}:
            return int(raw)
        print("  Podaj liczbę 1-7.")
    raise SelectionError("Zbyt wiele nieprawidłowych prób wyboru okresu.")


def _compute_preset_periods(
    preset: int, today: date | None = None
) -> tuple[Period, Period]:
    """Zwraca (current, previous) na podstawie presetu. `today` dla testowalności."""
    today = today or date.today()
    end_ref = today - timedelta(days=GSC_LAG_DAYS)

    def _period(start: date, end: date) -> Period:
        return Period(start.isoformat(), end.isoformat())

    if preset in (1, 2, 3):
        length = {1: 7, 2: 14, 3: 30}[preset]
        cur_end = end_ref
        cur_start = cur_end - timedelta(days=length - 1)
        prev_end = cur_start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=length - 1)
        return _period(cur_start, cur_end), _period(prev_start, prev_end)

    if preset in (4, 5):
        length = 7 if preset == 4 else 30
        cur_end = end_ref
        cur_start = cur_end - timedelta(days=length - 1)
        prev_start = cur_start - timedelta(weeks=52)
        prev_end = cur_end - timedelta(weeks=52)
        return _period(cur_start, cur_end), _period(prev_start, prev_end)

    # preset 6: miesiąc kalendarzowy
    first_this_month = today.replace(day=1)
    cur_end = first_this_month - timedelta(days=1)
    cur_start = cur_end.replace(day=1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    return _period(cur_start, cur_end), _period(prev_start, prev_end)


def _choose_sources() -> list[str]:
    print("\nŹródła danych: 1=Search  2=Discover  3=News")
    for _ in range(5):
        raw = input("Wybierz źródła (np. 1,2,3) [Enter = wszystkie]: ").strip()
        if not raw:
            return ["web", "discover", "news"]
        mapping = {"1": "web", "2": "discover", "3": "news"}
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if all(p in mapping for p in parts) and parts:
            return list(dict.fromkeys(mapping[p] for p in parts))
        print("  Podaj numery 1-3 po przecinku albo Enter.")
    raise SelectionError("Zbyt wiele nieprawidłowych prób wyboru źródeł.")


def _print_source_category_report(label: str, analysis: SourceAnalysis) -> None:
    print("\n" + "#" * 66)
    print(f"# ŹRÓDŁO: {label}")
    print("#" * 66)

    pages = analysis.pages
    current = int(pages["current_clicks"].sum()) if not pages.empty else 0
    previous = int(pages["previous_clicks"].sum()) if not pages.empty else 0
    diff = current - previous
    pct = ((diff / previous) * 100.0) if previous else float("nan")
    print(f"\nTOTAL clicks:  current {current}  previous {previous}  "
          f"diff {diff:+d}  {_fmt_pct(pct)}")

    tree = analysis.tree
    if tree.empty:
        print("(brak danych kategorii)")
        return

    growing = tree[tree["clicks_change"] > 0].sort_values(
        "clicks_change", ascending=False
    ).head(15)
    declining = tree[tree["clicks_change"] < 0].sort_values("clicks_change").head(15)
    _print_category_metric_table(f"{label} – TOP rosnące kategorie", growing)
    _print_category_metric_table(f"{label} – TOP spadające kategorie", declining)


def _print_category_metric_table(title: str, df: pd.DataFrame) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    if df.empty:
        print("(brak danych)")
        return
    header = (
        f"{'Category':<34}{'Diff':>10}{'%':>8}{'Current':>10}{'Prev':>10}{'URLs':>7}"
    )
    print(header)
    print("-" * len(header))
    for _, row in df.iterrows():
        cat = f"{row['category_path']} (d{int(row['depth'])})"
        print(
            f"{cat[:34]:<34}"
            f"{_fmt_signed(row['clicks_change']):>10}"
            f"{_fmt_pct(row['clicks_change_pct']):>8}"
            f"{int(row['current_clicks']):>10}"
            f"{int(row['previous_clicks']):>10}"
            f"{int(row['number_of_urls']):>7}"
        )


def _run_single_site_sources(
    global_config: GlobalConfig, site: SiteConfig
) -> None:
    preset = _choose_period_preset()
    if preset == 7:
        current_start, current_end = _ask_period("CURRENT PERIOD")
        previous_start, previous_end = _ask_period("PREVIOUS PERIOD")
        current = Period(current_start, current_end)
        previous = Period(previous_start, previous_end)
    else:
        current, previous = _compute_preset_periods(preset)

    print(f"\nCURRENT:  {current.start} … {current.end}")
    print(f"PREVIOUS: {previous.start} … {previous.end}")
    _report_period_lengths(current.start, current.end, previous.start, previous.end)

    sources = _choose_sources()
    try:
        results = analyze_site_multi_source(
            site, current, previous, global_config.google_credentials_path, sources
        )
    except (GSCError, SitemapError) as exc:
        print(f"\n[Błąd] {site.name}: {exc}")
        return

    buffer = _Tee(sys.stdout)
    with contextlib.redirect_stdout(buffer):
        print("\n" + "=" * 66)
        print(f"ANALIZA SERWISU: {site.name} ({site.gsc_property})")
        print(f"CURRENT {current.start}…{current.end}  vs  "
              f"PREVIOUS {previous.start}…{previous.end}")
        print("=" * 66)
        for search_type in sources:
            _print_source_category_report(SOURCE_LABELS[search_type], results[search_type])
    summary_text = buffer.getvalue()

    _export_single_site_sources(site, current, results, summary_text)


def _export_single_site_sources(
    site: SiteConfig,
    current: Period,
    results: dict[str, SourceAnalysis],
    summary_text: str,
) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    run_dir = os.path.join(OUTPUT_DIR, f"{stamp}_{site.key}_categories")
    os.makedirs(run_dir, exist_ok=True)
    for search_type, analysis in results.items():
        label = SOURCE_LABELS[search_type]
        analysis.tree.to_csv(
            os.path.join(run_dir, f"{search_type}_category_tree.csv"),
            index=False,
            encoding="utf-8",
        )
        analysis.pages.to_csv(
            os.path.join(run_dir, f"{search_type}_pages.csv"),
            index=False,
            encoding="utf-8",
        )
    with open(os.path.join(run_dir, "summary.txt"), "w", encoding="utf-8") as handle:
        handle.write(summary_text)
    print(f"\nZapisano wyniki do katalogu:\n  {run_dir}")


def _run_pairwise(
    global_config: GlobalConfig,
    sites_config: SitesConfig,
    site_a: SiteConfig,
    site_b: SiteConfig,
) -> None:
    print("\nPodaj wspólne okresy (daty w formacie YYYY-MM-DD).")
    current_start, current_end = _ask_period("CURRENT PERIOD")
    previous_start, previous_end = _ask_period("PREVIOUS PERIOD")
    _report_period_lengths(current_start, current_end, previous_start, previous_end)

    current = Period(current_start, current_end)
    previous = Period(previous_start, previous_end)
    credentials = global_config.google_credentials_path

    analysis_a = analyze_site(site_a, current, previous, credentials)
    analysis_b = analyze_site(site_b, current, previous, credentials)

    groups = _build_group_comparisons(sites_config, analysis_a, analysis_b)
    totals_a = _site_totals(analysis_a)
    totals_b = _site_totals(analysis_b)

    # Raport przechwytywany równolegle do summary.txt.
    buffer = _Tee(sys.stdout)
    with contextlib.redirect_stdout(buffer):
        _print_run_warnings(analysis_a, analysis_b)
        _print_site_comparison(totals_a, totals_b)
        _print_performance_difference(totals_a, totals_b)
        _print_top_level_categories(analysis_a)
        _print_top_level_categories(analysis_b)
        _print_group_comparisons(groups)
        _print_opposite_directions(groups)
        _print_biggest_differences(groups)
        _print_group_drilldowns(sites_config.category_groups, groups, analysis_a, analysis_b)
        _print_site_report(analysis_a)
        _print_site_report(analysis_b)
    summary_text = buffer.getvalue()

    _export(
        site_a,
        site_b,
        analysis_a,
        analysis_b,
        totals_a,
        totals_b,
        groups,
        summary_text,
    )


def _run_multi_ranking(
    global_config: GlobalConfig, sites: list[SiteConfig]
) -> None:
    print("\nPodaj wspólne okresy (daty w formacie YYYY-MM-DD).")
    current_start, current_end = _ask_period("CURRENT PERIOD")
    previous_start, previous_end = _ask_period("PREVIOUS PERIOD")
    _report_period_lengths(current_start, current_end, previous_start, previous_end)

    current = Period(current_start, current_end)
    previous = Period(previous_start, previous_end)
    credentials = global_config.google_credentials_path

    analyses = []
    for s in sites:
        try:
            analyses.append(analyze_site(s, current, previous, credentials))
        except (GSCError, SitemapError) as exc:
            print(f"\n[POMINIĘTO] {s.name}: {exc}")
    if not analyses:
        print("\nŻaden serwis nie zwrócił danych – nic do pokazania.")
        return
    combined = _combined_categories_df(analyses)

    buffer = _Tee(sys.stdout)
    with contextlib.redirect_stdout(buffer):
        _print_multi_warnings(analyses)
        _print_multi_totals(analyses)
        _print_combined_ranking(
            combined, "TOP 20 GROWING CATEGORIES (all sites)", ascending=False
        )
        _print_combined_ranking(
            combined, "TOP 20 DECLINING CATEGORIES (all sites)", ascending=True
        )
    summary_text = buffer.getvalue()

    _export_multi(sites, analyses, combined, summary_text)


def _combined_categories_df(analyses: list[SiteAnalysis]) -> pd.DataFrame:
    """Łączy drzewa kategorii wszystkich serwisów w jedną tabelę (wszystkie poziomy)."""
    columns = [
        "site_key",
        "site_name",
        "category_path",
        "depth",
        "current_clicks",
        "previous_clicks",
        "clicks_change",
        "clicks_change_pct",
        "current_impressions",
        "previous_impressions",
        "impressions_change",
        "impressions_change_pct",
        "number_of_urls",
    ]
    frames = []
    for analysis in analyses:
        tree = analysis.tree
        if tree.empty:
            continue
        frame = tree.copy()
        frame["site_key"] = analysis.site_key
        frame["site_name"] = analysis.site_name
        frames.append(frame[columns])
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


def _print_multi_warnings(analyses: list[SiteAnalysis]) -> None:
    all_warnings = [w for a in analyses for w in a.warnings]
    if not all_warnings:
        return
    print("\n" + "=" * 50)
    print("VALIDATION WARNINGS")
    print("=" * 50)
    for warning in all_warnings:
        print(f"WARNING: {warning}")


def _print_multi_totals(analyses: list[SiteAnalysis]) -> None:
    print("\n" + "=" * 66)
    print("SITES OVERVIEW")
    print("=" * 66)
    print(f"{'Site':24}{'Current':>14}{'Previous':>14}{'Change':>14}")
    for analysis in analyses:
        pages = analysis.pages
        current = float(pages["current_clicks"].sum())
        previous = float(pages["previous_clicks"].sum())
        print(
            f"{analysis.site_name[:24]:24}"
            f"{_fmt_clicks(current):>14}"
            f"{_fmt_clicks(previous):>14}"
            f"{_fmt_signed(current - previous):>14}"
        )


def _print_combined_ranking(
    combined: pd.DataFrame, title: str, ascending: bool
) -> None:
    print("\n" + "=" * 66)
    print(title)
    print("=" * 66)
    if combined.empty:
        print("(brak danych)")
        return
    if ascending:
        subset = combined[combined["clicks_change"] < 0]
    else:
        subset = combined[combined["clicks_change"] > 0]
    ranked = subset.sort_values("clicks_change", ascending=ascending).head(20)
    if ranked.empty:
        print("(brak danych)")
        return
    header = f"{'Site':16}{'Category':<34}{'Diff':>10}{'%':>9}"
    print(header)
    print("-" * len(header))
    for _, row in ranked.iterrows():
        label = f"{row['category_path']} (d{int(row['depth'])})"
        print(
            f"{str(row['site_name'])[:16]:16}"
            f"{label[:34]:<34}"
            f"{_fmt_signed(row['clicks_change']):>10}"
            f"{_fmt_pct(row['clicks_change_pct']):>9}"
        )


def _make_multi_run_dir(sites: list[SiteConfig]) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    keys = "_".join(s.key for s in sites)
    name = f"{stamp}_multi_{keys}"[:120]
    run_dir = os.path.join(OUTPUT_DIR, name)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def _export_multi(
    sites: list[SiteConfig],
    analyses: list[SiteAnalysis],
    combined: pd.DataFrame,
    summary_text: str,
) -> None:
    run_dir = _make_multi_run_dir(sites)
    for analysis in analyses:
        _save_site_csv(analysis, os.path.join(run_dir, analysis.site_key))
    combined.to_csv(
        os.path.join(run_dir, "categories_all_sites.csv"),
        index=False,
        encoding="utf-8",
    )
    with open(os.path.join(run_dir, "summary.txt"), "w", encoding="utf-8") as handle:
        handle.write(summary_text)
    print(f"\nZapisano wyniki do katalogu:\n  {run_dir}")


# --- tryb: Search vs Discover + device + YMYL -----------------------------

SOURCE_LABELS = {"web": "Search", "discover": "Discover", "news": "News"}


def _run_sources_report(
    global_config: GlobalConfig, sites: list[SiteConfig]
) -> None:
    print("\nPodaj wspólne okresy (daty w formacie YYYY-MM-DD).")
    current_start, current_end = _ask_period("CURRENT PERIOD")
    previous_start, previous_end = _ask_period("PREVIOUS PERIOD")
    _report_period_lengths(current_start, current_end, previous_start, previous_end)

    current = Period(current_start, current_end)
    previous = Period(previous_start, previous_end)
    credentials = global_config.google_credentials_path

    sources = []
    for s in sites:
        try:
            sources.append(analyze_site_sources(s, current, previous, credentials))
        except (GSCError, SitemapError) as exc:
            print(f"\n[POMINIĘTO] {s.name}: {exc}")
    if not sources:
        print("\nŻaden serwis nie zwrócił danych – nic do pokazania.")
        return
    combined = _combined_source_categories_df(sources)

    buffer = _Tee(sys.stdout)
    with contextlib.redirect_stdout(buffer):
        _print_sources_warnings(sources)
        _print_source_overview(sources)
        _print_ymyl_split(sources)
        _print_device_breakdown(sources)
        _print_source_ranking(
            combined, "web", "TOP 20 GROWING CATEGORIES — SEARCH", ascending=False
        )
        _print_source_ranking(
            combined, "web", "TOP 20 DECLINING CATEGORIES — SEARCH", ascending=True
        )
        _print_source_ranking(
            combined, "discover", "TOP 20 GROWING CATEGORIES — DISCOVER", ascending=False
        )
        _print_source_ranking(
            combined, "discover", "TOP 20 DECLINING CATEGORIES — DISCOVER", ascending=True
        )
    summary_text = buffer.getvalue()

    _export_sources(sites, sources, combined, summary_text)


def _iter_sources(site: SiteSources):
    """Zwraca pary (search_type, SourceAnalysis) dla serwisu."""
    return (("web", site.search), ("discover", site.discover))


def _combined_source_categories_df(sources: list[SiteSources]) -> pd.DataFrame:
    columns = [
        "site_key",
        "site_name",
        "source",
        "category_path",
        "depth",
        "ymyl",
        "current_clicks",
        "previous_clicks",
        "clicks_change",
        "clicks_change_pct",
        "current_impressions",
        "previous_impressions",
        "impressions_change",
        "impressions_change_pct",
        "number_of_urls",
    ]
    frames = []
    for site in sources:
        for search_type, analysis in _iter_sources(site):
            tree = analysis.tree
            if tree.empty:
                continue
            frame = tree.copy()
            frame["site_key"] = site.site_key
            frame["site_name"] = site.site_name
            frame["source"] = SOURCE_LABELS[search_type]
            frames.append(frame[columns])
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


def _source_overview_df(sources: list[SiteSources]) -> pd.DataFrame:
    rows = []
    for site in sources:
        for search_type, analysis in _iter_sources(site):
            pages = analysis.pages
            current_impr = float(pages["current_impressions"].sum()) if not pages.empty else 0.0
            previous_impr = float(pages["previous_impressions"].sum()) if not pages.empty else 0.0
            rows.append(
                {
                    "site_key": site.site_key,
                    "site_name": site.site_name,
                    "source": SOURCE_LABELS[search_type],
                    "current_clicks": analysis.current_clicks,
                    "previous_clicks": analysis.previous_clicks,
                    "clicks_change": analysis.current_clicks - analysis.previous_clicks,
                    "clicks_change_pct": _pct(
                        analysis.current_clicks, analysis.previous_clicks
                    ),
                    "current_impressions": current_impr,
                    "previous_impressions": previous_impr,
                    "impressions_change": current_impr - previous_impr,
                }
            )
    return pd.DataFrame(rows)


def _device_breakdown_df(sources: list[SiteSources]) -> pd.DataFrame:
    rows = []
    for site in sources:
        for search_type, analysis in _iter_sources(site):
            for _, row in analysis.device.iterrows():
                rows.append(
                    {
                        "site_key": site.site_key,
                        "site_name": site.site_name,
                        "source": SOURCE_LABELS[search_type],
                        "device": row["device"],
                        "current_clicks": row["current_clicks"],
                        "previous_clicks": row["previous_clicks"],
                        "clicks_change": row["clicks_change"],
                    }
                )
    return pd.DataFrame(
        rows,
        columns=[
            "site_key",
            "site_name",
            "source",
            "device",
            "current_clicks",
            "previous_clicks",
            "clicks_change",
        ],
    )


def _ymyl_summary_df(sources: list[SiteSources]) -> pd.DataFrame:
    rows = []
    for site in sources:
        for search_type, analysis in _iter_sources(site):
            summary = ymyl_summary(analysis.tree)
            for _, row in summary.iterrows():
                rows.append(
                    {
                        "site_key": site.site_key,
                        "site_name": site.site_name,
                        "source": SOURCE_LABELS[search_type],
                        "ymyl": bool(row["ymyl"]),
                        "current_clicks": row["current_clicks"],
                        "previous_clicks": row["previous_clicks"],
                        "clicks_change": row["clicks_change"],
                        "clicks_change_pct": row["clicks_change_pct"],
                    }
                )
    return pd.DataFrame(
        rows,
        columns=[
            "site_key",
            "site_name",
            "source",
            "ymyl",
            "current_clicks",
            "previous_clicks",
            "clicks_change",
            "clicks_change_pct",
        ],
    )


def _print_sources_warnings(sources: list[SiteSources]) -> None:
    all_warnings = [w for s in sources for w in s.warnings]
    if not all_warnings:
        return
    print("\n" + "=" * 50)
    print("VALIDATION WARNINGS")
    print("=" * 50)
    for warning in all_warnings:
        print(f"WARNING: {warning}")


def _print_source_overview(sources: list[SiteSources]) -> None:
    print("\n" + "=" * 72)
    print("SOURCE OVERVIEW (Search vs Discover)")
    print("=" * 72)
    header = f"{'Site':22}{'Source':10}{'Current':>12}{'Previous':>12}{'Change':>12}{'%':>10}"
    print(header)
    print("-" * len(header))
    for site in sources:
        for search_type, analysis in _iter_sources(site):
            current = analysis.current_clicks
            previous = analysis.previous_clicks
            print(
                f"{site.site_name[:22]:22}"
                f"{SOURCE_LABELS[search_type]:10}"
                f"{_fmt_clicks(current):>12}"
                f"{_fmt_clicks(previous):>12}"
                f"{_fmt_signed(current - previous):>12}"
                f"{_fmt_pct(_pct(current, previous)):>10}"
            )


def _print_ymyl_split(sources: list[SiteSources]) -> None:
    print("\n" + "=" * 72)
    print("YMYL vs NON-YMYL (podział ruchu kategorii)")
    print("=" * 72)
    for site in sources:
        print(f"\n{site.site_name}")
        if not site.ymyl_paths:
            print("  (brak ymyl_paths w sites.yaml – pomijam)")
            continue
        for search_type, analysis in _iter_sources(site):
            summary = ymyl_summary(analysis.tree)
            label = SOURCE_LABELS[search_type]
            if summary.empty:
                print(f"  {label}: (brak danych)")
                continue
            for _, row in summary.iterrows():
                tag = "YMYL    " if row["ymyl"] else "non-YMYL"
                print(
                    f"  {label:9}{tag}  "
                    f"cur {_fmt_clicks(row['current_clicks']):>11}  "
                    f"prev {_fmt_clicks(row['previous_clicks']):>11}  "
                    f"{_fmt_signed(row['clicks_change']):>11}  "
                    f"{_fmt_pct(row['clicks_change_pct']):>8}"
                )


def _print_device_breakdown(sources: list[SiteSources]) -> None:
    print("\n" + "=" * 72)
    print("DEVICE BREAKDOWN")
    print("=" * 72)
    for site in sources:
        print(f"\n{site.site_name}")
        for search_type, analysis in _iter_sources(site):
            label = SOURCE_LABELS[search_type]
            if analysis.device.empty:
                print(f"  {label}: (brak danych)")
                continue
            for _, row in analysis.device.iterrows():
                print(
                    f"  {label:9}{str(row['device']):10}"
                    f"cur {_fmt_clicks(row['current_clicks']):>11}  "
                    f"prev {_fmt_clicks(row['previous_clicks']):>11}  "
                    f"{_fmt_signed(row['clicks_change']):>11}"
                )


def _print_source_ranking(
    combined: pd.DataFrame, search_type: str, title: str, ascending: bool
) -> None:
    label = SOURCE_LABELS[search_type]
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    if combined.empty:
        print("(brak danych)")
        return
    subset = combined[combined["source"] == label]
    if ascending:
        subset = subset[subset["clicks_change"] < 0]
    else:
        subset = subset[subset["clicks_change"] > 0]
    ranked = subset.sort_values("clicks_change", ascending=ascending).head(20)
    if ranked.empty:
        print("(brak danych)")
        return
    header = f"{'Site':14}{'Category':<30}{'YMYL':>5}{'Diff':>11}{'%':>9}"
    print(header)
    print("-" * len(header))
    for _, row in ranked.iterrows():
        cat = f"{row['category_path']} (d{int(row['depth'])})"
        ymyl_mark = "Y" if row["ymyl"] else "-"
        print(
            f"{str(row['site_name'])[:14]:14}"
            f"{cat[:30]:<30}"
            f"{ymyl_mark:>5}"
            f"{_fmt_signed(row['clicks_change']):>11}"
            f"{_fmt_pct(row['clicks_change_pct']):>9}"
        )


def _make_sources_run_dir(sites: list[SiteConfig]) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    keys = "_".join(s.key for s in sites)
    name = f"{stamp}_sources_{keys}"[:120]
    run_dir = os.path.join(OUTPUT_DIR, name)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def _export_sources(
    sites: list[SiteConfig],
    sources: list[SiteSources],
    combined: pd.DataFrame,
    summary_text: str,
) -> None:
    run_dir = _make_sources_run_dir(sites)
    _source_overview_df(sources).to_csv(
        os.path.join(run_dir, "source_overview.csv"), index=False, encoding="utf-8"
    )
    _device_breakdown_df(sources).to_csv(
        os.path.join(run_dir, "device_breakdown.csv"), index=False, encoding="utf-8"
    )
    _ymyl_summary_df(sources).to_csv(
        os.path.join(run_dir, "ymyl_summary.csv"), index=False, encoding="utf-8"
    )
    combined.to_csv(
        os.path.join(run_dir, "categories_by_source.csv"),
        index=False,
        encoding="utf-8",
    )
    with open(os.path.join(run_dir, "summary.txt"), "w", encoding="utf-8") as handle:
        handle.write(summary_text)
    print(f"\nZapisano wyniki do katalogu:\n  {run_dir}")


def _print_run_warnings(a: SiteAnalysis, b: SiteAnalysis) -> None:
    all_warnings = list(a.warnings) + list(b.warnings)
    if not all_warnings:
        return
    print("\n" + "=" * 50)
    print("VALIDATION WARNINGS")
    print("=" * 50)
    for warning in all_warnings:
        print(f"WARNING: {warning}")


def _export(
    site_a: SiteConfig,
    site_b: SiteConfig,
    analysis_a: SiteAnalysis,
    analysis_b: SiteAnalysis,
    totals_a: dict,
    totals_b: dict,
    groups: list[dict],
    summary_text: str,
) -> None:
    run_dir = _make_run_dir(site_a, site_b)

    _site_comparison_df(totals_a, totals_b).to_csv(
        os.path.join(run_dir, "site_comparison.csv"), index=False, encoding="utf-8"
    )
    _save_site_csv(analysis_a, os.path.join(run_dir, site_a.key))
    _save_site_csv(analysis_b, os.path.join(run_dir, site_b.key))

    if groups:
        _group_comparison_df(groups).to_csv(
            os.path.join(run_dir, "category_groups_comparison.csv"),
            index=False,
            encoding="utf-8",
        )

    with open(os.path.join(run_dir, "summary.txt"), "w", encoding="utf-8") as handle:
        handle.write(summary_text)

    print(f"\nZapisano wyniki do katalogu:\n  {run_dir}")


def main() -> int:
    # Wymuś UTF-8 na wyjściu, aby polskie znaki i '—' nie były zniekształcone
    # w konsoli Windows.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    try:
        global_config = load_global_config()
        sites_config = load_sites()
        run(global_config, sites_config)
    except ConfigError as exc:
        print(f"\n[Konfiguracja] {exc}")
        return 1
    except SelectionError as exc:
        print(f"\n[Wybór serwisu] {exc}")
        return 1
    except DateInputError as exc:
        print(f"\n[Daty] {exc}")
        return 1
    except GSCError as exc:
        print(f"\n[Google Search Console] {exc}")
        return 1
    except SitemapError as exc:
        print(f"\n[Sitemap] {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nPrzerwano.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

