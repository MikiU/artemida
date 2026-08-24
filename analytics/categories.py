"""Przypisywanie URL-i do kategorii na podstawie sitemapy kategorii.

Zasada: dla danego URL wybieramy kategorię, której ścieżka jest najdłuższym
poprawnym prefiksem ścieżki URL-a (najbardziej szczegółowa kategoria).
"""
from __future__ import annotations

from urllib.parse import unquote, urlparse

import pandas as pd

OTHER = "Other"
HOMEPAGE = "Homepage"

# Ścieżki traktowane jako strona główna domeny (niezależnie od domeny).
_HOMEPAGE_PATHS = {"", "/"}


def _path(url: str) -> str:
    """Zwraca część ścieżki URL-a (bez schematu, hosta, query)."""
    return urlparse(url).path


def _normalize_category_path(path: str) -> str:
    """Zapewnia, że ścieżka kategorii kończy się '/' (granica segmentu)."""
    if not path:
        return "/"
    return path if path.endswith("/") else path + "/"


def _label_from_path(path: str) -> str:
    """Buduje czytelną etykietę, np. '/sport/pilka-nozna/' -> 'Sport > Pilka Nozna'."""
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return "/"
    parts = []
    for seg in segments:
        readable = unquote(seg).replace("-", " ").replace("_", " ").strip()
        parts.append(readable.title())
    return " > ".join(parts)


def build_category_index(category_urls: list[str]) -> list[tuple[str, str]]:
    """Zwraca listę (znormalizowana_ścieżka, etykieta) posortowaną od najdłuższej.

    Dzięki sortowaniu malejąco po długości ścieżki pierwsze dopasowanie jest
    zawsze najbardziej szczegółowe.
    """
    index: dict[str, str] = {}
    for url in category_urls:
        norm = _normalize_category_path(_path(url))
        if norm and norm != "/":
            index[norm] = _label_from_path(norm)
    return sorted(index.items(), key=lambda item: len(item[0]), reverse=True)


def match_category(url: str, category_index: list[tuple[str, str]]) -> str:
    """Zwraca etykietę kategorii: 'Homepage', najdłuższy pasujący prefiks lub 'Other'."""
    url_path = _path(url)
    if url_path in _HOMEPAGE_PATHS:
        return HOMEPAGE
    for cat_path, label in category_index:
        # cat_path kończy się '/'; obsłuż też URL kategorii bez końcowego '/'.
        if url_path == cat_path[:-1] or url_path.startswith(cat_path):
            return label
    return OTHER


def assign_categories(
    df: pd.DataFrame,
    category_urls: list[str],
    url_column: str = "page",
) -> pd.DataFrame:
    """Dodaje kolumnę `category_path` do DataFrame na podstawie sitemapy."""
    category_index = build_category_index(category_urls)
    result = df.copy()
    result["category_path"] = result[url_column].apply(
        lambda url: match_category(url, category_index)
    )
    return result
