"""Oznaczanie kategorii jako YMYL i agregacja YMYL vs non-YMYL.

YMYL (Your Money or Your Life) to kategorie wrażliwe (finanse, polityka, zdrowie).
Klasyfikacja jest konfigurowana w sites.yaml (ymyl_paths) na poziomie top-level.
"""
from __future__ import annotations

import pandas as pd

from analytics.comparison import pct_change

CATEGORY_SEPARATOR = " > "


def is_ymyl(
    category_path: str,
    ymyl_paths: list[str],
    exclude_paths: list[str] | None = None,
) -> bool:
    """True, gdy kategoria jest YMYL (lub jej podkategorią) i nie jest wykluczona.

    Wykluczenia mają pierwszeństwo: ścieżka pasująca do exclude_paths nigdy nie
    jest YMYL, nawet jeśli leży pod kategorią YMYL (np. Pogoda pod Wydarzenia).
    """
    exclude_paths = exclude_paths or []
    for path in exclude_paths:
        if category_path == path or category_path.startswith(
            f"{path}{CATEGORY_SEPARATOR}"
        ):
            return False
    for path in ymyl_paths:
        if category_path == path or category_path.startswith(
            f"{path}{CATEGORY_SEPARATOR}"
        ):
            return True
    return False


def tag_ymyl(
    tree: pd.DataFrame,
    ymyl_paths: list[str],
    exclude_paths: list[str] | None = None,
) -> pd.DataFrame:
    """Dodaje kolumnę `ymyl` (bool) do drzewa kategorii."""
    result = tree.copy()
    if result.empty:
        result["ymyl"] = pd.Series(dtype=bool)
        return result
    result["ymyl"] = result["category_path"].apply(
        lambda c: is_ymyl(c, ymyl_paths, exclude_paths)
    )
    return result


def ymyl_summary(tagged_tree: pd.DataFrame) -> pd.DataFrame:
    """Agreguje ruch po fladze YMYL, używając tylko węzłów depth=1 (bez podwójnego liczenia).

    Zwraca DataFrame z kolumnami: ymyl, current_clicks, previous_clicks,
    clicks_change, clicks_change_pct.
    """
    columns = [
        "ymyl",
        "current_clicks",
        "previous_clicks",
        "clicks_change",
        "clicks_change_pct",
    ]
    if tagged_tree.empty:
        return pd.DataFrame(columns=columns)

    top = tagged_tree[tagged_tree["depth"] == 1]
    rows = []
    for flag, group in top.groupby("ymyl"):
        current = float(group["current_clicks"].sum())
        previous = float(group["previous_clicks"].sum())
        rows.append(
            {
                "ymyl": bool(flag),
                "current_clicks": current,
                "previous_clicks": previous,
                "clicks_change": current - previous,
                "clicks_change_pct": pct_change(current, previous),
            }
        )
    return pd.DataFrame(rows, columns=columns)
