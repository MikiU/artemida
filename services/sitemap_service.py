"""Pobieranie i parsowanie sitemapy kategorii.

Obsługuje zwykły <urlset> oraz <sitemapindex> (wtedy pobiera wskazane sitemapy).
Obsługuje też sitemapy spakowane gzipem (np. adresy kończące się na .gz).
"""
from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET

import requests

# Standardowa przestrzeń nazw sitemap.
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

_REQUEST_TIMEOUT = 30


class SitemapError(Exception):
    """Błąd pobierania lub parsowania sitemapy."""


def _fetch(url: str) -> bytes:
    """Pobiera zawartość URL i w razie potrzeby rozpakowuje gzip."""
    try:
        response = requests.get(url, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise SitemapError(f"Sitemap '{url}' zwróciła status HTTP {status}.") from exc
    except requests.RequestException as exc:
        raise SitemapError(f"Nie udało się pobrać sitemapy '{url}': {exc}") from exc

    content = response.content
    # Rozpakuj, gdy adres kończy się na .gz lub zawartość ma magiczne bajty gzip.
    if url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except OSError as exc:
            raise SitemapError(
                f"Nie udało się rozpakować sitemapy gzip '{url}': {exc}"
            ) from exc
    return content


def _parse_xml(content: bytes, url: str) -> ET.Element:
    """Parsuje XML. Rzuca SitemapError dla błędnego XML."""
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise SitemapError(f"Błędny XML w sitemapie '{url}': {exc}") from exc


def _local_name(tag: str) -> str:
    """Zwraca nazwę taga bez przestrzeni nazw."""
    return tag.split("}")[-1]


def get_category_urls(sitemap_url: str) -> list[str]:
    """Zwraca listę URL-i kategorii z sitemapy.

    Jeśli sitemap jest indeksem (<sitemapindex>), pobiera każdą wskazaną
    sitemapę i łączy wyniki.
    """
    content = _fetch(sitemap_url)
    root = _parse_xml(content, sitemap_url)
    root_name = _local_name(root.tag)

    urls: list[str] = []

    if root_name == "sitemapindex":
        for sitemap in root.findall("sm:sitemap", _NS):
            loc = sitemap.find("sm:loc", _NS)
            if loc is not None and loc.text:
                urls.extend(get_category_urls(loc.text.strip()))
    elif root_name == "urlset":
        for url_node in root.findall("sm:url", _NS):
            loc = url_node.find("sm:loc", _NS)
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
    else:
        raise SitemapError(
            f"Nieznany format sitemapy '{sitemap_url}' (root: <{root_name}>)."
        )

    # Usuń duplikaty zachowując kolejność.
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique
