"""Filter the saved scrape without downloading anything."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORMATS = {"json", "csv", "ndjson"}
FACILITY_FIELDS = {
    "id", "ident", "gps_code", "local_code", "scheduled_service", "continent",
    "municipality", "location", "coordinates", "field_elevation", "home_link",
    "members", "type",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Filter saved airports into selected formats")
    parser.add_argument("--config", type=Path, default=ROOT / "filter_config.json")
    parser.add_argument("--input", type=Path, default=ROOT / "outputs/html/airports_html.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/selected")
    parser.add_argument("--formats", nargs="+", choices=sorted(FORMATS), help="Override formats from config")
    return parser.parse_args()


def clean_codes(values: object, name: str) -> set[str]:
    if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
        raise ValueError(f"{name} must be a list of ICAO strings")
    return {value.strip().upper() for value in values if value.strip()}


def has_complete_value(value: object) -> bool:
    """Require nonempty values, including nested data; zero and False are valid."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        values = value.values() if isinstance(value, dict) else value
        return bool(value) and all(has_complete_value(item) for item in values)
    return True


def select(records: list[dict], config: dict, routes_by_icao: dict | None = None) -> list[dict]:
    allowed = clean_codes(config.get("include_icao", []), "include_icao")
    excluded = clean_codes(config.get("exclude_icao", []), "exclude_icao")
    types = set(config.get("airport_types", []))
    fields = config.get("fields")
    if not isinstance(fields, list) or not fields or any(not isinstance(x, str) for x in fields):
        raise ValueError("fields must be a nonempty list of field names")
    if len(fields) != len(set(fields)):
        raise ValueError("fields contains duplicates")
    result = []
    seen = set()
    for record in records:
        code = str(record.get("icao_code") or "").strip().upper()
        if not code or code in seen or code in excluded:
            continue
        if allowed and code not in allowed:
            continue
        if types and record.get("facility_type") not in types:
            continue
        facility = record.get("facility_fields") or {}
        row = {}
        for field in fields:
            if field == "routes":
                row[field] = (routes_by_icao or {}).get(code, [])
            elif field == "runway_surfaces":
                row[field] = [runway.get("surface") for runway in record.get("runways", [])]
            elif field == "type":
                row[field] = facility.get("type") or record.get("facility_type")
            elif field in FACILITY_FIELDS:
                row[field] = facility.get(field)
            else:
                row[field] = record.get(field)
        if config.get("require_complete", True) and not all(has_complete_value(value) for value in row.values()):
            continue
        seen.add(code)
        result.append(row)
    return sorted(result, key=lambda row: str(row.get("icao_code") or row.get("_id") or ""))


def write_outputs(rows: list[dict], output_dir: Path, formats: set[str], fields: list[str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for fmt in FORMATS - formats:
        (output_dir / f"airports.{fmt}").unlink(missing_ok=True)
    if "json" in formats:
        (output_dir / "airports.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if "ndjson" in formats:
        with (output_dir / "airports.ndjson").open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    if "csv" in formats:
        with (output_dir / "airports.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items()})


def main() -> int:
    args = arguments()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    formats = set(args.formats or config.get("formats", []))
    if not formats or formats - FORMATS:
        raise SystemExit("formats must contain json, csv, and/or ndjson")
    payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise SystemExit("Input must be a JSON array of airport objects")
    routes_by_icao = {}
    if "routes" in config["fields"]:
        route_file = ROOT / "data/airline_routes.json"
        route_payload = json.loads(route_file.read_text(encoding="utf-8-sig"))
        routes_by_icao = {
            value["icao"].strip().upper(): value.get("routes") or []
            for value in route_payload.values()
            if isinstance(value, dict) and isinstance(value.get("icao"), str)
        }
    rows = select(payload, config, routes_by_icao)
    if config.get("require_complete", True):
        unchecked = select(payload, {**config, "require_complete": False}, routes_by_icao)
        missing = {field: sum(not has_complete_value(row[field]) for row in unchecked) for field in config["fields"]}
        print("Incomplete selected fields: " + ", ".join(f"{field}={count}" for field, count in missing.items() if count))
    write_outputs(rows, args.output_dir, formats, config["fields"])
    print(f"Selected {len(rows)} of {len(payload)} airports -> {args.output_dir}")
    print("Formats: " + ", ".join(sorted(formats)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
