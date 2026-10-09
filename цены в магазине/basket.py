"""A persistent 500-SKU basket, read using Selenium's rendered product pages."""
import hashlib
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from history import HEADERS, read_snapshot, save_run
from metro_parser import (BASE, EXTRACT_CARDS, MetroCrawler, create_driver,
                          observation, page_url, store_identity, category_url)

# 360 food + 90 drinks + 50 household consumables.
TARGETS = [(u, 30) for u in (
    'myasnye', 'rybnye', 'ovoshchi-i-frukty', 'molochnye-prodkuty-syry-i-yayca',
    'siry', 'zamorozhennye-produkty', 'myasnye-delikatesy', 'sladosti_',
    'hleb-vypechka-torty', 'bakaleya', 'chipsy-sneki-orehi',
    'gotovye-bljuda-polufabrikaty', 'alkogolnaya-produkciya',
    'bezalkogolnye-napitki', 'chaj-kofe-kakao')]
TARGETS += [('tovary-dlya-doma-dachi-sada', 20), ('bytovaya-himiya', 15), ('kosmetika', 15)]

DETAIL_SCRIPT = EXTRACT_CARDS.replace(
    "const link = card.querySelector('a.product-card-name[href]');",
    "const link = null;") .replace(
    "card.querySelector('[class*=dropdown] .product-availability-status')",
    "card.querySelector('.product-page-prices-and-buttons__store-availability-status')") .replace(
    "card.getAttribute('data-sku')",
    "(document.querySelector('.product-page-content__article')?.textContent.match(/\\d+/) || [null])[0]") .replace(
    "clean(link?.innerText)", "clean(document.querySelector('h1')?.textContent)") .replace(
    "link?.href || ''", "location.href")


def build(crawler, root):
    progress = root / 'basket_progress.json'
    prior = json.loads(progress.read_text(encoding='utf-8')) if progress.exists() else []
    if any(store_identity(r['store_address']) != store_identity(crawler.address) for r in prior):
        raise ValueError('Для продолжения создания корзины нужен прежний торговый центр')
    selected = {r['product_id']: r for r in prior}
    crawler.products.update(selected)
    def add_category(slug, quota):
        url = BASE + '/category/' + slug
        count = sum(r['source_category_url'] == url for r in selected.values())
        if count >= quota:
            return
        seen_cards = set()
        for page in range(1, 11):
            crawler.open_page(url if page == 1 else page_url(url, page))
            cards = crawler.load_cards()
            if cards and not any(card.get('store_scopes') for card in cards):
                try:
                    WebDriverWait(crawler.driver, crawler.timeout).until(
                        lambda d: d.find_elements(By.CSS_SELECTOR, '.product-prices-lines .product-price__sum-rubles'))
                    cards = crawler.load_cards()
                except Exception:
                    pass
            card_ids = {str(c.get('id')) for c in cards if c.get('id')}
            if not card_ids or (seen_cards and card_ids <= seen_cards):
                break
            seen_cards.update(card_ids)
            headings = crawler.driver.find_elements(By.TAG_NAME, 'h1')
            source = headings[0].text if headings else slug
            for card in cards:
                row = observation(card, run_id=crawler.run_id, address=crawler.address,
                                  source_name=source, category_url=url)
                if not row or row['regular_price'] is None or row['product_id'] in selected:
                    continue
                if slug == 'tovary-dlya-doma-dachi-sada' and row['section'] != 'Расходники':
                    continue
                selected[row['product_id']] = row
                count += 1
                if count == quota:
                    break
            crawler.checkpoint([r for r in selected.values() if r['product_id'] not in crawler.products])
            crawler.products.update(selected)
            temp_progress = progress.with_suffix('.tmp')
            temp_progress.write_text(json.dumps(list(selected.values()), ensure_ascii=False), encoding='utf-8')
            temp_progress.replace(progress)
            print(f'Корзина: {slug}, выбрано {count}/{quota}; всего {len(selected)}/500', flush=True)
            if count == quota:
                break
        if count < quota:
            print(f'В {slug} доступно {count}/{quota}; недостаток восполняется внутри той же группы', flush=True)

    for slug, quota in TARGETS:
        add_category(slug, quota)
    # Keep the agreed 360 food / 90 drinks / 50 household composition, but
    # do not require every section to have exactly its initial quota.
    for targets, total in ((TARGETS[:12], 360), (TARGETS[12:15], 90), (TARGETS[15:], 50)):
        urls = {BASE + '/category/' + slug for slug, _ in targets}
        belongs = lambda r: any(r['source_category_url'] == u or r['source_category_url'].startswith(u + '/') for u in urls)
        count = sum(belongs(r) for r in selected.values())
        for slug, _ in targets:
            if count >= total:
                break
            url = BASE + '/category/' + slug
            existing = sum(r['source_category_url'] == url for r in selected.values())
            add_category(slug, existing + total - count)
            count = sum(belongs(r) for r in selected.values())
        if count < total:
            # Parent catalogue pages are curated and may repeat cards on
            # numbered pages. Visit their real child-category links instead.
            for slug, _ in targets:
                if count >= total:
                    break
                parent = BASE + '/category/' + slug
                crawler.open_page(parent)
                child_urls = sorted({u for link in crawler.links()
                                     if (u := category_url(link['url'])) and u.startswith(parent + '/')})
                for child in child_urls:
                    if count >= total:
                        break
                    existing = sum(r['source_category_url'] == child for r in selected.values())
                    add_category(child.removeprefix(BASE + '/category/'), existing + total - count)
                    count = sum(belongs(r) for r in selected.values())
        if count != total:
            raise RuntimeError(f'Недостаточно товаров в группе корзины: {count}/{total}; сохранено для продолжения')
    import pandas as pd
    path = root / 'basket.csv'
    temp = path.with_suffix('.tmp')
    pd.DataFrame(selected.values(), columns=list(HEADERS)).rename(columns=HEADERS).to_csv(temp, index=False, encoding='utf-8-sig')
    temp.replace(path)
    return list(selected.values())


