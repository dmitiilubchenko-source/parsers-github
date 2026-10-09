"""Real Chrome/Edge DOM extraction on a local page, with no METRO requests."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from metro_parser import CARD_SELECTOR, EXTRACT_CARDS, MetroCrawler, create_driver, observation, store_price


@unittest.skipUnless(os.environ.get("METRO_TEST_DRIVER"), "Set METRO_TEST_DRIVER to a compatible local chromedriver")
class SeleniumDomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.driver = create_driver(Path(cls.temp.name), "chrome", True, os.environ["METRO_TEST_DRIVER"])
        cls.driver.get((Path(__file__).parent / "fixture.html").resolve().as_uri())
        WebDriverWait(cls.driver, 10).until(lambda d: d.find_elements(By.CSS_SELECTOR, CARD_SELECTOR))

    def setUp(self):
        self.driver.get((Path(__file__).parent / "fixture.html").resolve().as_uri())
        WebDriverWait(self.driver, 10).until(lambda d: d.find_elements(By.CSS_SELECTOR, CARD_SELECTOR))

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.temp.cleanup()

    def test_rendered_dom_separates_store_online_and_bulk_prices(self):
        cards = self.driver.execute_script(EXTRACT_CARDS, CARD_SELECTOR)
        self.assertEqual(len(cards), 3)
        self.assertEqual(store_price(cards[0])[0], 129)
        self.assertIsNone(store_price(cards[1])[0])
        self.assertEqual(store_price(cards[2])[:2], (76.90, "/кг"))
        row = observation(cards[2], run_id="fixture", address="Москва, Ленинградское ш., 71Г",
                          source_name="Овощи", category_url="x")
        self.assertEqual(row["availability"], "В наличии")

    def test_moscow_address_is_read_from_browser(self):
        crawler = MetroCrawler(self.driver, run_id="fixture", debug_dir=Path(self.temp.name))
        self.assertIn("71Г", crawler.verify_store())

    def test_hidden_store_dropdown_is_read(self):
        self.driver.execute_script("""
            document.querySelectorAll('.product-prices-lines').forEach(el => el.style.display = 'none');
        """)
        cards = self.driver.execute_script(EXTRACT_CARDS, CARD_SELECTOR)
        self.assertEqual(store_price(cards[0])[0], 129)
        self.assertEqual(store_price(cards[2])[:2], (76.90, "/кг"))

    def test_navigation_does_not_wait_for_every_resource(self):
        self.assertEqual(self.driver.capabilities['pageLoadStrategy'], 'none')

    def test_first_level_catalogue_cards_are_supported(self):
        self.driver.execute_script("""
            document.querySelectorAll('.catalog-2-level-product-card').forEach(el => {
              el.classList.remove('catalog-2-level-product-card');
              el.classList.add('catalog-1-level-product-card');
            });
        """)
        cards = self.driver.execute_script(EXTRACT_CARDS, CARD_SELECTOR)
        self.assertEqual(len(cards), 3)
        self.assertEqual(store_price(cards[0])[0], 129)


if __name__ == "__main__":
    unittest.main()
