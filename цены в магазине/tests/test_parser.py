import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from categories import classify, excluded
from history import HEADERS, compare, read_snapshot, save_run
from metro_parser import MetroCrawler, category_url, number, observation, store_price, store_identity
from selenium.common.exceptions import TimeoutException


def entry(price, old=False, unit="/шт"):
    return {"rubles": str(price), "pennies": "", "old": old, "unit": unit}


def card():
    return {"id": "0012", "name": "Сахар, 1кг", "stock": "0", "text": "В торговом центре",
            "store_scopes": [{"text": "от 1 шт Товара много", "entries": [entry(99), entry(129, True)]}]}


def row(product_id="0012", price=129):
    result = observation(card(), run_id="test", address="Москва, Ленинградское ш., 71Г",
                         source_name="Бакалея / Сахар", category_url="https://online.metro-cc.ru/category/sugar")
    result["product_id"] = product_id
    result["regular_price"] = price
    return result


class PriceTests(unittest.TestCase):
    def test_observed_address_labels_refer_to_same_store(self):
        self.assertEqual(store_identity("Москва, Ленинградское шоссе, 71Г"),
                         store_identity("Москва и область, Ленинградское ш., 71Г"))
        with tempfile.TemporaryDirectory() as temp:
            driver = Mock()
            address = Mock()
            address.text = "Москва, Ленинградское шоссе, 71Г"
            address.is_displayed.return_value = True
            driver.find_elements.return_value = [address]
            crawler = MetroCrawler(driver, run_id="x", debug_dir=Path(temp))
            crawler.address = "Москва и область, Ленинградское ш., 71Г"
            self.assertEqual(crawler.verify_store(), address.text)

    def test_old_price_separates_promo(self):
        self.assertEqual(store_price(card())[0], 129)

    def test_online_price_is_not_a_store_price(self):
        raw = card()
        raw["store_scopes"] = []
        raw["online_price"] = 20
        self.assertIsNone(store_price(raw)[0])

    def test_bottle_retail_price_is_separated_from_bulk_tiers(self):
        raw = card()
        raw["store_scopes"] = [
            {"text": "от 1 бт", "entries": [entry(1399, unit="/бт"), entry(1799, True, unit="/бт")]},
            {"text": "от 6 бт", "entries": [entry(1299, unit="/бт")]},
        ]
        self.assertEqual(store_price(raw)[:2], (1799, "/бт"))

    def test_bulk_and_card_prices_are_excluded(self):
        for text in ["от 10 шт", "от 6 бт", "от 1 шт по карте", "от 1 шт от 10 шт", "скидка 20%"]:
            raw = card()
            raw["store_scopes"] = [{"text": text, "entries": [entry(50)]}]
            self.assertIsNone(store_price(raw)[0], text)

    def test_decimal_pennies(self):
        self.assertEqual(number({"rubles": "1 299", "pennies": ".05"}), 1299.05)
        self.assertEqual(number({"rubles": "99", "pennies": ".9"}), 99.90)
        self.assertIsNone(number({"rubles": "0", "pennies": ""}))

    def test_online_absence_does_not_mean_store_absence(self):
        self.assertEqual(row()["availability"], "В наличии")

    def test_store_absence_has_no_current_price(self):
        raw = card()
        raw["store_scopes"][0]["text"] = "Нет в наличии"
        result = observation(raw, run_id="x", address="Москва, магазин",
                             source_name="Сахар", category_url="x")
        self.assertEqual(result["availability"], "Нет в наличии")
        self.assertIsNone(result["regular_price"])

    def test_category_context_overrides_fruit_flavour(self):
        self.assertEqual(classify("Шампунь с яблоком", "Косметика и гигиена")[:2],
                         ("Гигиена и косметика", "Уход за волосами"))
        self.assertEqual(classify("Корм с говядиной для собак", "Зоотовары")[0], "Зоотовары")
        self.assertEqual(classify("Коктейль безалкогольный", "Алкоголь")[0], "Напитки")
        self.assertEqual(classify("Салат с курицей", "Готовая еда")[0], "Готовая еда и полуфабрикаты")
        self.assertEqual(classify("Эскалоп Мираторг", "Мясо / Свинина")[:2], ("Мясо и птица", "Свинина"))
        self.assertFalse(excluded("Сырные стики", "Сыры"))
        self.assertEqual(classify("Ополаскиватель для рта безалкогольный", "Гигиена")[0], "Гигиена и косметика")
        self.assertEqual(classify("Ежевика, 125г", "Овощи, фрукты / Ягоды")[1], "Фрукты и ягоды")
        self.assertEqual(classify("Бумага туалетная Zewa", "Товары для дома")[0], "Расходники")
        self.assertEqual(classify("Губки для посуды", "Бытовая химия")[0], "Расходники")
        self.assertEqual(classify("Книга о яблоках", "Школа, офис, хобби")[0], "Непродовольственные товары")

    def test_category_urls_exclude_foreign_and_filters(self):
        self.assertIsNone(category_url("https://foreign.example/category/foo"))
        self.assertIsNone(category_url("/category/foo/f/brand/bar"))
        self.assertIsNone(category_url("/category/vse_skidki-40813/dynamic_myasnye_40813"))
        self.assertEqual(category_url("/category/foo?page=2"), "https://online.metro-cc.ru/category/foo")
        self.assertTrue(excluded("Сигареты", ""))


