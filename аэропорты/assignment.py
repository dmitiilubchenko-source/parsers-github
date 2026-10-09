"""python assignment.py: source airports with valid ICAO codes."""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from scripts.airport_parser.config import detect_icao_column, clean_icao, optional_float, optional_int, now_iso
from scripts.airport_parser.local_data import LocalCsvDataset
from scripts.airport_parser.enrichment import enrich

ROOT = Path(__file__).resolve().parent
FIELDS = ('airport_id', 'ident', 'icao_code', 'name', 'latitude_deg', 'longitude_deg',
          'status', 'type', 'country', 'elevation_ft', 'timezone', 'runways_count',
          'runways', 'source_url', 'details_source', 'status_source', 'timezone_source')


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def without_nulls(value):
    if isinstance(value, dict):
        return {k: without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [without_nulls(v) for v in value]
    return value


def export(root=ROOT, formats=('csv', 'json'), on_progress=print):
    raw = read_csv(root / 'data/airports.csv')
    icao_column = detect_icao_column(raw[0], 'icao_code')
    html_path = root / 'outputs/html/airports_html.json'
    html = json.loads(html_path.read_text(encoding='utf-8-sig')) if html_path.exists() else []
    html_by_icao = {r['icao_code']: r for r in html}
    country = {r['code']: r['name'] for r in read_csv(root / 'data/countries.csv')}
    runways = {}
    for r in read_csv(root / 'data/runways.csv'):
        runways.setdefault(r['airport_ref'], []).append(LocalCsvDataset._runway(r))
    records, issues = [], []
    seen_ids = set()
    for index, source in enumerate(raw, 1):
        airport_id = source['id']
        if airport_id in seen_ids:
            raise ValueError(f'Duplicate source airport ID: {airport_id}')
        seen_ids.add(airport_id)
        icao = clean_icao(source.get(icao_column)) or None
        if icao is None:
            continue
        detail = html_by_icao.get(icao, {})
        # The complete runway inventory comes from the source table. HTML can
        # enrich matching runway fields, but never remove rows from that table.
        strips = [dict(r) for r in runways.get(airport_id, [])]
        html_strips = {r.get('name'): r for r in detail.get('runways', [])}
        for strip in strips:
            for key in ('name', 'length_ft', 'surface'):
                if strip.get(key) is None:
                    strip[key] = html_strips.get(strip.get('name'), {}).get(key)
        record = dict(airport_id=airport_id, ident=source['ident'], icao_code=icao,
                      name=detail.get('name') or source['name'],
                      latitude_deg=optional_float(source['latitude_deg']),
                      longitude_deg=optional_float(source['longitude_deg']),
                      facility_type=source['type'], type=source['type'],
                      country=country.get(source['iso_country']) or source['iso_country'],
                      elevation_ft=optional_int(source['elevation_ft']),
                      runways_count=len(strips), runways=strips,
                      source_url=f"https://ourairports.com/airports/{source['ident']}/",
                      details_source=detail.get('source_type') or 'LOCAL_CSV')
        enrich(record)
        record.pop('facility_type')
        missing = [k for k in ('icao_code', 'latitude_deg', 'longitude_deg', 'elevation_ft', 'timezone')
                   if record.get(k) is None]
        for n, strip in enumerate(strips, 1):
            missing.extend(f'runway_{n}.{k}' for k in ('name', 'length_ft', 'surface') if strip.get(k) is None)
        if missing:
            issues.append({'airport_id': airport_id, 'ident': source['ident'], 'missing': '; '.join(missing)})
        records.append(record)
        if index % 10000 == 0 and on_progress:
            on_progress(f'Airports: {index}/{len(raw)}')
    output = root / 'outputs/assignment'
    output.mkdir(parents=True, exist_ok=True)
    for fmt in formats:
        target = output / f'airports.{fmt}'
        temp = target.with_suffix('.tmp')
        if fmt == 'csv':
            with temp.open('w', encoding='utf-8-sig', newline='') as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                for r in records:
                    w.writerow({**r, 'runways': json.dumps(r['runways'], ensure_ascii=False)})
        elif fmt == 'json':
            temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        elif fmt == 'yaml':
            import yaml
            temp.write_text(yaml.safe_dump(records, allow_unicode=True, sort_keys=False), encoding='utf-8')
        elif fmt == 'toml':
            import tomli_w
            temp.write_text(tomli_w.dumps({'airports': without_nulls(records)}), encoding='utf-8')
        else:
            raise ValueError(fmt)
        temp.replace(target)
    with (output / 'missing_source_fields.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=('airport_id', 'ident', 'missing'))
        w.writeheader()
        w.writerows(issues)
    summary = dict(generated_at=now_iso(), source_records=len(raw), exported_records=len(records),
                   excluded_without_valid_icao=len(raw) - len(records),
                   types=dict(Counter(r['type'] for r in records)),
                   statuses=dict(Counter(r['status'] for r in records)),
                   missing_icao=sum(r['icao_code'] is None for r in records),
                   missing_timezone=sum(r['timezone'] is None for r in records),
                   records_with_source_gaps=len(issues), formats=list(formats),
                   scope='Only records with valid ICAO codes in the supplied OurAirports snapshot, including closed facilities; no claim of independent world census.',
                   null_policy='Absent source fields are null/empty, never invented. TOML omits null-valued keys.',
                   timezone_method='IANA timezone inferred from coordinates, not UTC offset.')
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    # Enrich the existing HTML result too, keeping its original scope.
    type_by_icao = {clean_icao(s.get(icao_column)): s['type'] for s in raw if clean_icao(s.get(icao_column))}
    for r in html:
        enrich(r, type_by_icao.get(r['icao_code']))
    if html:
        temp = html_path.with_suffix('.tmp')
        temp.write_text(json.dumps(html, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(html_path)
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Airports with valid ICAO codes, status and IANA timezone')
    p.add_argument('--formats', nargs='+', choices=('csv', 'json', 'toml', 'yaml'), default=['csv', 'json'])
    args = p.parse_args()
    print(json.dumps(export(formats=args.formats), ensure_ascii=False, indent=2))
