"""A frozen food basket, weekly kg/l observations, and a budget check."""
import argparse
import csv
import hashlib
import json
import re
import tomllib
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from normalization import normalize

ROOT = Path(__file__).resolve().parent
FOOD_SECTIONS = {'Мясо и птица', 'Рыба и морепродукты', 'Овощи и фрукты', 'Молочные продукты и яйца',
                 'Замороженные продукты', 'Готовая еда и полуфабрикаты', 'Бакалея', 'Хлеб и выпечка',
                 'Сладости и перекусы', 'Напитки', 'Особое питание'}
MROT_SOURCE = 'https://base.garant.ru/10180093/'
MROT = Decimal('27093')  # federal minimum, effective 2026-01-01
COLUMNS = ('Дата наблюдения', 'Магазин', 'ID товара', 'Раздел', 'Группа', 'Товар', 'Цена упаковки, руб',
           'Масса/объём упаковки', 'Цена за кг/л', 'Единица', 'Статус', 'Источник')


def now():
    return datetime.now(ZoneInfo('Europe/Moscow'))


def cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def candidate_pool(root=ROOT):
    from history import read_snapshot
    from categories import classify
    from metro_parser import store_identity
    candidates = {}
    paths = sorted((root / 'output').glob('prices_*.csv'))
    sources = []
    for p in paths:
        sources.extend(read_snapshot(p).to_dict('records'))
    progress = root / 'basket_progress.json'
    if progress.exists():
        sources.extend(json.loads(progress.read_text(encoding='utf-8')))
    for p in sorted((root / 'output/inflation/raw').glob('*.json')):
        sources.extend(json.loads(p.read_text(encoding='utf-8'))['rows'])
    # Only today's observations are eligible to establish a new basket.
    for r in sources:
        if str(r.get('collected_at', ''))[:10] != now().date().isoformat():
            continue
        r['store_id'] = store_identity(r['store_address'])
        key = (r['store_id'], r['product_id'])
        if key not in candidates or r['collected_at'] > candidates[key]['collected_at']:
            candidates[key] = r
    result = []
    for r in candidates.values():
        r['section'], r['category'], r['category_rule'] = classify(r['name'], r.get('source_category', ''))
        if r['section'] not in FOOD_SECTIONS or not r.get('regular_price') or r.get('availability') == 'Нет в наличии':
            continue
        r['regular_price'] = float(r['regular_price'])
        r.update(normalize(r))
        if r['normalized_price'] is not None and r['regular_price'] > 0:
            result.append(r)
    return result


def select_basket(candidates, target, tolerance=Decimal('0.05')):
    """Bounded multiple-choice search; all accepted costs are checked in cents."""
    groups = defaultdict(list)
    for r in candidates:
        groups[(r['section'], r['category'])].append(r)
    lower, upper = cents(target * (1 - tolerance)), cents(target * (1 + tolerance))
    target_cents = cents(target)
    # Keep one deterministic candidate per distinct cent price per group.
    choices = []
    for group in sorted(groups):
        prices = {}
        for r in sorted(groups[group], key=lambda r: (r['regular_price'], r['product_id'])):
            prices.setdefault(cents(r['regular_price']), r)
        choices.append([(cost, row) for cost, row in prices.items()])
    # Ruble buckets bound memory/runtime. Keep exact cent totals for every
    # retained basket and for the final ±5% check; prices are never rounded
    # in the exported dataset.
    states = {0: (0, ())}
    for items in choices:
        updated = dict(states)  # skipping a group allows maximum coverage within budget
        for total, selected in states.values():
            for cost, item in items:
                new_total = total + cost
                bucket = new_total // 100
                if new_total <= upper:
                    prior = updated.get(bucket)
                    if prior is None or len(prior[1]) < len(selected) + 1 or (len(prior[1]) == len(selected) + 1 and new_total < prior[0]):
                        updated[bucket] = (new_total, selected + (item,))
        states = updated
    valid = [(total, items) for total, items in states.values() if lower <= total <= upper]
    if not valid:
        raise ValueError('Не удалось подобрать продуктовую корзину в пределах 1/4 МРОТ ±5%; нужно собрать больше групп/цен')
    total, selected = min(valid, key=lambda x: (-len(x[1]), abs(x[0] - target_cents), x[0]))
    return list(selected), total / 100, len(groups)