def collect(root, args):
    run_id = datetime.now().astimezone().strftime('%Y-%m-%d_%H-%M-%S_%f')
    path = root / 'basket.csv'
    rows = []
    errors = []
    if not path.exists():
        driver = create_driver(root, args.browser, args.headless, args.driver, args.profile)
        crawler = MetroCrawler(driver, run_id=run_id, debug_dir=root/'output'/'debug'/run_id,
                               timeout=args.timeout, delay=args.delay)
        try:
            crawler.navigate(BASE)
            if not args.yes and not args.headless:
                input('Выберите в браузере торговый центр в Москве, затем нажмите Enter: ')
            crawler.wait_store()
            rows = build(crawler, root)
        finally:
            if not rows:
                crawler.report.update(mode='basket', status='partial', errors=['Создание корзины не завершено'])
                save_run(root/'output', run_id, list(crawler.products.values()), crawler.report)
            driver.quit()
    else:
        basket = read_snapshot(path).to_dict('records')
        if len(basket) != 500 or len({r['product_id'] for r in basket}) != 500:
            raise ValueError('basket.csv должен содержать ровно 500 уникальных товаров')
        expected = {store_identity(r['store_address']) for r in basket}
        if len(expected) != 1:
            raise ValueError('В корзине несколько торговых центров')

        def worker(items, index):
            result, failures = [], []
            # Reuse the chosen store profile. A fresh temporary profile loses
            # region/store settings and cannot provide comparable prices.
            driver = create_driver(root, args.browser, True, args.driver, args.profile)
            crawler = MetroCrawler(driver, run_id=run_id,
                debug_dir=root/'output'/'debug'/run_id/f'worker_{index}', timeout=args.timeout)
            try:
                crawler.navigate(BASE)
                crawler.wait_store()
                if store_identity(crawler.address) not in expected:
                    raise ValueError('Торговый центр нового профиля отличается от корзины')
                for item in items:
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
                        row = observation(card, run_id=run_id, address=crawler.address,
                            source_name=item['source_category'], category_url=item['source_category_url'])
                        if not row:
                            raise ValueError('Карточка товара не подтверждена')
                    except Exception as exc:
                        row = dict(item, run_id=run_id, regular_price=None,
                            collected_at=datetime.now().astimezone().isoformat(),
                            availability='Не удалось проверить', price_evidence='', price_text=str(exc))
                        failures.append(f"{item['product_id']}: {type(exc).__name__}")
                    result.append(row)
                    crawler.checkpoint([row])
                    print(f'Корзина: поток {index}, проверено {len(result)}/{len(items)}', flush=True)
            finally:
                driver.quit()
            return result, failures

        with ThreadPoolExecutor(max_workers=1) as pool:
            futures = [pool.submit(worker, basket, 1)]
            for future in futures:
                result, failures = future.result()
                rows.extend(result)
                errors.extend(failures)
    fingerprint = hashlib.sha256('|'.join(sorted(r['product_id'] for r in rows)).encode()).hexdigest()
    report = dict(run_id=run_id, mode='basket', basket_id=fingerprint,
        status='partial' if errors else 'complete', errors=errors, warnings=[],
        complete_categories=[], categories={}, pages=[], products=len(rows),
        products_with_regular_price=sum(r['regular_price'] is not None for r in rows),
        store_id=rows[0]['store_id'], store_address=rows[0]['store_address'])
    for row in rows:
        row['run_id'] = run_id
    snapshot = save_run(root/'output', run_id, rows, report)
    print(f"CSV: {snapshot}\nТоваров: {len(rows)}; статус: {report['status']}", flush=True)
    return 1 if errors else 0
