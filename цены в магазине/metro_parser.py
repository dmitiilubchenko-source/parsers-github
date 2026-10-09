"""Read METRO's rendered DOM through Selenium; never substitute online prices."""

import hashlib
import csv
import json
import re
import time
from collections import deque
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from categories import classify, excluded
from history import HEADERS

BASE = "https://online.metro-cc.ru"
CARD_SELECTOR = ".catalog-1-level-product-card[data-sku], .catalog-2-level-product-card[data-sku]"
ADDRESS_SELECTOR = ".header-common__address-button-text"

# Data-only browser script: examines visible rendered elements, not hidden APIs.
EXTRACT_CARDS = r"""
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const visible = el => !!(el && el.getClientRects().length);
function priceData(scope) {
  const entries = [];
  for (const rub of scope.querySelectorAll('.product-price__sum-rubles')) {
    const price = rub.closest('.product-price') || rub.parentElement;
    const penny = price.querySelector('.product-price__sum-penny');
    const unit = price.querySelector('.product-price__unit');
    let ancestor = price, old = false;
    while (ancestor && ancestor !== scope.parentElement) {
      if (/old|previous/.test(String(ancestor.className)) ||
          getComputedStyle(ancestor).textDecorationLine.includes('line-through')) old = true;
      ancestor = ancestor.parentElement;
    }
    entries.push({rubles: clean(rub.textContent), pennies: clean(penny?.textContent),
      unit: clean(unit?.textContent), old, text: clean(price.innerText)});
  }
  return entries;
}
return Array.from(document.querySelectorAll(arguments[0])).filter(visible).map(card => {
  const link = card.querySelector('a.product-card-name[href]');
  const cardText = clean(card.textContent);
  const candidates = Array.from(card.querySelectorAll('.product-prices-lines'));
  const scopes = [];
  for (const block of candidates) {
    const blockText = clean(block.textContent);
    const storeLabel = /в торговом центре|цена в тц/i.test(cardText);
    if (!storeLabel || /только для онлайн/i.test(blockText)) continue;
    let lines = Array.from(block.querySelectorAll(
      '.product-prices-lines__item, .product-prices-lines__line, .product-prices-line'));
    if (!lines.length) lines = [block];
    // Drop parent scopes when a more specific line has also matched.
    lines = lines.filter(line => !lines.some(other => line !== other && line.contains(other)));
    for (const line of lines) {
      const text = clean(line.textContent);
      const entries = priceData(line);
      if (entries.length) scopes.push({text, entries});
    }
  }
  const stockElement = card.querySelector('[class*=dropdown] .product-availability-status');
  return {id: card.getAttribute('data-sku'), name: clean(link?.innerText),
    url: link?.href || '', stock: link?.getAttribute('data-gtm-in-stock'),
    text: cardText, store_stock_text: clean(stockElement?.textContent), store_scopes: scopes};
});
"""


def number(entry: dict) -> float | None:
    whole = re.sub(r"[^\d]", "", entry.get("rubles", ""))
    pennies = re.sub(r"[^\d]", "", entry.get("pennies", ""))
    if not whole or len(pennies) > 2:
        return None
    try:
        value = Decimal(whole) + Decimal((pennies or "0").ljust(2, "0")) / 100
    except InvalidOperation:
        return None
    return float(value) if value > 0 else None


