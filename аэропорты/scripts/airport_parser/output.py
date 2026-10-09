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



from .config import CSV_COLUMNS, STATUS_COLUMNS, clean_icao, now_iso
from .downloader import FetchResult, atomic_write_text

def csv_row(record: dict[str, Any]) -> dict[str, Any]:
    runways = record.get("runways") or []
    row = {key: record.get(key) for key in CSV_COLUMNS}
    row["tags"] = json.dumps(record.get("tags") or [], ensure_ascii=False)
    row["runway_names"] = json.dumps(
        [item.get("name") for item in runways if item.get("name")],
        ensure_ascii=False,
    )
    row["runway_lengths_ft"] = json.dumps(
        [item.get("length_ft") for item in runways if item.get("length_ft") is not None],
        ensure_ascii=False,
    )
    row["runways"] = json.dumps(runways, ensure_ascii=False, separators=(",", ":"))
    return row


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def save_outputs(
    output_dir: Path,
    records_by_icao: dict[str, dict[str, Any]],
    status_by_icao: dict[str, dict[str, Any]],
) -> None:
    from .enrichment import enrich
    for record in records_by_icao.values():
        enrich(record)
    records = [records_by_icao[key] for key in sorted(records_by_icao)]
    statuses = [status_by_icao[key] for key in sorted(status_by_icao)]
    output_dir.mkdir(parents=True, exist_ok=True)

    atomic_write_text(
        output_dir / "airports_html.json",
        json.dumps(records, ensure_ascii=False, indent=2),
    )
    write_csv(output_dir / "html_scrape_status.csv", statuses, STATUS_COLUMNS)

    successful = sum(bool(item.get("success")) for item in statuses)
    summary = {
        "schema_version": 2,
        "generated_at": now_iso(),
        "records": len(records),
        "status_rows": len(statuses),
        "successful": successful,
        "failed": len(statuses) - successful,
        "formats": {
            "json": "airports_html.json",
            "status": "html_scrape_status.csv",
        },
    }
    atomic_write_text(
        output_dir / "html_scrape_summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2),
    )


def load_previous_outputs(
    output_dir: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    records: dict[str, dict[str, Any]] = {}
    statuses: dict[str, dict[str, Any]] = {}

    summary_path = output_dir / "html_scrape_summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        summary = {}
    if summary.get("schema_version") != 2:
        if (output_dir / "airports_html.json").exists():
            print("Previous scraper output uses an older schema; rebuilding it.")
        return records, statuses

    json_path = output_dir / "airports_html.json"
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict) and (icao := clean_icao(item.get("icao_code"))):
                        item.setdefault("_id", icao)
                        records[icao] = item
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Previous JSON could not be loaded: {exc}")

    status_path = output_dir / "html_scrape_status.csv"
    if status_path.exists():
        try:
            with status_path.open("r", encoding="utf-8-sig", newline="") as handle:
                for item in csv.DictReader(handle):
                    if icao := clean_icao(item.get("icao_code")):
                        item["success"] = str(item.get("success", "")).lower() == "true"
                        item["skipped"] = str(item.get("skipped", "")).lower() == "true"
                        item["from_cache"] = (
                            str(item.get("from_cache", "")).lower() == "true"
                        )
                        statuses[icao] = item
        except OSError as exc:
            print(f"Previous status CSV could not be loaded: {exc}")
    return records, statuses


def save_checkpoint(
    output_dir: Path,
    sequence: int,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        return
    columns = list(dict.fromkeys(STATUS_COLUMNS + ["sequence"]))
    name = f"status_{sequence - len(rows) + 1:06d}_{sequence:06d}.csv"
    write_csv(output_dir / "checkpoints" / name, rows, columns)


def status_row(
    icao: str,
    *,
    success: bool,
    skipped: bool = False,
    fetch: FetchResult | None = None,
    source_url: str = "",
    parsed_runways: int | str = "",
    error: str = "",
    source_type: str = "HTML",
) -> dict[str, Any]:
    return {
        "icao_code": icao,
        "success": success,
        "skipped": skipped,
        "from_cache": fetch.from_cache if fetch else "",
        "source_url": source_url,
        "http_status": fetch.status_code if fetch else "",
        "parsed_runways": parsed_runways,
        "error": error,
        "processed_at": now_iso(),
        "source_type": source_type,
    }