class HistoryTests(unittest.TestCase):
    def test_growth_and_missing_price(self):
        before, after = row(price=100), row(price=110)
        result = compare(pd.DataFrame([after]), pd.DataFrame([before]), set()).iloc[0]
        self.assertEqual(result["Изменение, руб"], 10)
        self.assertEqual(result["Изменение, %"], 10)
        after["regular_price"] = None
        result = compare(pd.DataFrame([after]), pd.DataFrame([before]), set()).iloc[0]
        self.assertEqual(result["Статус"], "Нет сопоставимой обычной цены")

    def test_missing_requires_successful_category(self):
        before = row()
        after = row("other")
        result = compare(pd.DataFrame([after]), pd.DataFrame([before]), set())
        self.assertEqual(result.iloc[0]["Статус"], "Не удалось проверить")
        result = compare(pd.DataFrame([after]), pd.DataFrame([before]), {before["source_category_url"]})
        self.assertEqual(result.iloc[0]["Статус"], "Не найден в каталоге")
        self.assertEqual(result.iloc[1]["Статус"], "Новый товар")

    def test_package_change_is_not_price_growth(self):
        before, after = row(price=100), row(price=90)
        after["package"] = "900г"
        result = compare(pd.DataFrame([after]), pd.DataFrame([before]), set()).iloc[0]
        self.assertEqual(result["Статус"], "Изменилась упаковка или единица цены")
        self.assertTrue(pd.isna(result["Изменение, %"]))

    def test_probe_never_becomes_comparison_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            report = {"status": "complete", "mode": "full", "store_id": row()["store_id"],
                      "complete_categories": []}
            save_run(output, "01", [row(price=100)], copy.deepcopy(report))
            probe = {**report, "status": "partial", "mode": "probe"}
            save_run(output, "02", [row(price=200)], probe)
            save_run(output, "03", [row(price=110)], copy.deepcopy(report))
            changes = pd.read_csv(output / "changes_03.csv")
            self.assertEqual(changes.iloc[0]["Предыдущая цена"], 100)
            self.assertEqual(read_snapshot(output / "prices_03.csv").iloc[0]["product_id"], "0012")
            self.assertEqual(read_snapshot(output / "prices_03.csv").iloc[0]["run_status"], "Завершён")
            self.assertEqual((output / "prices_03.csv").read_bytes()[:3], b"\xef\xbb\xbf")


class CrawlTests(unittest.TestCase):
    def test_checkpoint_preserves_ids_and_records(self):
        with tempfile.TemporaryDirectory() as temp:
            crawler = MetroCrawler(Mock(), run_id="x", debug_dir=Path(temp))
            crawler.checkpoint([row()])
            crawler.checkpoint([row("0034")])
            data = pd.read_csv(Path(temp) / "checkpoint.csv", dtype=str, keep_default_na=False)
            self.assertEqual(data.product_id.tolist(), ["0012", "0034"])
            metadata = json.loads((Path(temp) / "checkpoint_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "partial")

    def test_renderer_timeout_can_recover_if_store_is_present(self):
        driver = Mock()
        driver.get.side_effect = TimeoutException("renderer did not finish")
        address = Mock()
        address.text = "Москва, Ленинградское шоссе, 71Г"
        address.is_displayed.return_value = True
        driver.find_elements.return_value = [address]
        with tempfile.TemporaryDirectory() as temp:
            crawler = MetroCrawler(driver, run_id="x", debug_dir=Path(temp), timeout=1)
            crawler.navigate("https://online.metro-cc.ru/")
            self.assertEqual(crawler.wait_store(), address.text)
            self.assertEqual(len(crawler.report["warnings"]), 1)
            self.assertFalse(crawler.report["errors"])
            driver.execute_script.assert_not_called()

    def test_pagination_collects_both_pages(self):
        class Driver:
            def find_elements(self, *args):
                return []

        class Crawler(MetroCrawler):
            def open_page(self, url):
                self.current_url = url
                self.address = "Москва, Ленинградское ш., 71Г"

            def load_cards(self):
                raw = card()
                raw["id"] = "2" if "page=2" in self.current_url else "1"
                return [raw]

            def links(self):
                return [{"url": "https://online.metro-cc.ru/category/sugar?page=2", "name": "2"}]

        with tempfile.TemporaryDirectory() as temp:
            crawler = Crawler(Driver(), run_id="x", debug_dir=Path(temp), delay=0)
            url = "https://online.metro-cc.ru/category/sugar"
            rows = crawler.crawl({url: "Сахар"})
            self.assertEqual({item["product_id"] for item in rows}, {"1", "2"})
            self.assertEqual(crawler.report["complete_categories"], [url])
            self.assertEqual(crawler.report["status"], "complete")

    def test_repeated_page_is_incomplete(self):
        class Driver:
            def find_elements(self, *args):
                return []

        class Crawler(MetroCrawler):
            def open_page(self, url):
                self.address = "Москва, Ленинградское ш., 71Г"

            def load_cards(self):
                return [card()]

            def links(self):
                return [{"url": "https://online.metro-cc.ru/category/sugar?page=2", "name": "2"}]

            def dump(self, label):
                pass

        with tempfile.TemporaryDirectory() as temp:
            crawler = Crawler(Driver(), run_id="x", debug_dir=Path(temp), delay=0)
            crawler.crawl({"https://online.metro-cc.ru/category/sugar": "Сахар"})
            self.assertEqual(crawler.report["status"], "partial")
            self.assertEqual(crawler.report["complete_categories"], [])
            self.assertTrue(crawler.report["errors"])


if __name__ == "__main__":
    unittest.main()
