

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


def project_root() -> Path:
    script_dir = Path(__file__).resolve().parent
    for candidate in (script_dir.parent.parent, script_dir.parent, script_dir, Path.cwd().resolve()):
        if (candidate / "data" / "airports.csv").exists():
            return candidate
    return script_dir.parent


ROOT = project_root()
DEFAULT_INPUT = ROOT / "data" / "airports.csv"
DEFAULT_CACHE = ROOT / "cache" / "html"
DEFAULT_OUTPUT = ROOT / "outputs" / "html"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download and parse ordinary OurAirports HTML pages for ICAO codes. "
            "Produces CSV, JSON and MongoDB-ready NDJSON."
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--icao-column", default="icao_code")
    parser.add_argument(
        "--icao",
        action="append",
        help="Process one ICAO code. May be supplied more than once.",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Deprecated compatibility flag; all ICAO codes are processed by default.",
    )
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--backoff", type=float, default=5.0)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help=(
            "Do not request HTML for pending ICAO codes; build records from the "
            "local OurAirports CSV files instead. Useful after an HTML run."
        ),
    )
    parser.add_argument(
        "--no-local-fallback",
        dest="local_fallback",
        action="store_false",
        help="Do not use local CSV data when both HTML pages are unusable.",
    )
    parser.set_defaults(local_fallback=True)
    parser.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Ignore previous JSON/status outputs and start a new result set.",
    )
    parser.set_defaults(resume=True)
    parser.add_argument("--save-every", type=int, default=250)
    parser.add_argument("--checkpoint-every", type=int, default=1000)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--user-agent",
        default="WorldAirportsStudentProject/2.0 (polite HTML scraper)",
    )
    return parser.parse_args(argv)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def clean_icao(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if ICAO_RE.fullmatch(text) else ""


def detect_icao_column(columns: Iterable[Any], requested: str) -> Any:
    columns = list(columns)
    if requested in columns:
        return requested

    matches: list[Any] = []
    for column in columns:
        raw = str(column).strip().strip("\"'")
        lowered = raw.lower()
        ascii_compact = re.sub(
            r"[^a-z_]", "", lowered.encode("ascii", errors="ignore").decode()
        )
        if (
            lowered in {"icao", "icao_code"}
            or (lowered.startswith("ica") and lowered.endswith("o_code"))
            or ascii_compact == "icao_code"
        ):
            matches.append(column)

    matches = list(dict.fromkeys(matches))
    if len(matches) != 1:
        raise SystemExit(
            f"ICAO column {requested!r} was not found uniquely. "
            f"Candidates: {matches}; columns: {columns}"
        )
    return matches[0]


def load_icaos(
    path: Path,
    requested_column: str,
    explicit: list[str] | None,
) -> list[str]:
    if explicit:
        values = [clean_icao(item) for item in explicit]
        invalid = [raw for raw, clean in zip(explicit, values) if not clean]
        if invalid:
            raise SystemExit(f"Invalid ICAO code(s): {', '.join(invalid)}")
        return list(dict.fromkeys(values))

    if not path.exists():
        raise SystemExit(f"Input CSV not found: {path}")

    frame = pd.read_csv(path, low_memory=False, keep_default_na=False)
    column = detect_icao_column(frame.columns, requested_column)
    if column != requested_column:
        print(f"ICAO column auto-detected as {column!r}")

    return sorted(
        {
            icao
            for icao in (clean_icao(value) for value in frame[column])
            if icao
        }
    )


def optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def optional_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def optional_int(value: Any) -> int | None:
    number = optional_float(value)
    return int(number) if number is not None else None


