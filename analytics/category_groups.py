"""Agregacja i porównanie grup kategorii między dwoma serwisami (ETAP 2).

Grupa kategorii to ręczne wskazanie odpowiadających sobie category_path
w różnych serwisach. Sumujemy TOTAL wskazanych węzłów drzewa (który już
zawiera dzieci) – nie sumujemy dzieci ponownie, aby nie liczyć ruchu podwójnie.
"""
from __future__ import annotations

import math

import pandas as pd

from analytics.comparison import pct_change

# Próg uznania zmiany za "płaską" (w procentach).
FLAT_THRESHOLD_PCT = 1.0


def aggregate_group(tree: pd.DataFrame, paths: list[str]) -> dict:
    """Sumuje TOTAL clicks wskazanych category_path z drzewa serwisu.

    Zwraca current/previous/change/change_pct oraz listę brakujących ścieżek.
    Zakłada, że podane ścieżki nie zawierają się nawzajem (np. parent i child),
    zgodnie z zasadą braku podwójnego liczenia.
    """
    by_path: dict[str, pd.Series] = {}
    if not tree.empty:
        by_path = {row["category_path"]: row for _, row in tree.iterrows()}

    current = 0.0
    previous = 0.0
    missing: list[str] = []
    for path in paths:
        if path in by_path:
            row = by_path[path]
            current += float(row["total_current_clicks"])
            previous += float(row["total_previous_clicks"])
        else:
            missing.append(path)

    return {
        "current_clicks": current,
        "previous_clicks": previous,
        "clicks_change": current - previous,
        "clicks_change_pct": pct_change(current, previous),
        "missing_paths": missing,
    }


def growth_difference_pp(site_a_pct: float, site_b_pct: float) -> float:
    """Różnica dynamiki w punktach procentowych (site_a - site_b)."""
    if _is_nan(site_a_pct) or _is_nan(site_b_pct):
        return math.nan
    return site_a_pct - site_b_pct


def direction_pattern(
    site_a_pct: float,
    site_b_pct: float,
    flat_threshold: float = FLAT_THRESHOLD_PCT,
) -> str:
    """Klasyfikuje wzajemny kierunek zmian dwóch serwisów."""
    a = _state(site_a_pct, flat_threshold)
    b = _state(site_b_pct, flat_threshold)
    if a == "up" and b == "up":
        return "both_growing"
    if a == "down" and b == "down":
        return "both_declining"
    if a == "up" and b == "down":
        return "site_a_growing_site_b_declining"
    if a == "down" and b == "up":
        return "site_a_declining_site_b_growing"
    return "flat_or_mixed"


def build_group_comparison(
    group_key: str,
    group_label: str,
    site_a_key: str,
    site_a_name: str,
    tree_a: pd.DataFrame,
    paths_a: list[str],
    site_b_key: str,
    site_b_name: str,
    tree_b: pd.DataFrame,
    paths_b: list[str],
) -> dict:
    """Buduje porównanie jednej grupy kategorii między dwoma serwisami."""
    agg_a = aggregate_group(tree_a, paths_a)
    agg_b = aggregate_group(tree_b, paths_b)
    diff_pp = growth_difference_pp(
        agg_a["clicks_change_pct"], agg_b["clicks_change_pct"]
    )
    pattern = direction_pattern(
        agg_a["clicks_change_pct"], agg_b["clicks_change_pct"]
    )

    return {
        "group_key": group_key,
        "group_label": group_label,
        "site_a_key": site_a_key,
        "site_a_name": site_a_name,
        "site_a_current_clicks": agg_a["current_clicks"],
        "site_a_previous_clicks": agg_a["previous_clicks"],
        "site_a_change": agg_a["clicks_change"],
        "site_a_change_pct": agg_a["clicks_change_pct"],
        "site_a_missing_paths": agg_a["missing_paths"],
        "site_b_key": site_b_key,
        "site_b_name": site_b_name,
        "site_b_current_clicks": agg_b["current_clicks"],
        "site_b_previous_clicks": agg_b["previous_clicks"],
        "site_b_change": agg_b["clicks_change"],
        "site_b_change_pct": agg_b["clicks_change_pct"],
        "site_b_missing_paths": agg_b["missing_paths"],
        "growth_difference_pp": diff_pp,
        "direction_pattern": pattern,
    }


def _is_nan(value: float) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _state(pct: float, flat_threshold: float) -> str:
    if _is_nan(pct):
        return "flat"
    if pct > flat_threshold:
        return "up"
    if pct < -flat_threshold:
        return "down"
    return "flat"
