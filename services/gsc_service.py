"""Pobieranie danych z Google Search Console (Search Analytics API).

Autoryzacja przez Service Account. Konto serwisowe musi być dodane jako
użytkownik property w Google Search Console (patrz README).
"""
from __future__ import annotations

import hashlib
import json
import os

import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Zakres tylko do odczytu danych Search Console.
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

# Maksymalny rozmiar strony wyników zwracany przez API.
ROW_LIMIT = 25000

# Katalog lokalnego cache; klucz zawiera property, więc dane domen się nie mieszają.
CACHE_DIR = os.path.join(".cache", "gsc")


class GSCError(Exception):
    """Błąd komunikacji z Google Search Console."""


def _build_service(credentials_ref: str):
    """Tworzy klienta Search Console API z pliku Service Account albo z JSON-a.

    credentials_ref może być ścieżką do pliku JSON lub pełną treścią JSON
    (np. z sekretu na hostingu).
    """
    try:
        ref = credentials_ref.strip()
        if ref.startswith("{"):
            info = json.loads(ref)
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
        else:
            credentials = service_account.Credentials.from_service_account_file(
                ref, scopes=SCOPES
            )
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise GSCError(f"Nieprawidłowe credentials Service Account: {exc}") from exc

    return build("searchconsole", "v1", credentials=credentials, cache_discovery=False)


def _cache_path(
    gsc_property: str,
    start_date: str,
    end_date: str,
    dimensions: list[str],
    search_type: str,
) -> str:
    """Ścieżka pliku cache – klucz uwzględnia property, daty, wymiary i typ."""
    key = f"{gsc_property}|{start_date}|{end_date}|{','.join(dimensions)}"
    # Zachowaj zgodność ze starym cache dla web (bez typu w kluczu).
    if search_type != "web":
        key += f"|{search_type}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{digest}.csv")


def get_gsc_data(
    start_date: str,
    end_date: str,
    gsc_property: str,
    credentials_path: str,
    dimensions: tuple[str, ...] = ("page",),
    search_type: str = "web",
    use_cache: bool = True,
) -> pd.DataFrame:
    """Pobiera dane Search Analytics dla podanych wymiarów i typu danych.

    dimensions: np. ("page",) albo ("device",).
    search_type: "web" (Search) lub "discover" (Google Discover).
    Zwraca DataFrame z kolumnami: <wymiary...>, clicks, impressions, ctr, position.
    Obsługuje paginację przez startRow. Wynik jest cache'owany lokalnie
    (klucz: property + daty + wymiary + typ), więc dane domen i typów się nie mieszają.
    """
    dims = list(dimensions)
    columns = dims + ["clicks", "impressions", "ctr", "position"]
    cache_file = _cache_path(gsc_property, start_date, end_date, dims, search_type)
    if use_cache and os.path.exists(cache_file):
        print(f"Cache hit: {gsc_property} {start_date}..{end_date} [{search_type}]")
        dtype = {dim: str for dim in dims}
        return pd.read_csv(cache_file, dtype=dtype)

    service = _build_service(credentials_path)

    rows: list[dict] = []
    start_row = 0

    while True:
        print(f"Fetching rows {start_row}-{start_row + ROW_LIMIT - 1} [{search_type}]")
        request_body = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dims,
            "type": search_type,
            "rowLimit": ROW_LIMIT,
            "startRow": start_row,
        }

        try:
            response = (
                service.searchanalytics()
                .query(siteUrl=gsc_property, body=request_body)
                .execute()
            )
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status == 403:
                raise GSCError(
                    f"Brak dostępu do property '{gsc_property}'.\n"
                    "Sprawdź, czy adres e-mail Service Account został dodany jako "
                    "użytkownik tej property w Google Search Console."
                ) from exc
            if status == 404:
                raise GSCError(
                    f"Nie znaleziono property '{gsc_property}'.\n"
                    "Sprawdź wartość gsc_property w sites.yaml "
                    "(np. 'sc-domain:example.com' lub 'https://example.com/')."
                ) from exc
            raise GSCError(f"Błąd Google Search Console API: {exc}") from exc

        batch = response.get("rows", [])
        for row in batch:
            record: dict = {}
            for i, dim in enumerate(dims):
                record[dim] = row["keys"][i]
            record["clicks"] = row.get("clicks", 0)
            record["impressions"] = row.get("impressions", 0)
            record["ctr"] = row.get("ctr", 0.0)
            record["position"] = row.get("position", 0.0)
            rows.append(record)

        if len(batch) < ROW_LIMIT:
            break
        start_row += ROW_LIMIT

    df = pd.DataFrame(rows, columns=columns)

    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(cache_file, index=False, encoding="utf-8")

    return df


