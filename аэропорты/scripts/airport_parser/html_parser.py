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



from .config import BASE_URL, FIELD_NAMES, RUNWAY_DIM_RE, RUNWAY_NAME_RE, clean_icao, normalize_spaces, now_iso

def facility_table(soup: BeautifulSoup) -> Tag | None:
    for table in soup.find_all("table"):
        labels = {
            normalize_spaces(th.get_text(" ", strip=True)).lower()
            for th in table.find_all("th")
        }
        if {"name", "icao code", "coordinates"}.issubset(labels):
            return table
    return None


def parse_facility_cells(soup: BeautifulSoup) -> tuple[dict[str, str], dict[str, Tag]]:
    table = facility_table(soup)
    if table is None:
        raise ValueError("Facility data table was not found")

    values: dict[str, str] = {}
    cells: dict[str, Tag] = {}
    for row in table.find_all("tr"):
        heading = row.find("th")
        cell = row.find("td")
        if not heading or not cell:
            continue
        label = normalize_spaces(heading.get_text(" ", strip=True)).lower()
        key = FIELD_NAMES.get(label)
        if not key:
            continue
        values[key] = normalize_spaces(cell.get_text(" ", strip=True))
        cells[key] = cell
    return values, cells


def first_absolute_link(cell: Tag | None) -> str | None:
    if not cell:
        return None
    link = cell.find("a", href=True)
    if not link:
        return None
    href = str(link.get("href", "")).strip()
    return urljoin(BASE_URL, href) if href else None


def parse_location_links(cell: Tag | None) -> dict[str, str | None]:
    result: dict[str, str | None] = {
        "country_code": None,
        "country_name": None,
        "region_code": None,
        "region_name": None,
    }
    if not cell:
        return result

    for link in cell.find_all("a", href=True):
        href = str(link.get("href", ""))
        text = normalize_spaces(link.get_text(" ", strip=True)) or None
        match = re.fullmatch(r"/countries/([A-Z]{2})(?:/([A-Z0-9-]+))?/?", href)
        if not match:
            continue
        country, subdivision = match.groups()
        if subdivision:
            result["region_code"] = f"{country}-{subdivision}"
            result["region_name"] = text
        else:
            result["country_code"] = country
            result["country_name"] = text
    return result


def parse_coordinates(value: str | None) -> tuple[float | None, float | None]:
    if not value:
        return None, None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", value)
    if not match:
        return None, None
    latitude, longitude = float(match.group(1)), float(match.group(2))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None, None
    return latitude, longitude


def parse_elevation(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None

    def integer(pattern: str) -> int | None:
        match = re.search(pattern, value, re.IGNORECASE)
        return int(match.group(1).replace(",", "")) if match else None

    return integer(r"(-?[\d,]+)\s*ft"), integer(r"/\s*(-?[\d,]+)\s*m")


def parse_tags(cell: Tag | None) -> list[str]:
    if not cell:
        return []
    tags = []
    for link in cell.find_all("a", href=True):
        if str(link.get("href", "")).startswith("/tags/"):
            value = normalize_spaces(link.get_text(" ", strip=True))
            if value:
                tags.append(value)
    return list(dict.fromkeys(tags))


def parse_facility_page(html: str, requested_icao: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    fields, cells = parse_facility_cells(soup)
    parsed_icao = clean_icao(fields.get("icao_code"))
    if parsed_icao != requested_icao:
        raise ValueError(
            f"page ICAO mismatch: requested {requested_icao}, parsed {parsed_icao or 'none'}"
        )

    latitude, longitude = parse_coordinates(fields.get("coordinates"))
    elevation_ft, elevation_m = parse_elevation(fields.get("field_elevation"))
    location = parse_location_links(cells.get("location"))

    record: dict[str, Any] = {
        "scraper_version": 2,
        "_id": requested_icao,
        "icao_code": requested_icao,
        "iata_code": fields.get("iata_code") or None,
        "name": fields.get("name") or None,
        "facility_type": fields.get("facility_type") or None,
        "airline_service": fields.get("airline_service") or None,
        "latitude_deg": latitude,
        "longitude_deg": longitude,
        "elevation_ft": elevation_ft,
        "elevation_m": elevation_m,
        "location_text": fields.get("location") or None,
        **location,
        "web_site": first_absolute_link(cells.get("web_site")),
        "wikipedia_page": first_absolute_link(cells.get("wikipedia_page")),
        "keywords": fields.get("keywords") or None,
        "tags": parse_tags(cells.get("tags")),
        "last_updated": fields.get("last_updated") or None,
        "facility_fields": fields,
    }
    if not record["name"]:
        raise ValueError("airport name is empty")
    return record


def parse_runway_section(section: Tag) -> dict[str, Any] | None:
    bold = section.find("b")
    raw_name = normalize_spaces(bold.get_text(" ", strip=True)) if bold else ""
    if not RUNWAY_NAME_RE.fullmatch(raw_name):
        return None

    text = normalize_spaces(section.get_text(" ", strip=True))
    dimensions = RUNWAY_DIM_RE.search(text)
    surface_match = re.search(r"\bSurface\s+(.+?)(?:\.|$)", text, re.IGNORECASE)
    surface_text = surface_match.group(1).strip(" ,") if surface_match else ""
    lower_surface = surface_text.lower()

    lighted: bool | None = None
    if re.search(r"\b(?:not\s+lighted|unlighted)\b", lower_surface):
        lighted = False
    elif re.search(r"\blighted\b", lower_surface):
        lighted = True

    surface = re.sub(
        r",?\s*\b(?:not\s+lighted|unlighted|lighted)\b.*$",
        "",
        surface_text,
        flags=re.IGNORECASE,
    ).strip(" ,.")

    def dimension(name: str) -> int | None:
        if not dimensions:
            return None
        return int(dimensions.group(name).replace(",", ""))

    return {
        "name": raw_name.replace("-", "/"),
        "raw_name": raw_name,
        "length_ft": dimension("length_ft"),
        "width_ft": dimension("width_ft"),
        "length_m": dimension("length_m"),
        "width_m": dimension("width_m"),
        "surface": surface or None,
        "lighted": lighted,
    }


def parse_runways_page(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    parsed = [
        runway
        for section in soup.select("section.runway")
        if (runway := parse_runway_section(section)) is not None
    ]

    # Fallback for older/simpler page layouts.
    if not parsed:
        strings = [normalize_spaces(value) for value in soup.stripped_strings]
        for index, value in enumerate(strings):
            if not RUNWAY_NAME_RE.fullmatch(value):
                continue
            fragment = " ".join(strings[index : index + 4])
            fake = BeautifulSoup(
                f"<section><b>{value}</b><p>{fragment}</p></section>",
                "html.parser",
            ).section
            if fake and (runway := parse_runway_section(fake)):
                parsed.append(runway)

    unique: dict[str, dict[str, Any]] = {}
    for runway in parsed:
        unique[runway["name"]] = runway
    return list(unique.values())


def parse_airport_document(
    html: str,
    icao: str,
    source_url: str,
    airport_url: str,
    runways_url: str,
) -> dict[str, Any]:
    record = parse_facility_page(html, icao)
    runways = parse_runways_page(html)
    record.update(
        {
            "runways": runways,
            "runways_count": len(runways),
            "source_url": source_url,
            "airport_url": airport_url,
            "runways_url": runways_url,
            "source_type": "HTML",
            "scraped_at": now_iso(),
        }
    )
    return record


