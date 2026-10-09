"""A dedicated CSV with numbered party protocol fields, one row per UIK."""
import csv
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .collector import CATALOG, save_report
from .config import BASE_DIR, RAW_DIR
from .results import PARTY_PROTOCOL, parse_result, result_api_url
from .tree import UikNode

# Source: Appendix 4, Rosstat order 519 (2025), federal district territories.
DISTRICT_SOURCE = 'https://rosstat.gov.ru/storage/mediabank/приказ%20№%20519_5.5.2.pdf'
DISTRICTS = {
    'Центральный': 'Белгородская область|Брянская область|Владимирская область|Воронежская область|Ивановская область|Калужская область|Костромская область|Курская область|Липецкая область|Московская область|Орловская область|Рязанская область|Смоленская область|Тамбовская область|Тверская область|Тульская область|Ярославская область|Москва',
    'Северо-Западный': 'Республика Карелия|Республика Коми|Архангельская область|Вологодская область|Калининградская область|Ленинградская область|Мурманская область|Новгородская область|Псковская область|Санкт-Петербург|Ненецкий автономный округ',
    'Южный': 'Республика Адыгея|Республика Калмыкия|Республика Крым|Краснодарский край|Астраханская область|Волгоградская область|Ростовская область|Севастополь',
    'Северо-Кавказский': 'Республика Дагестан|Республика Ингушетия|Кабардино-Балкарская Республика|Карачаево-Черкесская Республика|Республика Северная Осетия|Чеченская Республика|Ставропольский край',
    'Приволжский': 'Республика Башкортостан|Республика Марий Эл|Республика Мордовия|Республика Татарстан|Удмуртская Республика|Чувашская Республика|Пермский край|Кировская область|Нижегородская область|Оренбургская область|Пензенская область|Самарская область|Саратовская область|Ульяновская область',
    'Уральский': 'Курганская область|Свердловская область|Тюменская область|Челябинская область|Ханты-Мансийский автономный округ|Ямало-Ненецкий автономный округ',
    'Сибирский': 'Республика Алтай|Республика Тыва|Республика Хакасия|Алтайский край|Красноярский край|Иркутская область|Кемеровская область|Новосибирская область|Омская область|Томская область',
    'Дальневосточный': 'Республика Бурятия|Республика Саха|Забайкальский край|Камчатский край|Приморский край|Хабаровский край|Амурская область|Магаданская область|Сахалинская область|Еврейская автономная область|Чукотский автономный округ',
}


def federal_district(region):
    normalized = re.sub(r'^(?:город|г\.)\s+', '', region, flags=re.I)
    for district, regions in DISTRICTS.items():
        for candidate in regions.split('|'):
            if normalized == candidate or normalized.startswith(candidate + ' ') or normalized.startswith(candidate + '-'):
                return district + ' федеральный округ'
    # Special branches outside the confirmed district list remain explicit.
    return 'Вне подтверждённого перечня федеральных округов'


BASE_COLUMNS = ('Федеральный округ', 'ИКСРФ', 'ОИК', 'ТИК', 'УИК', 'Номер УИК',
                'ID УИК', 'Путь комиссий', 'ID протокола', 'Подписан', 'Дата подписания',
                'Статус данных', 'Замечание', 'Источник')
COLUMNS = BASE_COLUMNS + tuple(f'Строка {n:02d}' for n in range(1, 23))


def load_row(item, raw_dir):
    path = item['path']
    region = path[1] if len(path) > 1 else ''
    row = dict(zip(BASE_COLUMNS, ('',) * len(BASE_COLUMNS)))
    row.update({'Федеральный округ': federal_district(region), 'ИКСРФ': region,
                'ОИК': path[2] if len(path) >= 4 else '',
                'ТИК': ' / '.join(path[3:-1]) if len(path) >= 5 else '',
                'УИК': item['name'], 'Номер УИК': item['number'], 'ID УИК': item['commission_id'],
                'Путь комиссий': ' / '.join(path), 'Статус данных': 'missing',
                'Источник': result_api_url(item['commission_id'], PARTY_PROTOCOL)})
    row.update({f'Строка {n:02d}': None for n in range(1, 23)})
    labels = {}
    file = raw_dir / f"party_{item['commission_id']}_242.json"
    try:
        payload = json.loads(file.read_text(encoding='utf-8'))['data']
        body = payload.get('body') or {}
        if body.get('commissionClassifierId') != item['commission_id'] or body.get('protocolNum') != 2:
            raise ValueError('Wrong commission or non-party protocol')
        values = {}
        for r in body.get('records') or []:
            n = str(r.get('infoPrintNum', ''))
            if n not in {str(x) for x in range(1, 23)}:
                continue
            if n in values:
                raise ValueError(f'Duplicate protocol row {n}')
            value = str(r.get('value', ''))
            if not re.fullmatch(r'\d+', value):
                raise ValueError(f'Non-numeric protocol row {n}')
            values[n] = int(value)
            labels[n] = str(r.get('infoText') or '').strip()
        row.update({f'Строка {int(n):02d}': value for n, value in values.items()})
        row['ID протокола'] = body.get('protocolId') or ''
        row['Подписан'] = body.get('signed')
        row['Дата подписания'] = body.get('signDateTime') or ''
        if set(values) != {str(n) for n in range(1, 23)}:
            row['Статус данных'] = 'incomplete'
            row['Замечание'] = 'Нет строк: ' + ', '.join(str(n) for n in range(1, 23) if str(n) not in values)
        else:
            node = UikNode(item['commission_id'], item['number'], item['name'], tuple(path))
            parse_result(payload, node, PARTY_PROTOCOL)
            row['Статус данных'] = 'complete'
    except FileNotFoundError:
        row['Замечание'] = 'Партийный протокол не загружен'
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        row['Статус данных'] = 'invalid'
        row['Замечание'] = str(exc)
    return row, labels


