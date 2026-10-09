"""Completeness rules shared by the standalone sorting command."""
from __future__ import annotations

from typing import Any

AIRPORT_TYPES = {"small_airport", "medium_airport", "large_airport"}
REQUIRED_AIRPORT_FIELDS = (
    "_id", "icao_code", "name", "facility_type", "latitude_deg",
    "longitude_deg", "elevation_ft", "country_code", "country_name",
    "region_code", "region_name",
)
REQUIRED_RUNWAY_FIELDS = ("name", "length_ft", "width_ft", "surface", "lighted")


def has_value(value: Any) -> bool:
    return value is not None and value != "" and value != []


def is_complete_airport(record: dict[str, Any]) -> bool:
    if record.get("facility_type") not in AIRPORT_TYPES:
        return False
    if any(not has_value(record.get(field)) for field in REQUIRED_AIRPORT_FIELDS):
        return False
    latitude = record.get("latitude_deg")
    longitude = record.get("longitude_deg")
    if not isinstance(latitude, (int, float)) or not -90 <= latitude <= 90:
        return False
    if not isinstance(longitude, (int, float)) or not -180 <= longitude <= 180:
        return False
    runways = record.get("runways")
    return isinstance(runways, list) and bool(runways) and all(
        isinstance(runway, dict)
        and all(has_value(runway.get(field)) for field in REQUIRED_RUNWAY_FIELDS)
        for runway in runways
    )
