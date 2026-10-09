"""Combine this collection's main pass and alcohol retry after both finish."""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from categories import classify
from history import read_snapshot, save_run
from metro_parser import store_identity

ROOT = Path(__file__).resolve().parent


def finalize(paths):
    reports = [json.loads(p.with_suffix('.json').read_text(encoding='utf-8')) for p in paths]
    excluded = {'https://online.metro-cc.ru/category/zony-brendov-50961'}
    categories = {u: n for report in reports for u, n in report['categories'].items() if u not in excluded}
    complete = {u for report in reports for u in report['complete_categories'] if u not in excluded}
    errors, recovered = [], []
    for report in reports:
        for error in report.get('errors', []):
            url = error.split(': ', 1)[0].split('?', 1)[0]
            (recovered if url in complete or url in excluded else errors).append(error)
    products = {}
    for path in paths:
        for row in read_snapshot(path).to_dict('records'):
            row['store_id'] = store_identity(row['store_address'])
            row['regular_price'] = float(row['regular_price']) if row['regular_price'] else None
            row['section'], row['category'], row['category_rule'] = classify(row['name'], row['source_category'])
            key = row['store_id'], row['product_id']
            prior = products.get(key)
            if prior is None or (row['source_category_url'].count('/'), row['collected_at']) > (prior['source_category_url'].count('/'), prior['collected_at']):
                products[key] = row
    rows = list(products.values())
    if not rows or len({r['store_id'] for r in rows}) != 1:
        raise ValueError('Нет данных или несколько торговых центров: объединение отменено')
    missing = sorted(set(categories) - complete)
    if missing:
        errors.append('Не завершены категории: ' + ', '.join(missing))
    run_id = datetime.now().astimezone().strftime('%Y-%m-%d_%H-%M-%S_%f')
    for row in rows:
        row['run_id'] = run_id
    report = dict(run_id=run_id, mode='full', status='partial' if errors else 'complete',
                  categories=categories, complete_categories=sorted(complete), errors=errors,
                  warnings=[w for r in reports for w in r.get('warnings', [])],
                  pages=[page for r in reports for page in r['pages']],
                  source_snapshots=[str(p) for p in paths], recovered_errors=recovered,
                  excluded_navigation={next(iter(excluded)): 'Перенаправляет в рекламный раздел promo/brandzones'},
                  store_id=rows[0]['store_id'], store_address=rows[0]['store_address'],
                  products=len(rows), products_with_regular_price=sum(r['regular_price'] is not None for r in rows))
    result = save_run(ROOT / 'output', run_id, rows, report)
    (ROOT / 'output' / 'collection_result.txt').write_text(
        f"CSV: {result}\nТоваров: {len(rows)}\nС обычной ценой: {report['products_with_regular_price']}\nСтатус: {report['status']}\n", encoding='utf-8')
    print(result, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--wait', action='store_true')
    args = parser.parse_args()
    paths = [ROOT / 'output' / f'prices_{run}.csv' for run in
             ('2026-10-05_11-49-12_582093', '2026-10-05_12-07-45_477602')]
    while not all(p.exists() and p.with_suffix('.json').exists() for p in paths):
        if not args.wait:
            raise SystemExit('Оба исходных сбора ещё не завершены')
        time.sleep(10)
    finalize(paths)
