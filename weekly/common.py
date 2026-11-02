"""Configuration, ISO periods and safe JSON persistence."""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
PACIFIC = ZoneInfo("America/Los_Angeles")
METRICS = ("clicks", "impressions", "ctr", "position")
Status = Literal["OK", "WARN", "FAIL"]


class Threshold(BaseModel):
    """Relative deterioration required for each severity."""

    model_config = ConfigDict(extra="forbid")
    warn: float = Field(ge=0, allow_inf_nan=False)
    fail: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self) -> Threshold:
        """Require a stronger failure threshold."""
        if self.fail < self.warn:
            raise ValueError("fail must be >= warn")
        return self


class Site(BaseModel):
    """Validated non-secret site configuration."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*$")
    name: str
    placeholder: bool = True
    gsc_property: str = Field(pattern=r"^(sc-domain:[^/\s]+|https?://\S+)$")
    base_url: str = Field(pattern=r"^https?://\S+$")
    sitemap_url: str | None = Field(default=None, pattern=r"^https?://\S+$")
    category_sitemaps: list[str] = Field(default_factory=list)
    brand_terms: list[str] = Field(default_factory=list)
    thresholds: dict[str, Threshold]
    representative_urls: dict[str, str | None]
    inspection_limit: int = Field(default=3, ge=0, le=20)

    @model_validator(mode="after")
    def required_settings(self) -> Site:
        """Reject missing metrics, roles and empty brand terms."""
        if set(self.thresholds) != set(METRICS):
            raise ValueError("thresholds must contain all four metrics")
        if set(self.representative_urls) != {"home", "article", "category"}:
            raise ValueError("representative_urls must contain home, article, category")
        if any(not value.strip() for value in self.brand_terms):
            raise ValueError("brand terms cannot be empty")
        if any(
            value is not None and not re.match(r"^https?://\S+$", value)
            for value in self.representative_urls.values()
        ):
            raise ValueError("representative URLs must be HTTP(S)")
        if any(not re.match(r"^https?://\S+$", value) for value in self.category_sitemaps):
            raise ValueError("category sitemaps must be HTTP(S)")
        return self


class Settings(BaseModel):
    """Request budget and site list."""

    model_config = ConfigDict(extra="forbid")
    request_interval_seconds: float = Field(default=1, ge=0.1, allow_inf_nan=False)
    max_retries: int = Field(default=4, ge=0, le=8)
    sites: list[Site] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_sites(self) -> Settings:
        """Prevent output collisions."""
        if len({site.id for site in self.sites}) != len(self.sites):
            raise ValueError("site IDs must be unique")
        return self


def load_settings(path: Path = ROOT / "sites.yaml") -> Settings:
    """Read and validate YAML without loading secrets into reports."""
    return Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def today_gsc() -> date:
    """Use the calendar timezone of Search Analytics."""
    return datetime.now(PACIFIC).date()


def week_start(week: str | None = None, today: date | None = None) -> date:
    """Resolve an ISO week or the last completed Monday-Sunday period."""
    current = today or today_gsc()
    if week is None:
        return current - timedelta(days=current.weekday() + 7)
    if not re.fullmatch(r"\d{4}-W\d{2}", week):
        raise ValueError("week must use YYYY-Www, for example 2026-W43")
    start = date.fromisocalendar(int(week[:4]), int(week[6:]), 1)
    if start + timedelta(days=6) >= current:
        raise ValueError("week must be completed in America/Los_Angeles")
    return start


def week_id(start: date) -> str:
    """Return the ISO year and week, including year boundaries."""
    year, number, _ = start.isocalendar()
    return f"{year}-W{number:02d}"


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Replace a JSON file atomically and reject non-finite numbers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def setup_logging() -> None:
    """Configure console logging and local environment variables."""
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def parser(description: str) -> argparse.ArgumentParser:
    """Build the shared command-line interface."""
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--week", help="Completed ISO week: YYYY-Www")
    result.add_argument("--config", type=Path, default=ROOT / "sites.yaml")
    return result


def worst_status(statuses: list[str]) -> Status:
    """Aggregate severity without asking a language model to calculate."""
    if "FAIL" in statuses:
        return "FAIL"
    return "WARN" if "WARN" in statuses else "OK"