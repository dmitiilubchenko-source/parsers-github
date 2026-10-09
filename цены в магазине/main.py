"""Entry point: python main.py --probe, then python main.py."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from history import save_run
from categories import classify
from metro_parser import BASE, MetroCrawler, create_driver, store_identity, category_url

ROOT = Path(__file__).resolve().parent


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if '--inflation' in sys.argv:
        from inflation import main as inflation_main
        return inflation_main([a for a in sys.argv[1:] if a != '--inflation'])
    parser = argparse.ArgumentParser(description="Сбор обычных цен торгового центра METRO в Москве в CSV")
    parser.add_argument('--inflation', action='store_true', help='Режим September.pdf; параметры: main.py --inflation --help')
    parser.add_argument("--probe", action="store_true", help="До 3 категорий, по одной странице; сохраняет HTML")
    parser.add_argument("--full", action="store_true", help="Весь каталог вместо фиксированной корзины 500 товаров")
    parser.add_argument("--browser", choices=["chrome", "edge"], default="chrome")
    parser.add_argument("--driver", help="Путь к chromedriver/msedgedriver, если Selenium Manager недоступен")
    parser.add_argument("--profile", type=Path, help="Отдельная папка профиля браузера, если основной профиль занят")
    parser.add_argument("--headless", action="store_true", help="Без окна; требует уже сохранённого выбора магазина")
    parser.add_argument("--yes", action="store_true", help="Не ждать Enter: использовать сохранённый магазин")
    parser.add_argument("--resume", type=Path, help="Продолжить из CSV или папки checkpoint")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--max-pages", type=int, default=0, help="Ограничить число страниц; 0 — без ограничения")
    args = parser.parse_args()
    if args.timeout <= 0 or args.delay < 0 or args.max_pages < 0:
        parser.error("timeout > 0, delay >= 0, max-pages >= 0")
    if not args.full and not args.probe and not args.resume:
        from basket import collect
        return collect(ROOT, args)
    run_id = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
    driver = None
    crawler = None
    exit_code = 0
    try:
        driver = create_driver(ROOT, args.browser, args.headless, args.driver, args.profile)
        crawler = MetroCrawler(driver, run_id=run_id, debug_dir=ROOT / "output" / "debug" / run_id,
                               timeout=args.timeout, delay=args.delay, max_pages=args.max_pages)
        print("Открываю METRO. Ожидаем каталог, без ожидания всех рекламных ресурсов.", flush=True)
        crawler.navigate(BASE)
        if not args.yes and not args.headless:
            print("В браузере выберите Москву и конкретный торговый центр METRO.")
            print("Если сайт просит подтверждение возраста, выполните его самостоятельно.")
            input("Когда каталог и адрес магазина загрузятся, нажмите Enter здесь: ")
        crawler.wait_store()
        print(f"Торговый центр: {crawler.address}")
        categories = crawler.discover()
        if args.resume:
            from resume import restore
            restored, prior = restore(args.resume)
            expected = store_identity(crawler.address)
            if any(store_identity(row['store_address']) != expected for row in restored):
                raise ValueError('Для продолжения нужен тот же торговый центр')
            if prior.get('mode') == 'probe':
                raise ValueError('Пробный замер нельзя продолжать как полный')
            for row in restored:
                row['store_id'] = expected
                row['run_id'] = run_id
                row['section'], row['category'], row['category_rule'] = classify(row['name'], row['source_category'])
                crawler.products[row['product_id']] = row
            categories.update({u: n for u, n in prior['categories'].items() if category_url(u)})
            crawler.report['complete_categories'] = [u for u in prior['complete_categories'] if category_url(u)]
            crawler.report['pages'] = prior.get('pages', [])
            crawler.report['resumed_from'] = str(args.resume.resolve())
            crawler.report['warnings'].append('Продолженный замер: даты наблюдений сохранены, сбор проходил в несколько сеансов')
            crawler.checkpoint(list(crawler.products.values()))
            print(f'Восстановлено товаров: {len(restored)}; завершённых категорий: {len(crawler.report["complete_categories"])}')
        print(f"Найдено ссылок категорий: {len(categories)}")
        crawler.crawl(categories, probe=args.probe)
    except KeyboardInterrupt:
        exit_code = 130
        if crawler:
            crawler.report["errors"].append("Сбор остановлен пользователем")
            crawler.report["status"] = "partial"
        print("Сбор остановлен. Сохраняю уже полученные товары.")
    except Exception as exc:
        exit_code = 1
        if crawler:
            crawler.dump("startup_error")
            crawler.report["errors"].append(f"{type(exc).__name__}: {exc}")
            crawler.report["status"] = "partial"
        print(f"Ошибка: {exc}", file=sys.stderr)
    finally:
        try:
            if crawler:
                rows = list(crawler.products.values())
                crawler.report["mode"] = "probe" if args.probe else "full"
                crawler.report["store_address"] = crawler.address
                crawler.report["store_id"] = rows[0]["store_id"] if rows else None
                crawler.report["products"] = len(rows)
                priced = sum(row["regular_price"] is not None for row in rows)
                crawler.report["products_with_regular_price"] = priced
                if not rows or not priced:
                    crawler.report["status"] = "partial"
                    crawler.report["errors"].append("Нет товаров с подтверждённой обычной ценой торгового центра")
                csv_path = save_run(ROOT / "output", run_id, rows, crawler.report)
                print(f"CSV: {csv_path}")
                print(f"Товаров: {len(rows)}; с обычной ценой: {priced}; статус: {crawler.report['status']}")
                if crawler.report["errors"]:
                    exit_code = exit_code or 1
                    print(f"Подробности: {csv_path.with_suffix('.json')}")
                elif crawler.report["status"] == "partial" and not args.probe:
                    exit_code = exit_code or 2
        finally:
            if driver:
                driver.quit()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
