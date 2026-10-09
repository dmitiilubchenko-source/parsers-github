from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag


BASE_URL = "https://ourairports.com"
ICAO_RE = re.compile(r"^[A-Z]{4}$")
RUNWAY_NAME_RE = re.compile(
    r"^(?:\d{1,2}[LCR]?|[A-Z0-9]{1,4})[-/](?:\d{1,2}[LCR]?|[A-Z0-9]{1,4})$",
    re.IGNORECASE,
)
RUNWAY_DIM_RE = re.compile(
    r"(?P<length_ft>[\d,]+)\s*[x×]\s*(?P<width_ft>[\d,]+)\s*ft\s*"
    r"\(\s*(?P<length_m>[\d,]+)\s*[x×]\s*(?P<width_m>[\d,]+)\s*m\s*\)",
    re.IGNORECASE,
)

FIELD_NAMES = {
    "tags": "tags",
    "name": "name",
    "location": "location",
    "iata code": "iata_code",
    "icao code": "icao_code",
    "facility type": "facility_type",
    "airline service?": "airline_service",
    "coordinates": "coordinates",
    "field elevation": "field_elevation",
    "members": "members",
    "web site": "web_site",
    "wikipedia page": "wikipedia_page",
    "keywords": "keywords",
    "last updated": "last_updated",
}

CSV_COLUMNS = [
    "_id",
    "icao_code",
    "iata_code",
    "name",
    "facility_type",
    "airline_service",
    "latitude_deg",
    "longitude_deg",
    "elevation_ft",
    "elevation_m",
    "location_text",
    "country_code",
    "country_name",
    "region_code",
    "region_name",
    "web_site",
    "wikipedia_page",
    "keywords",
    "tags",
    "last_updated",
    "runways_count",
    "runway_names",
    "runway_lengths_ft",
    "runways",
    "source_url",
    "airport_url",
    "runways_url",
    "scraped_at",
    "source_type",
]

STATUS_COLUMNS = [
    "icao_code",
    "success",
    "skipped",
    "from_cache",
    "source_url",
    "http_status",
    "parsed_runways",
    "error",
    "processed_at",
    "source_type",
]




def atomic_write_text(path: Path, value: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding=encoding)
    temporary.replace(path)


def cache_path(cache_dir: Path, icao: str, page: str) -> Path:
    return cache_dir / icao / f"{page}.html"


@dataclass(slots=True)
class FetchResult:
    html: str | None
    from_cache: bool
    status_code: int | None
    error: str = ""


class Downloader:
    def __init__(
        self,
        *,
        delay: float,
        timeout: float,
        retries: int,
        backoff: float,
        refresh: bool,
        user_agent: str,
    ) -> None:
        self.delay = max(0.0, delay)
        self.timeout = max(1.0, timeout)
        self.retries = max(1, retries)
        self.backoff = max(0.25, backoff)
        self.refresh = refresh
        self.last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.8",
            }
        )

    def _throttle(self) -> None:
        if not self.last_request_at:
            return
        remaining = self.delay - (time.monotonic() - self.last_request_at)
        if remaining > 0:
            time.sleep(remaining + random.uniform(0.05, 0.25))

    def _retry_after(self, response: requests.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After", "").strip()
        if header:
            try:
                return min(300.0, max(0.0, float(header)))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(header)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    return min(
                        300.0,
                        max(0.0, (retry_at - datetime.now(UTC)).total_seconds()),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(120.0, self.backoff * (2 ** (attempt - 1)))

    def fetch(self, url: str, target: Path) -> FetchResult:
        if target.exists() and not self.refresh:
            try:
                html = target.read_text(encoding="utf-8", errors="replace")
                if html.strip():
                    return FetchResult(html, True, 200)
            except OSError as exc:
                print(f"    Cache read failed ({exc}); downloading again")

        retryable = {408, 425, 429, 500, 502, 503, 504}
        last_error = "unknown download error"
        last_status: int | None = None

        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                response = self.session.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                self.last_request_at = time.monotonic()
                last_status = response.status_code

                if response.status_code == 404:
                    return FetchResult(None, False, 404, "404 not found")

                if response.status_code in retryable:
                    last_error = f"HTTP {response.status_code}"
                    if attempt < self.retries:
                        wait = self._retry_after(response, attempt)
                        print(
                            f"    {last_error}; retry {attempt}/{self.retries - 1} "
                            f"in {wait:.1f}s"
                        )
                        time.sleep(wait)
                        continue
                    return FetchResult(None, False, last_status, last_error)

                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if "html" not in content_type:
                    return FetchResult(
                        None,
                        False,
                        last_status,
                        f"unexpected Content-Type: {content_type or 'missing'}",
                    )

                html = response.text
                if not html.strip():
                    return FetchResult(None, False, last_status, "empty response")
                atomic_write_text(target, html)
                return FetchResult(html, False, last_status)

            except requests.RequestException as exc:
                self.last_request_at = time.monotonic()
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    wait = min(120.0, self.backoff * (2 ** (attempt - 1)))
                    print(
                        f"    Network error; retry {attempt}/{self.retries - 1} "
                        f"in {wait:.1f}s"
                    )
                    time.sleep(wait)

        return FetchResult(None, False, last_status, last_error)