def store_price(card: dict) -> tuple[float | None, str, str, str]:
    """Require an unambiguous retail tier in the physical-store block."""
    possibilities = []
    for scope in card.get("store_scopes", []):
        text = scope["text"]
        tiers = re.findall(r"от\s+(\d+(?:[.,]\d+)?)\s*([а-яё]+)", text, re.I)
        # A block combining multiple tiers needs a more specific DOM selector.
        if tiers:
            if len(set(tiers)) != 1:
                continue
            quantity, tier_unit = tiers[0]
            quantity = Decimal(quantity.replace(",", "."))
            if quantity != 1 and not (tier_unit.casefold() in {"кг", "л"} and 0 < quantity < 1):
                continue
        if re.search(r"по карте|с картой|онлайн|самовывоз|резервирован", text, re.I):
            continue
        current = [(number(item), item.get("unit", "")) for item in scope["entries"]
                   if not item["old"]]
        old = [(number(item), item.get("unit", "")) for item in scope["entries"]
               if item["old"]]
        current = list(dict.fromkeys(item for item in current if item[0] is not None))
        old = list(dict.fromkeys(item for item in old if item[0] is not None))
        if len(current) != 1:
            continue
        current_price, unit = current[0]
        if len(old) == 1 and old[0][0] > current_price:
            old_price, old_unit = old[0]
            if old_unit and unit and old_unit != unit:
                continue
            possibilities.append((old_price, old_unit or unit,
                                  "зачёркнутая цена в блоке торгового центра", text))
        elif not old and not re.search(r"скидк|акци|промо|%", text, re.I):
            possibilities.append((current_price, unit, "цена в блоке торгового центра", text))
    unique = {(price, unit) for price, unit, _, _ in possibilities}
    if len(unique) == 1:
        price, unit, evidence, text = possibilities[0]
        # Without a sale unit comparing numeric prices can be misleading.
        if unit:
            return price, unit, evidence, text
    return None, "", "обычная цена торгового центра не подтверждена", card.get("text", "")


def package(name: str) -> str:
    matches = re.findall(r"(?:~\s*)?\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+)?\s*(?:кг|мл|г|л|шт)\b", name, re.I)
    return "; ".join(matches)


def store_identity(address: str) -> str:
    normalized = re.sub(r"\s+", " ", address.strip()).casefold()
    # Both labels were observed for the same verified Moscow trade centre.
    if "ленинград" in normalized and re.search(r"\b71\s*г\b", normalized):
        return "metro_moscow_leningradskoe_71g"
    return "metro_" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


def observation(card: dict, *, run_id: str, address: str, source_name: str,
                category_url: str) -> dict | None:
    if not card.get("id") or not card.get("name") or excluded(card["name"], source_name):
        return None
    price, unit, evidence, text = store_price(card)
    # The link's data-gtm-in-stock may describe online ordering, not the store.
    store_text = " ".join(scope["text"] for scope in card.get("store_scopes", []))
    store_text += " " + card.get("store_stock_text", "")
    unavailable = bool(re.search(r"нет в наличии|раскупили|сообщить о поступлении", store_text, re.I))
    available = bool(re.search(r"товара много|в наличии|товара мало|осталось", store_text, re.I)) and not unavailable
    section, category, rule = classify(card["name"], source_name)
    store_id = store_identity(address)
    return {
        "run_id": run_id, "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "retailer": "METRO", "city": "Москва", "store_id": store_id,
        "store_address": address, "product_id": str(card["id"]), "name": card["name"],
        "section": section, "category": category, "category_rule": rule,
        "source_category": source_name, "source_category_url": category_url,
        "regular_price": None if unavailable else price, "price_unit": unit,
        "price_type": "торговый центр", "price_evidence": evidence,
        "package": package(card["name"]),
        "availability": "Нет в наличии" if unavailable else (
            "В наличии" if available else "Наличие не подтверждено"),
        "url": card.get("url", ""), "price_text": text,
    }


