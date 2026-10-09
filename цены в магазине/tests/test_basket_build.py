import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from basket import build, TARGETS
from metro_parser import BASE


class BasketBuildTests(unittest.TestCase):
    def test_short_bakery_section_is_filled_from_food_not_household(self):
        crawler = SimpleNamespace(address='Москва, Ленинградское ш., 71Г', products={},
                                  run_id='test', driver=Mock(), timeout=1, checkpoint=Mock())
        crawler.driver.find_elements.return_value = []
        current = ['']
        crawler.open_page = lambda url: current.__setitem__(0, url)

        def cards():
            slug = current[0].split('/category/')[1].split('?')[0]
            count = 27 if slug == 'hleb-vypechka-torty' else 35
            return [dict(id=f'{slug}_{n}', store_scopes=[{}]) for n in range(count)]

        crawler.load_cards = cards

        def observation(card, **kwargs):
            return dict(product_id=card['id'], regular_price=10,
                        section='Расходники' if 'tovary-dlya-doma' in card['id'] else 'Продукты',
                        source_category_url=kwargs['category_url'], store_address=crawler.address)

        with tempfile.TemporaryDirectory() as d, patch('basket.observation', observation):
            rows = build(crawler, Path(d))
            self.assertEqual(len(rows), 500)
            self.assertEqual(len({r['product_id'] for r in rows}), 500)
            for targets, expected in ((TARGETS[:12], 360), (TARGETS[12:15], 90), (TARGETS[15:], 50)):
                urls = {BASE+'/category/'+slug for slug, _ in targets}
                self.assertEqual(sum(r['source_category_url'] in urls for r in rows), expected)
