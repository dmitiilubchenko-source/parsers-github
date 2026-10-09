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



from .config import BASE_URL, DEFAULT_INPUT, clean_icao, detect_icao_column, now_iso, optional_float, optional_int, optional_text

class LocalCsvDataset:
    """Fallback records built from the project's downloaded OurAirports CSVs."""

    def __init__(self, root: Path, requested_icao_column: str = "icao_code") -> None:
        data_dir = root / "data"
        airports = pd.read_csv(
            data_dir / "airports.csv", low_memory=False, keep_default_na=False
        )
        icao_column = detect_icao_column(airports.columns, requested_icao_column)

        self.airports: dict[str, dict[str, Any]] = {}
        self.airport_ids: dict[str, str] = {}
        for raw in airports.to_dict(orient="records"):
            icao = clean_icao(raw.get(icao_column))
            if not icao:
                continue
            self.airports[icao] = raw
            airport_id = str(raw.get("id", "")).strip()
            if airport_id:
                self.airport_ids[airport_id] = icao

        self.countries = self._lookup(data_dir / "countries.csv", "code")
        self.regions = self._lookup(data_dir / "regions.csv", "code")
        self.runways: dict[str, list[dict[str, Any]]] = {}
        runway_path = data_dir / "runways.csv"
        if runway_path.exists():
            frame = pd.read_csv(runway_path, low_memory=False, keep_default_na=False)
            for raw in frame.to_dict(orient="records"):
                icao = clean_icao(raw.get("airport_ident"))
                if not icao:
                    icao = self.airport_ids.get(
                        str(raw.get("airport_ref", "")).strip(), ""
                    )
                if icao:
                    self.runways.setdefault(icao, []).append(self._runway(raw))

    @staticmethod
    def _lookup(path: Path, key: str) -> dict[str, dict[str, Any]]:
        if not path.exists():
            return {}
        frame = pd.read_csv(path, low_memory=False, keep_default_na=False)
        return {
            str(row.get(key, "")).strip(): row
            for row in frame.to_dict(orient="records")
            if str(row.get(key, "")).strip()
        }

    @staticmethod
    def _runway(raw: dict[str, Any]) -> dict[str, Any]:
        low = optional_text(raw.get("le_ident"))
        high = optional_text(raw.get("he_ident"))
        name = "/".join(value for value in (low, high) if value) or None
        length_ft = optional_int(raw.get("length_ft"))
        width_ft = optional_int(raw.get("width_ft"))
        return {
            "name": name,
            "raw_name": "-".join(value for value in (low, high) if value) or None,
            "length_ft": length_ft,
            "width_ft": width_ft,
            "length_m": round(length_ft * 0.3048) if length_ft is not None else None,
            "width_m": round(width_ft * 0.3048) if width_ft is not None else None,
            "surface": optional_text(raw.get("surface")),
            "lighted": (
                bool(optional_int(raw.get("lighted")))
                if str(raw.get("lighted", "")).strip()
                else None
            ),
            "closed": (
                bool(optional_int(raw.get("closed")))
                if str(raw.get("closed", "")).strip()
                else None
            ),
            "low_end": {
                "ident": low,
                "latitude_deg": optional_float(raw.get("le_latitude_deg")),
                "longitude_deg": optional_float(raw.get("le_longitude_deg")),
                "elevation_ft": optional_int(raw.get("le_elevation_ft")),
                "heading_deg": optional_float(raw.get("le_heading_degT")),
            },
            "high_end": {
                "ident": high,
                "latitude_deg": optional_float(raw.get("he_latitude_deg")),
                "longitude_deg": optional_float(raw.get("he_longitude_deg")),
                "elevation_ft": optional_int(raw.get("he_elevation_ft")),
                "heading_deg": optional_float(raw.get("he_heading_degT")),
            },
        }

    def record(self, icao: str) -> dict[str, Any] | None:
        raw = self.airports.get(icao)
        if raw is None:
            return None
        country_code = optional_text(raw.get("iso_country"))
        region_code = optional_text(raw.get("iso_region"))
        country = self.countries.get(country_code or "", {})
        region = self.regions.get(region_code or "", {})
        elevation_ft = optional_int(raw.get("elevation_ft"))
        runways = self.runways.get(icao, [])
        location_parts = [
            optional_text(raw.get("municipality")),
            optional_text(region.get("name")),
            optional_text(country.get("name")),
        ]
        return {
            "scraper_version": 2,
            "_id": icao,
            "icao_code": icao,
            "iata_code": optional_text(raw.get("iata_code")),
            "name": optional_text(raw.get("name")),
            "facility_type": optional_text(raw.get("type")),
            "airline_service": optional_text(raw.get("scheduled_service")),
            "latitude_deg": optional_float(raw.get("latitude_deg")),
            "longitude_deg": optional_float(raw.get("longitude_deg")),
            "elevation_ft": elevation_ft,
            "elevation_m": (
                round(elevation_ft * 0.3048) if elevation_ft is not None else None
            ),
            "location_text": ", ".join(part for part in location_parts if part) or None,
            "country_code": country_code,
            "country_name": optional_text(country.get("name")),
            "region_code": region_code,
            "region_name": optional_text(region.get("name")),
            "web_site": optional_text(raw.get("home_link")),
            "wikipedia_page": optional_text(raw.get("wikipedia_link")),
            "keywords": optional_text(raw.get("keywords")),
            "tags": [],
            "last_updated": None,
            "facility_fields": raw,
            "runways": runways,
            "runways_count": len(runways),
            "source_url": str(DEFAULT_INPUT),
            "airport_url": f"{BASE_URL}/airports/{icao}/",
            "runways_url": f"{BASE_URL}/airports/{icao}/runways.html",
            "source_type": "LOCAL_CSV",
            "scraped_at": now_iso(),
        }