def category_url(url: str) -> str | None:
    parsed = urlparse(urljoin(BASE, url))
    if parsed.netloc != urlparse(BASE).netloc or not parsed.path.startswith("/category/"):
        return None
    # Filter and sorting pages aren't independent catalogue categories.
    if "/f/" in parsed.path:
        return None
    # Promotional collections repeat ordinary categories and cannot establish
    # absence of a product from the actual catalogue.
    root_category = parsed.path.split("/")[2].casefold()
    if root_category == "zony-brendov-50961" or root_category.startswith(("vse_skidki", "akcii", "novinki", "promo")):
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def page_url(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def create_driver(root: Path, browser: str, headless: bool, driver_path: str | None = None,
                  profile_dir: Path | None = None):
    if browser == "edge":
        from selenium.webdriver.edge.options import Options
        from selenium.webdriver.edge.service import Service
        constructor = webdriver.Edge
    else:
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        constructor = webdriver.Chrome
    options = Options()
    # Advertising and analytics requests can keep the load event pending even
    # after the catalogue is usable. Wait for the actual DOM elements instead.
    options.page_load_strategy = "none"
    options.add_argument(f"--user-data-dir={(profile_dir or root / 'browser_profile' / browser).resolve()}")
    options.add_argument("--lang=ru-RU")
    options.add_argument("--window-size=1440,1000")
    if headless:
        options.add_argument("--headless=new")
    try:
        driver = constructor(options=options, service=Service(executable_path=driver_path) if driver_path else Service())
    except SessionNotCreatedException as exc:
        if "DevToolsActivePort" in str(exc):
            raise RuntimeError(
                "Chrome не запустился. Возможно, профиль уже открыт другим запуском. "
                "Закройте окно Chrome предыдущего запуска парсера или укажите отдельный профиль: "
                "--profile browser_profile/test"
            ) from exc
        raise
    driver.set_page_load_timeout(60)
    return driver


class MetroCrawler:
    def __init__(self, driver, *, run_id: str, debug_dir: Path, timeout: int = 30,
                 delay: float = 0.3, max_pages: int = 0):
        self.driver = driver
        self.run_id = run_id
        self.debug_dir = debug_dir
        self.timeout = timeout
        self.delay = delay
        self.max_pages = max_pages
        self.address = ""
        self.products = {}
        self.report = {"run_id": run_id, "mode": "full", "status": "partial",
                       "complete_categories": [], "pages": [], "errors": [],
                       "warnings": [], "categories": {}, "discovery": "links in rendered catalogue"}

    def dump(self, label: str) -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        filename = re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:100]
        try:
            (self.debug_dir / f"{filename}.html").write_text(self.driver.page_source, encoding="utf-8")
            self.driver.save_screenshot(str(self.debug_dir / f"{filename}.png"))
        except Exception:
            pass  # Diagnostic capture must not overwrite the actual crawl error.

    def checkpoint(self, changed_rows=()):
        """Append observations per page so a long run survives interruption."""
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        observations = self.debug_dir / "checkpoint.csv"
        new_file = not observations.exists()
        with observations.open("a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(HEADERS))
            if new_file:
                writer.writeheader()
            writer.writerows(changed_rows)
        temporary = self.debug_dir / "checkpoint_metadata.tmp"
        temporary.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.debug_dir / "checkpoint_metadata.json")

    def verify_store(self):
        elements = self.driver.find_elements(By.CSS_SELECTOR, ADDRESS_SELECTOR)
        addresses = {re.sub(r"\s+", " ", item.text.strip()) for item in elements if item.is_displayed() and item.text.strip()}
        if len(addresses) != 1:
            raise RuntimeError("Не удалось однозначно определить торговый центр. Выберите его в браузере.")
        address = addresses.pop()
        if not re.search(r"москва", address, re.I):
            raise RuntimeError(f"Выбран регион вне Москвы: {address}")
        # "Москва и область" alone is a region, not a store.
        if len(address) < 25 or address.casefold() in {"москва и область", "москва"}:
            raise RuntimeError("В заголовке указан только регион. Нужен адрес конкретного торгового центра.")
        if self.address and store_identity(address) != store_identity(self.address):
            raise RuntimeError(f"Торговый центр изменился: {self.address} -> {address}")
        self.address = address
        return address

    def links(self):
        return self.driver.execute_script("""
            return Array.from(document.querySelectorAll('a[href]')).map(a =>
              ({url: a.href, name: (a.innerText || a.textContent || '').trim()}));
        """)

    def navigate(self, url: str):
        try:
            self.driver.get(url)
        except TimeoutException:
            # A renderer timeout does not prove that the required DOM is absent.
            # Do not stop the page: asynchronous product data may still arrive.
            warning = f"Браузер не завершил загрузку {url}; проверяем доступность каталога"
            self.report["warnings"].append(warning)
            print(warning, flush=True)

    def wait_store(self):
        WebDriverWait(self.driver, self.timeout).until(
            lambda d: any(item.is_displayed() and item.text.strip()
                          for item in d.find_elements(By.CSS_SELECTOR, ADDRESS_SELECTOR)))
        return self.verify_store()

    def open_page(self, url: str):
        self.navigate(url)
        target = urlparse(url)
        def page_ready(driver):
            current = urlparse(driver.current_url)
            # With strategy=none the previous document may briefly remain.
            if (current.netloc != target.netloc or current.path.rstrip("/") != target.path.rstrip("/")
                    or parse_qs(current.query).get("page", ["1"]) != parse_qs(target.query).get("page", ["1"])):
                return False
            if driver.execute_script("return document.readyState") == "loading":
                return False
            return (driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR) or
                    driver.find_elements(By.CSS_SELECTOR, ".catalog-empty, .catalog-no-products"))
        WebDriverWait(self.driver, self.timeout).until(
            page_ready)
        self.verify_store()

    def load_cards(self):
        last = len(self.driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR))
        stable = 0
        # Some catalogue pages append cards while scrolling. Never silently cap.
        for _ in range(120):
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            try:
                WebDriverWait(self.driver, 2, poll_frequency=0.25).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, CARD_SELECTOR)) != last)
            except TimeoutException:
                pass
            count = len(self.driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR))
            stable = stable + 1 if count == last else 0
            last = count
            if stable >= 2:
                break
        else:
            raise RuntimeError("Карточки продолжают подгружаться: страница не собрана полностью")
        return self.driver.execute_script(EXTRACT_CARDS, CARD_SELECTOR)

    def discover(self):
        # Opening the catalogue menu is an ordinary, reversible UI action.
        for element in self.driver.find_elements(By.CSS_SELECTOR, "button, a"):
            if element.is_displayed() and element.text.strip().casefold() == "каталог":
                try:
                    element.click()
                    WebDriverWait(self.driver, self.timeout).until(
                        lambda d: len(d.find_elements(By.CSS_SELECTOR, "a[href*='/category/']")) > 5)
                except Exception:
                    pass
                break
        found = {}
        for link in self.links():
            url = category_url(link["url"])
            if url and not excluded("", link["name"]) and link["name"].strip().casefold() not in {"скидки", "акции", "новинки"}:
                found.setdefault(url, link["name"] or urlparse(url).path.rsplit("/", 1)[-1])
        if not found:
            self.dump("no_categories")
            raise RuntimeError("Не найдены ссылки категорий. Сохранён HTML для проверки селекторов.")
        return found

    def collect_category(self, url: str, name: str, page_limit: int = 0):
        pages = deque([url])
        seen, ids, discovered = set(), set(), {}
        success = True
        while pages:
            target = pages.popleft()
            if target in seen:
                continue
            if page_limit and len(seen) >= page_limit:
                success = False
                break
            if self.max_pages and len(self.report["pages"]) >= self.max_pages:
                success = False
                self.report["errors"].append("Достигнут заданный предел страниц")
                break
            seen.add(target)
            try:
                self.open_page(target)
                cards = self.load_cards()
                if not cards:
                    # Conservatively avoid claiming that formerly listed goods disappeared.
                    raise RuntimeError("Нет карточек: пустая категория требует проверки")
                new_ids = {str(card["id"]) for card in cards if card.get("id")}
                if ids and new_ids and new_ids <= ids:
                    raise RuntimeError("Повтор карточек на другой странице: переход пагинации не подтверждён")
                ids.update(new_ids)
                heading = self.driver.find_elements(By.TAG_NAME, "h1")
                actual_name = heading[0].text.strip() if heading else name
                breadcrumbs = self.driver.find_elements(By.CSS_SELECTOR, ".breadcrumbs, [class*='breadcrumbs']")
                source = " / ".join(dict.fromkeys([b.text.replace("\n", " / ") for b in breadcrumbs if b.text] + [actual_name]))
                changed_rows = []
                for card in cards:
                    row = observation(card, run_id=self.run_id, address=self.address,
                                      source_name=source, category_url=url)
                    if row:
                        prior = self.products.get(row["product_id"])
                        # Prefer deeper catalogue paths to broad parent category duplicates.
                        if prior is None or len(urlparse(url).path.split("/")) > len(urlparse(prior["source_category_url"]).path.split("/")):
                            self.products[row["product_id"]] = row
                            changed_rows.append(row)
                links = self.links()
                for link in links:
                    canonical = category_url(link["url"])
                    if canonical and not excluded("", link["name"]):
                        discovered.setdefault(canonical, link["name"] or actual_name)
                    parsed = urlparse(link["url"])
                    query = parse_qs(parsed.query)
                    if canonical == url and query.get("page", [""])[0].isdigit():
                        candidate = page_url(url, int(query["page"][0]))
                        if candidate != page_url(url, 1) and candidate not in seen:
                            pages.append(candidate)
                # If a next button has no URL, don't declare this category complete.
                next_buttons = self.driver.find_elements(By.CSS_SELECTOR,
                    "button[aria-label*='След'], button[aria-label*='след'], button[class*='pagination'][class*='next']")
                if any(button.is_displayed() and button.is_enabled() for button in next_buttons):
                    raise RuntimeError("Пагинация кнопкой без ссылки требует проверки HTML")
                self.report["pages"].append({"url": target, "cards": len(cards)})
                self.checkpoint(changed_rows)
                if not any(row["regular_price"] is not None for row in self.products.values()):
                    self.dump("unconfirmed_store_prices")
                if self.report["mode"] == "probe":
                    self.dump(f"probe_{len(self.report['pages'])}")
                    (self.debug_dir / f"probe_{len(self.report['pages'])}_cards.json").write_text(
                        json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  {actual_name}: страница {len(seen)}, карточек {len(cards)}", flush=True)
                time.sleep(self.delay)
            except Exception as exc:
                success = False
                self.dump(f"error_{len(self.report['pages'])}_{len(self.report['errors'])}")
                self.report["errors"].append(f"{target}: {type(exc).__name__}: {exc}")
                print(f"  Ошибка: {exc}", flush=True)
                break
        if success:
            self.report["complete_categories"].append(url)
        self.checkpoint()
        return discovered

    def crawl(self, categories: dict[str, str], *, probe: bool = False):
        self.report["mode"] = "probe" if probe else "full"
        self.report["categories"] = categories
        queue = deque(categories.items())
        visited = set(self.report['complete_categories'])
        while queue:
            url, name = queue.popleft()
            if url in visited:
                continue
            if probe and len(visited) >= 3:
                break
            if self.max_pages and len(self.report["pages"]) >= self.max_pages:
                break
            visited.add(url)
            print(f"Категория {len(visited)}: {name}", flush=True)
            children = self.collect_category(url, name, page_limit=1 if probe else 0)
            for child, child_name in children.items():
                categories.setdefault(child, child_name)
                if child not in visited:
                    queue.append((child, child_name))
        self.report["categories"] = categories
        complete = (not probe and not self.report["errors"] and
                    visited == set(categories) and len(self.report["complete_categories"]) == len(visited))
        self.report["status"] = "complete" if complete else "partial"
        self.checkpoint()
        return list(self.products.values())
