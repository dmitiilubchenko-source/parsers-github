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



from .config import ROOT, load_icaos, now_iso, parse_args
from .local_data import LocalCsvDataset
from .downloader import Downloader
from .output import load_previous_outputs, save_checkpoint, save_outputs
from .pipeline import process_one

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    icaos = load_icaos(args.input, args.icao_column, args.icao)

    if args.limit is not None:
        icaos = icaos[: max(0, args.limit)]

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.resume:
        records, statuses = load_previous_outputs(args.output_dir)
    else:
        records, statuses = {}, {}

    # A previous run can be interrupted between writing the JSON records and
    # writing the status CSV. The record itself is authoritative in that case.
    for icao, record in records.items():
        status = statuses.get(icao)
        if status is not None and not bool(status.get("success")):
            status.update(
                {
                    "success": True,
                    "skipped": False,
                    "from_cache": False,
                    "source_url": record.get("source_url", ""),
                    "http_status": "",
                    "parsed_runways": record.get("runways_count", ""),
                    "error": "Recovered from saved record after interrupted output write",
                    "processed_at": now_iso(),
                    "source_type": record.get("source_type", ""),
                }
            )

    completed = set(records)
    pending = [icao for icao in icaos if icao not in completed]
    downloader = Downloader(
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        backoff=args.backoff,
        refresh=args.refresh,
        user_agent=args.user_agent,
    )
    local_data = (
        LocalCsvDataset(ROOT, args.icao_column)
        if args.local_fallback or args.local_only
        else None
    )

    print("=" * 72)
    print("OURAIRPORTS HTML SCRAPER v2")
    print("=" * 72)
    print(f"Selected ICAO codes: {len(icaos):,}")
    print(f"Already completed:   {len(icaos) - len(pending):,}")
    print(f"Pending this run:    {len(pending):,}")
    print(f"Request delay:       {args.delay:.1f}s")
    print(f"Local CSV fallback:  {'enabled' if local_data else 'disabled'}")
    print(f"Output directory:    {args.output_dir}")

    save_every = max(1, args.save_every)
    checkpoint_every = max(1, args.checkpoint_every)
    checkpoint_rows: list[dict[str, Any]] = []
    processed_count = 0

    try:
        for sequence, icao in enumerate(pending, start=1):
            processed_count = sequence
            print(f"[{sequence:>6}/{len(pending):,}] {icao}")
            record, status = process_one(
                icao,
                downloader,
                args.cache_dir,
                local_data=local_data,
                local_only=args.local_only,
            )
            status["sequence"] = sequence
            statuses[icao] = status
            checkpoint_rows.append(status)

            if record is not None:
                records[icao] = record
                source = status.get("source_type") or (
                    "CACHE" if status["from_cache"] else "HTML"
                )
                print(f"    OK: {source}, {record['runways_count']} runway(s)")
            else:
                print(f"    FAILED: {status['error']}")

            if sequence % save_every == 0:
                save_outputs(args.output_dir, records, statuses)
            if sequence % checkpoint_every == 0:
                save_checkpoint(args.output_dir, sequence, checkpoint_rows)
                checkpoint_rows = []

    except KeyboardInterrupt:
        print("\nInterrupted: saving completed work before exit...")
        save_outputs(args.output_dir, records, statuses)
        save_checkpoint(args.output_dir, processed_count, checkpoint_rows)
        return 130

    save_outputs(args.output_dir, records, statuses)
    if checkpoint_rows:
        save_checkpoint(args.output_dir, len(pending), checkpoint_rows)

    succeeded = sum(icao in records for icao in icaos)
    failed = sum(
        icao in statuses and not bool(statuses[icao].get("success")) for icao in icaos
    )
    print("=" * 72)
    print(f"DONE: {succeeded:,} successful, {failed:,} failed")
    print(f"JSON:   {args.output_dir / 'airports_html.json'}")
    print("Use sort_airports.py to create filtered JSON, CSV or NDJSON files.")
    return 0 if failed == 0 else 2