def write_manifest(path, rows, mrot):
    q = lambda v: json.dumps(v, ensure_ascii=False)
    text = f'year = 2026\nmrot = {mrot}\nbudget_fraction = 0.25\ntolerance = 0.05\n'
    text += f'mrot_source = {q(MROT_SOURCE)}\nstore_id = {q(rows[0]["store_id"])}\n'
    text += f'store_address = {q(rows[0]["store_address"])}\ncreated_at = {q(now().isoformat())}\n'
    for r in rows:
        text += '\n[[items]]\n'
        for key, value in {'product_id': r['product_id'], 'section': r['section'], 'group': r['category'],
                           'name': r['name'], 'url': r['url'], 'source_category': r['source_category'],
                           'source_category_url': r['source_category_url'], 'price_unit': r['price_unit'],
                           'package': r['package'], 'normalized_unit': r['normalized_unit']}.items():
            text += f'{key} = {q(value)}\n'
        text += 'purchase_units = 1\n'
    path.write_text(text, encoding='utf-8')


def live_collect(root, args, items=None):
    from metro_parser import BASE, EXTRACT_CARDS, MetroCrawler, create_driver, observation
    from basket import DETAIL_SCRIPT, TARGETS
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from urllib.parse import urlparse
    driver = create_driver(root, args.browser, True, args.driver, args.profile or root / 'browser_profile/basket')
    run_id = now().strftime('%Y-%m-%d_%H-%M-%S_%f')
    crawler = MetroCrawler(driver, run_id=run_id, debug_dir=root/'output/debug'/run_id,
                           timeout=args.timeout, delay=0.5)
    rows, errors = [], []
    try:
        crawler.navigate(BASE)
        crawler.wait_store()
        if items:
            manifest = tomllib.loads((root / 'inflation_basket.toml').read_text(encoding='utf-8'))
            if manifest['store_id'] != __import__('metro_parser').store_identity(crawler.address):
                raise ValueError('Выбран другой магазин; недельные замеры нельзя смешивать')
            for item in items:
                row = dict(item, category=item['group'], collected_at=now().isoformat(),
                           store_id=manifest['store_id'], store_address=manifest['store_address'],
                           regular_price=None, availability='Не удалось проверить')
                try:
                    crawler.navigate(item['url'])
                    def ready(d):
                        return (urlparse(d.current_url).path.rstrip('/') == urlparse(item['url']).path.rstrip('/')
                                and d.execute_script('return document.readyState') != 'loading'
                                and d.find_elements(By.CSS_SELECTOR, '.product-page-content__article')
                                and d.find_elements(By.CSS_SELECTOR, '.product-page-content h1')
                                and (d.find_elements(By.CSS_SELECTOR, '.product-page-content .product-prices-lines .product-price__sum-rubles')
                                     or d.execute_script("return /раскуп|нет в наличии/i.test(document.querySelector('.product-page-prices-and-buttons')?.textContent || '')")))
                    WebDriverWait(driver, args.timeout).until(ready)
                    crawler.verify_store()
                    cards = driver.execute_script(DETAIL_SCRIPT, '.product-page-content')
                    card = next(c for c in cards if str(c['id']) == item['product_id'])
                    parsed = observation(card, run_id=run_id, address=crawler.address,
                                         source_name=item['source_category'], category_url=item['source_category_url'])
                    if parsed is None:
                        raise ValueError('Не подтверждён ID карточки')
                    row.update(parsed)
                    # Keep the frozen category assignment even if classification changes.
                    row['section'], row['category'] = item['section'], item['group']
                    row.update(normalize(row))
                    if row['normalized_unit'] != item['normalized_unit']:
                        row['regular_price'] = None
                        row['normalization_note'] = 'Изменилась физическая единица товара'
                except Exception as exc:
                    crawler.dump('item_' + item['product_id'])
                    errors.append(f"{item['product_id']}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
                rows.append(row)
                print(f'Инфляция: {len(rows)}/{len(items)}, {item["name"]}', flush=True)
        else:
            # Establish broad food-group coverage, without harvesting household goods/alcohol.
            targets = [slug for slug, _ in TARGETS if slug not in {'alkogolnaya-produkciya', 'tovary-dlya-doma-dachi-sada', 'bytovaya-himiya', 'kosmetika'}]
            for slug in targets:
                url = BASE + '/category/' + slug
                try:
                    crawler.open_page(url)
                    cards = crawler.load_cards()
                    if cards and not any(c.get('store_scopes') for c in cards):
                        WebDriverWait(driver, args.timeout).until(lambda d: d.find_elements(By.CSS_SELECTOR, '.product-prices-lines .product-price__sum-rubles'))
                        cards = crawler.load_cards()
                    headings = driver.find_elements(By.TAG_NAME, 'h1')
                    category = headings[0].text if headings else slug
                    changed = []
                    for c in cards:
                        row = observation(c, run_id=run_id, address=crawler.address, source_name=category, category_url=url)
                        if row:
                            row.update(normalize(row))
                            crawler.products[row['product_id']] = row
                            changed.append(row)
                    crawler.checkpoint(changed)
                    print(f'Досбор продуктов: {slug}, карточек {len(cards)}', flush=True)
                except Exception as exc:
                    crawler.dump('category_' + slug)
                    errors.append(f'{slug}: {type(exc).__name__}: {str(exc).splitlines()[0]}')
            rows = list(crawler.products.values())
        raw_output = root / 'output/inflation/raw'
        raw_output.mkdir(parents=True, exist_ok=True)
        (raw_output / f'{run_id}.json').write_text(json.dumps({'rows': rows, 'errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        driver.quit()
    return rows, errors


def save_observation(root, manifest, rows, errors):
    output = root / 'output/inflation'
    output.mkdir(parents=True, exist_ok=True)
    run_id = now().strftime('%Y-%m-%d_%H-%M-%S_%f')
    by_id = {str(r['product_id']): r for r in rows}
    export_rows, total, incomplete = [], Decimal('0'), []
    for item in manifest['items']:
        r = by_id.get(item['product_id'])
        normalized = normalize(r) if r else normalize({})
        valid = r and normalized['normalized_price'] is not None and normalized['normalized_unit'] == item['normalized_unit']
        if valid:
            total += Decimal(str(r['regular_price'])) * item['purchase_units']
        else:
            incomplete.append(item['product_id'])
        export_rows.append({'Дата наблюдения': r.get('collected_at', '') if r else '',
                            'Магазин': manifest['store_address'], 'ID товара': item['product_id'],
                            'Раздел': item['section'], 'Группа': item['group'], 'Товар': r['name'] if r else item['name'],
                            'Цена упаковки, руб': r['regular_price'] if valid else None,
                            'Масса/объём упаковки': normalized['package_quantity'] if valid else None,
                            'Цена за кг/л': normalized['normalized_price'] if valid else None,
                            'Единица': normalized['normalized_unit'] if valid else item['normalized_unit'],
                            'Статус': 'Подтверждено' if valid else (r.get('availability', '') if r else 'Не удалось проверить'),
                            'Источник': item['url']})
    # No observation is relabeled with the execution date; original timestamps stay.
    snapshot = output / f'prices_{run_id}.csv'
    with snapshot.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(export_rows)
    budget = Decimal(str(manifest['mrot'])) * Decimal(str(manifest['budget_fraction']))
    tolerance = Decimal(str(manifest['tolerance']))
    fingerprint = hashlib.sha256(json.dumps(manifest['items'], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    report = dict(generated_at=now().isoformat(), basket_id=fingerprint, items=len(export_rows),
                  confirmed=len(export_rows)-len(incomplete), status='partial' if incomplete else 'complete',
                  observed_dates=sorted({r['Дата наблюдения'][:10] for r in export_rows if r['Дата наблюдения']}),
                  mrot=manifest['mrot'], target_budget=float(budget),
                  total_rub=float(total) if not incomplete else None, confirmed_subtotal_rub=float(total),
                  within_budget=bool(not incomplete and budget*(1-tolerance) <= total <= budget*(1+tolerance)),
                  missing_products=incomplete, errors=errors, next_collection_date=(now().date()+timedelta(days=7)).isoformat(),
                  note='One fixed product per food group; package cost checks budget, kg/l prices are stored separately. Prices may leave the original budget range in future weeks; never replace SKUs automatically.')
    snapshot.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    history = []
    for p in sorted(output.glob('prices_*.csv')):
        metadata = json.loads(p.with_suffix('.json').read_text(encoding='utf-8'))
        if metadata.get('basket_id') != fingerprint or metadata.get('status') != 'complete':
            continue
        with p.open(encoding='utf-8-sig', newline='') as f:
            data = list(csv.DictReader(f))
        # Rechecks in the same calendar week do not create invented new weeks.
        dates = metadata['observed_dates']
        date = max(dates) if dates else ''
        iso = datetime.fromisoformat(date).date().isocalendar() if date else None
        week = (iso.year, iso.week) if iso else None
        history.append((week, {'Дата': date, **{r['ID товара']: r['Цена за кг/л'] for r in data}}))
    weeks = dict(history)
    with (output / 'weekly_prices.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['Дата']+[i['product_id'] for i in manifest['items']])
        w.writeheader()
        w.writerows(weeks.values())
    (output / 'latest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'CSV: {snapshot}\nТоваров: {len(export_rows)}; подтверждено: {report["confirmed"]}; стоимость: {report["total_rub"]}; бюджет: {report["within_budget"]}', flush=True)
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description='Продуктовая корзина: 1/4 МРОТ, один товар на группу, цены за кг/л')
    p.add_argument('--collect', action='store_true', help='Досбор каталога при создании корзины или недельный сбор фиксированных товаров')
    p.add_argument('--browser', choices=['chrome', 'edge'], default='chrome')
    p.add_argument('--driver')
    p.add_argument('--profile', type=Path)
    p.add_argument('--timeout', type=int, default=45)
    p.add_argument('--mrot', type=Decimal, default=MROT)
    args = p.parse_args(argv)
    if args.timeout <= 0 or args.mrot <= 0:
        p.error('timeout and mrot must be positive')
    path = ROOT / 'inflation_basket.toml'
    errors = []
    if path.exists():
        manifest = tomllib.loads(path.read_text(encoding='utf-8'))
        if not args.collect:
            print('Корзина уже зафиксирована. Для нового недельного замера: python inflation.py --collect')
            return 0
        rows, errors = live_collect(ROOT, args, manifest['items'])
    else:
        candidates = candidate_pool()
        if args.collect:
            fresh, errors = live_collect(ROOT, args)
            for r in fresh:
                r.update(normalize(r))
                if r['section'] in FOOD_SECTIONS and r['normalized_price'] is not None:
                    candidates.append(r)
        if not candidates or len({r['store_id'] for r in candidates}) != 1:
            raise ValueError('Для корзины нужны цены одного подтверждённого магазина')
        # Use latest fresh record for each SKU, not stale duplicates.
        unique = {}
        for r in candidates:
            key = r['product_id']
            if key not in unique or r['collected_at'] > unique[key]['collected_at']:
                unique[key] = r
        rows, cost, groups = select_basket(list(unique.values()), args.mrot / 4)
        write_manifest(path, rows, args.mrot)
        manifest = tomllib.loads(path.read_text(encoding='utf-8'))
        print(f'Зафиксирована корзина: {len(rows)} из {groups} доступных групп, {cost:.2f} руб.', flush=True)
    report = save_observation(ROOT, manifest, rows, errors)
    return 0 if report['status'] == 'complete' else 2


if __name__ == '__main__':
    raise SystemExit(main())
