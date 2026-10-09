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



from .config import BASE_URL, DEFAULT_INPUT
from .local_data import LocalCsvDataset
from .downloader import Downloader, cache_path
from .html_parser import parse_airport_document
from .output import status_row

def local_fallback_result(
    icao: str,
    local_data: LocalCsvDataset | None,
    html_error: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    record = local_data.record(icao) if local_data else None
    if record is None:
        return None, status_row(
            icao,
            success=False,
            source_url=str(DEFAULT_INPUT),
            error=html_error,
            source_type="LOCAL_CSV",
        )
    return record, status_row(
        icao,
        success=True,
        source_url=str(DEFAULT_INPUT),
        parsed_runways=record["runways_count"],
        error=f"HTML unavailable; local CSV fallback used: {html_error}",
        source_type="LOCAL_CSV",
    )


def process_one(
    icao: str,
    downloader: Downloader,
    cache_dir: Path,
    local_data: LocalCsvDataset | None = None,
    local_only: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if local_only:
        return local_fallback_result(icao, local_data, "local-only mode")

    airport_url = f"{BASE_URL}/airports/{icao}/"
    runways_url = f"{BASE_URL}/airports/{icao}/runways.html"

    combined = downloader.fetch(runways_url, cache_path(cache_dir, icao, "runways"))
    if combined.html is not None:
        try:
            record = parse_airport_document(
                combined.html,
                icao,
                runways_url,
                airport_url,
                runways_url,
            )
            return record, status_row(
                icao,
                success=True,
                fetch=combined,
                source_url=runways_url,
                parsed_runways=record["runways_count"],
            )
        except Exception as exc:
            combined_error = f"combined page parse: {type(exc).__name__}: {exc}"
    else:
        combined_error = combined.error

    print(f"    Runways page unusable ({combined_error}); trying airport page")
    fallback = downloader.fetch(airport_url, cache_path(cache_dir, icao, "airport"))
    if fallback.html is None:
        error = f"runways: {combined_error}; airport: {fallback.error}"
        if local_data is not None:
            return local_fallback_result(icao, local_data, error)
        return None, status_row(
            icao,
            success=False,
            fetch=fallback,
            source_url=airport_url,
            error=error,
        )

    try:
        record = parse_airport_document(
            fallback.html,
            icao,
            airport_url,
            airport_url,
            runways_url,
        )
        return record, status_row(
            icao,
            success=True,
            fetch=fallback,
            source_url=airport_url,
            parsed_runways=record["runways_count"],
            error=f"runways page failed: {combined_error}",
        )
    except Exception as exc:
        error = f"airport page parse: {type(exc).__name__}: {exc}"
        if local_data is not None:
            return local_fallback_result(icao, local_data, error)
        return None, status_row(
            icao,
            success=False,
            fetch=fallback,
            source_url=airport_url,
            error=error,
        )


