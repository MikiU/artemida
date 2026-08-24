"""Porównanie okresów: per URL, agregacja płaska oraz agregacja drzewa kategorii."""
from __future__ import annotations

import math
from collections import defaultdict

import pandas as pd

from analytics.categories import HOMEPAGE, OTHER

# Separator poziomów w etykiecie kategorii, np. "Wydarzenia > Polska".
CATEGORY_SEPARATOR = " > "

# Etykiety, które nie są częścią drzewa kategorii sitemapy.
_NON_TREE = {HOMEPAGE, OTHER}

# Metryki sumowane w agregacjach.
_METRICS = [
    "current_clicks",
    "previous_clicks",
    "current_impressions",
    "previous_impressions",
]


def pct_change(current: float, previous: float) -> float:
    """Zmiana procentowa z obsługą dzielenia przez zero.

    - previous == 0 i current == 0 -> 0.0
    - previous == 0 i current != 0 -> NaN (nie zwracamy nieskończoności)
    - w pozostałych przypadkach zwykła zmiana procentowa
    """
    if previous == 0:
        return 0.0 if current == 0 else math.nan
    return (current - previous) / previous * 100.0


def compare_periods(
    current_df: pd.DataFrame,
    previous_df: pd.DataFrame,
) -> pd.DataFrame:
    """Łączy dane po URL i liczy zmiany kliknięć oraz impressions.

    Zwraca DataFrame z kolumnami:
        page,
        current_clicks, previous_clicks, clicks_change, clicks_change_pct,
        current_impressions, previous_impressions,
        impressions_change, impressions_change_pct
    Obsługuje URL-e występujące tylko w jednym z okresów (brak = 0).
    """
    current = current_df[["page", "clicks", "impressions"]].rename(
        columns={"clicks": "current_clicks", "impressions": "current_impressions"}
    )
    previous = previous_df[["page", "clicks", "impressions"]].rename(
        columns={"clicks": "previous_clicks", "impressions": "previous_impressions"}
    )

    merged = pd.merge(current, previous, on="page", how="outer")

    for col in [
        "current_clicks",
        "previous_clicks",
        "current_impressions",
        "previous_impressions",
    ]:
        merged[col] = merged[col].fillna(0)

    merged["clicks_change"] = merged["current_clicks"] - merged["previous_clicks"]
    merged["clicks_change_pct"] = merged.apply(
        lambda r: pct_change(r["current_clicks"], r["previous_clicks"]), axis=1
    )

    merged["impressions_change"] = (
        merged["current_impressions"] - merged["previous_impressions"]
    )
    merged["impressions_change_pct"] = merged.apply(
        lambda r: pct_change(r["current_impressions"], r["previous_impressions"]),
        axis=1,
    )

    columns = [
        "page",
        "current_clicks",
        "previous_clicks",
        "clicks_change",
        "clicks_change_pct",
        "current_impressions",
        "previous_impressions",
        "impressions_change",
        "impressions_change_pct",
    ]
    return merged[columns]


def aggregate_categories(comparison_df: pd.DataFrame) -> pd.DataFrame:
    """Agreguje wyniki po dokładnym `category_path`.

    Wymaga kolumny `category_path` w wejściowym DataFrame.
    """
    grouped = (
        comparison_df.groupby("category_path")
        .agg(
            current_clicks=("current_clicks", "sum"),
            previous_clicks=("previous_clicks", "sum"),
            current_impressions=("current_impressions", "sum"),
            previous_impressions=("previous_impressions", "sum"),
            number_of_urls=("page", "count"),
        )
        .reset_index()
    )

    grouped["clicks_change"] = grouped["current_clicks"] - grouped["previous_clicks"]
    grouped["clicks_change_pct"] = grouped.apply(
        lambda r: pct_change(r["current_clicks"], r["previous_clicks"]), axis=1
    )
    grouped["impressions_change"] = (
        grouped["current_impressions"] - grouped["previous_impressions"]
    )
    grouped["impressions_change_pct"] = grouped.apply(
        lambda r: pct_change(r["current_impressions"], r["previous_impressions"]),
        axis=1,
    )

    columns = [
        "category_path",
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
    return grouped[columns]


def build_category_tree(comparison_df: pd.DataFrame) -> pd.DataFrame:
    """Agreguje dane do drzewa kategorii (parent categories zawierają dzieci).

    Każdy URL zasila wszystkie węzły-przodków swojej kategorii, ale liczony jest
    tylko raz w każdym węźle. Pomija Homepage oraz Other (nie są częścią drzewa).

    Zwraca DataFrame z kolumnami: category_path, depth, current_clicks,
    previous_clicks, clicks_change, clicks_change_pct, current_impressions,
    previous_impressions, impressions_change, impressions_change_pct,
    number_of_urls.
    """
    acc: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "current_clicks": 0.0,
            "previous_clicks": 0.0,
            "current_impressions": 0.0,
            "previous_impressions": 0.0,
            "number_of_urls": 0,
        }
    )

    # Ruch przypisany dokładnie do danego category_path (bez potomków).
    direct: dict[str, dict[str, float]] = defaultdict(
        lambda: {"current_clicks": 0.0, "previous_clicks": 0.0}
    )

    for _, row in comparison_df.iterrows():
        category = row["category_path"]
        if category in _NON_TREE:
            continue
        direct[category]["current_clicks"] += row["current_clicks"]
        direct[category]["previous_clicks"] += row["previous_clicks"]
        segments = category.split(CATEGORY_SEPARATOR)
        for depth in range(1, len(segments) + 1):
            node = CATEGORY_SEPARATOR.join(segments[:depth])
            node_acc = acc[node]
            for metric in _METRICS:
                node_acc[metric] += row[metric]
            node_acc["number_of_urls"] += 1

    rows = []
    for node, values in acc.items():
        current_clicks = values["current_clicks"]
        previous_clicks = values["previous_clicks"]
        current_impressions = values["current_impressions"]
        previous_impressions = values["previous_impressions"]
        direct_current = direct[node]["current_clicks"]
        direct_previous = direct[node]["previous_clicks"]
        children_current = current_clicks - direct_current
        children_previous = previous_clicks - direct_previous
        rows.append(
            {
                "category_path": node,
                "depth": node.count(CATEGORY_SEPARATOR) + 1,
                "current_clicks": current_clicks,
                "previous_clicks": previous_clicks,
                "clicks_change": current_clicks - previous_clicks,
                "clicks_change_pct": pct_change(current_clicks, previous_clicks),
                "current_impressions": current_impressions,
                "previous_impressions": previous_impressions,
                "impressions_change": current_impressions - previous_impressions,
                "impressions_change_pct": pct_change(
                    current_impressions, previous_impressions
                ),
                "number_of_urls": values["number_of_urls"],
                "direct_current_clicks": direct_current,
                "direct_previous_clicks": direct_previous,
                "direct_clicks_change": direct_current - direct_previous,
                "children_current_clicks": children_current,
                "children_previous_clicks": children_previous,
                "children_clicks_change": children_current - children_previous,
                "total_current_clicks": current_clicks,
                "total_previous_clicks": previous_clicks,
                "total_clicks_change": current_clicks - previous_clicks,
            }
        )

    columns = [
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
        "direct_current_clicks",
        "direct_previous_clicks",
        "direct_clicks_change",
        "children_current_clicks",
        "children_previous_clicks",
        "children_clicks_change",
        "total_current_clicks",
        "total_previous_clicks",
        "total_clicks_change",
    ]
    tree = pd.DataFrame(rows, columns=columns)
    if tree.empty:
        return tree
    return tree.sort_values(
        ["depth", "current_clicks"], ascending=[True, False]
    ).reset_index(drop=True)