def retry_missing(items, raw_dir, workers=2):
    from .site_client import get_json, official_client
    pending = [r for r in items if not (raw_dir / f"party_{r['commission_id']}_242.json").exists()]
    errors = []
    if not pending:
        return {'requested': 0, 'saved': 0, 'errors': []}
    print(f'Missing party protocols: {len(pending)}', flush=True)
    with official_client() as client:
        def fetch(item):
            try:
                params = {'commissionClassifierId': item['commission_id'], 'protocolNum': 2}
                data = get_json(client, '/reports/242', params)
                body = data.get('body') or {}
                if body.get('commissionClassifierId') != item['commission_id'] or body.get('protocolNum') != 2:
                    raise ValueError('Unexpected response')
                save_report(raw_dir / f"party_{item['commission_id']}_242.json",
                            {'url': result_api_url(item['commission_id'], 2),
                             'saved_at': datetime.now(timezone.utc).isoformat(), 'data': data})
                return None
            except Exception as exc:
                return {'commission_id': item['commission_id'], 'error': str(exc)}
        for i, error in enumerate(ThreadPoolExecutor(max_workers=workers).map(fetch, pending), 1):
            if error:
                errors.append(error)
            if i % 100 == 0:
                print(f'Party retry: {i}/{len(pending)}', flush=True)
    return {'requested': len(pending), 'saved': len(pending) - len(errors), 'errors': errors}


def export(raw_dir=RAW_DIR, catalog_path=CATALOG, output_dir=None, *, retry=False, workers=4):
    output_dir = output_dir or BASE_DIR / 'data/assignment'
    items = json.loads(catalog_path.read_text(encoding='utf-8'))
    if len({i['commission_id'] for i in items}) != len(items):
        raise ValueError('Duplicate commission IDs in catalog')
    retry_report = retry_missing(items, raw_dir, min(workers, 2)) if retry else None
    rows, descriptions = [], defaultdict(set)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, (row, labels) in enumerate(pool.map(lambda item: load_row(item, raw_dir), items), 1):
            rows.append(row)
            for n, name in labels.items():
                descriptions[n].add(name)
            if index % 10000 == 0:
                print(f'Party CSV: {index}/{len(items)}', flush=True)
    protocols = Counter(r['ID протокола'] for r in rows if r['ID протокола'])
    for row in rows:
        if row['ID протокола'] and protocols[row['ID протокола']] > 1:
            row['Статус данных'] = 'invalid'
            row['Замечание'] = 'Один ID протокола у нескольких УИК'
    rows.sort(key=lambda r: (r['Федеральный округ'], r['ИКСРФ'], r['ОИК'], r['ТИК'], r['Номер УИК']))
    output_dir.mkdir(parents=True, exist_ok=True)
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(data)
        temp.replace(path)
    write(output_dir / 'party_uik_1_22.csv', rows)
    buckets = defaultdict(list)
    for row in rows:
        buckets[row['Федеральный округ']].append(row)
    for name, data in buckets.items():
        write(output_dir / 'по_федеральным_округам' / f'{name}.csv', data)
    write(output_dir / 'incomplete_uiks.csv', [r for r in rows if r['Статус данных'] != 'complete'])
    if retry_report is not None:
        (output_dir / 'retry_errors.json').write_text(json.dumps(retry_report['errors'], ensure_ascii=False, indent=2), encoding='utf-8')
        retry_report = {k: v for k, v in retry_report.items() if k != 'errors'} | {
            'error_count': len(retry_report['errors']), 'error_log': 'retry_errors.json'}
    summary = dict(generated_at=datetime.now(timezone.utc).isoformat(), catalog_uiks=len(items),
                   exported_uiks=len(rows), statuses=dict(Counter(r['Статус данных'] for r in rows)),
                   federal_districts={k: len(v) for k, v in buckets.items()}, retry=retry_report,
                   district_mapping_source=DISTRICT_SOURCE,
                   notes=['Rows include every catalog UIK, even missing reports; empty is not zero.',
                          'ИКСРФ/ОИК/ТИК names follow positions in the saved commission tree; full path retained.',
                          'Extra-territorial and unmatched regions have a separate explicit bucket.'])
    (output_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    dictionary = '# Поля партийного протокола\n\nОдна запись — один ID УИК. Строки 01–22 — печатные номера источника, не позиции массива.\n\n'
    for n in range(1, 23):
        dictionary += f'- Строка {n:02d}: ' + ' / '.join(sorted(descriptions[str(n)])) + '\n'
    dictionary += '\nПустые значения не заменяются нулём. Проверяйте «Статус данных» перед суммированием.\n'
    (output_dir / 'ПОЛЯ.md').write_text(dictionary, encoding='utf-8')
    return summary