def coverage_report(comparison_df: pd.DataFrame) -> dict:
    """Liczy pokrycie klasyfikacji: podział URL-i i clicks na Homepage/Other/kategorie."""
    is_home = comparison_df["category_path"] == HOMEPAGE
    is_other = comparison_df["category_path"] == OTHER
    is_category = ~(is_home | is_other)

    def _sum(mask: pd.Series, column: str) -> float:
        return float(comparison_df.loc[mask, column].sum())

    total_current = float(comparison_df["current_clicks"].sum())
    total_previous = float(comparison_df["previous_clicks"].sum())
    category_current = _sum(is_category, "current_clicks")
    category_previous = _sum(is_category, "previous_clicks")
    homepage_current = _sum(is_home, "current_clicks")
    homepage_previous = _sum(is_home, "previous_clicks")
    other_current = _sum(is_other, "current_clicks")
    other_previous = _sum(is_other, "previous_clicks")

    def _pct(part: float, whole: float) -> float:
        return (part / whole * 100.0) if whole else math.nan

    return {
        "total_urls": int(len(comparison_df)),
        "category_urls": int(is_category.sum()),
        "homepage_urls": int(is_home.sum()),
        "other_urls": int(is_other.sum()),
        "total_current_clicks": total_current,
        "category_current_clicks": category_current,
        "homepage_current_clicks": homepage_current,
        "other_current_clicks": other_current,
        "total_previous_clicks": total_previous,
        "category_previous_clicks": category_previous,
        "homepage_previous_clicks": homepage_previous,
        "other_previous_clicks": other_previous,
        "coverage_current_pct": _pct(category_current, total_current),
        "coverage_previous_pct": _pct(category_previous, total_previous),
        # Rozkład całego ruchu (Homepage / Categories / Other).
        "homepage_current_pct": _pct(homepage_current, total_current),
        "categories_current_pct": _pct(category_current, total_current),
        "other_current_pct": _pct(other_current, total_current),
        "homepage_previous_pct": _pct(homepage_previous, total_previous),
        "categories_previous_pct": _pct(category_previous, total_previous),
        "other_previous_pct": _pct(other_previous, total_previous),
        # Pokrycie treści z wyłączeniem Homepage.
        "content_coverage_current_pct": _pct(
            category_current, total_current - homepage_current
        ),
        "content_coverage_previous_pct": _pct(
            category_previous, total_previous - homepage_previous
        ),
    }




def check_totals(comparison_df: pd.DataFrame, tolerance: float = 0.5) -> list[dict]:
    """Sprawdza, że total == Homepage + Other + kategorie (bez parent categories).

    Zwraca listę rozbieżności; pusta lista oznacza, że sumy się zgadzają.
    """
    is_home = comparison_df["category_path"] == HOMEPAGE
    is_other = comparison_df["category_path"] == OTHER
    is_category = ~(is_home | is_other)

    warnings = []
    for metric in _METRICS:
        total = float(comparison_df[metric].sum())
        parts = (
            float(comparison_df.loc[is_home, metric].sum())
            + float(comparison_df.loc[is_other, metric].sum())
            + float(comparison_df.loc[is_category, metric].sum())
        )
        diff = total - parts
        if abs(diff) > tolerance:
            warnings.append({"metric": metric, "total": total, "parts": parts, "diff": diff})
    return warnings
