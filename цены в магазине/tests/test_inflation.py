import tempfile
import tomllib
import unittest
from decimal import Decimal
from pathlib import Path

from normalization import normalize
from inflation import select_basket, write_manifest, save_observation, now
from categories import classify


class NormalizationTests(unittest.TestCase):
    def row(self, name, price=90, unit='/шт'):
        return {'name': name, 'regular_price': price, 'price_unit': unit}

    def test_package_ml_and_grams(self):
        self.assertEqual(normalize(self.row('Молоко, 900мл'))['normalized_price'], 100)
        self.assertEqual(normalize(self.row('Сахар, 500г'))['normalized_price'], 180)

    def test_weighted_price_is_not_divided_by_package(self):
        r = normalize(self.row('Свинина, 500г', 600, '/кг'))
        self.assertEqual((r['normalized_price'], r['package_quantity']), (600, 1))

    def test_multipack_and_ambiguous_weight(self):
        self.assertEqual(normalize(self.row('Вода, 6x500мл', 150))['normalized_price'], 50)
        for name in ['Сыр, 200-300г', 'Мясо ~1кг', 'Яйца, 10шт', 'Конфеты, 100г 5шт']:
            self.assertIsNone(normalize(self.row(name))['normalized_price'])

    def test_missing_price_stays_missing(self):
        self.assertIsNone(normalize(self.row('Сахар, 1кг', None))['normalized_price'])

    def test_sugar_free_food_is_not_the_sugar_group(self):
        self.assertNotEqual(classify('Жевательная резинка без сахара, 13г', 'Бакалея')[1], 'Сахар и соль')

    def test_flavours_do_not_create_false_food_groups(self):
        self.assertEqual(classify('Лапша Доширак Говядина, 70г', 'Бакалея')[:2], ('Бакалея', 'Макароны'))
        self.assertEqual(classify('Бульон Maggi с курицей, 75г', 'Бакалея')[0], 'Бакалея')
        self.assertEqual(classify('Сыр творожный Виолетта креветка, 140г', 'Сыры')[1], 'Сыры')
        self.assertEqual(classify('Хлеб пшеничный на закваске, 250г', 'Хлеб и выпечка')[0], 'Хлеб и выпечка')
        self.assertNotEqual(classify('Капуста морская маринованная, 200г', 'Рыба и морепродукты')[0], 'Напитки')
        self.assertNotEqual(classify('Сельдерей стебли', 'Овощи и фрукты')[0], 'Рыба и морепродукты')

    def test_one_item_per_group_and_budget(self):
        candidates = [dict(section='Продукты', category=group, regular_price=price, product_id=f'{group}{price}')
                      for group, prices in [('a', [30, 50]), ('b', [40, 60]), ('c', [10, 20])]
                      for price in prices]
        rows, total, groups = select_basket(candidates, Decimal('100'))
        self.assertEqual(len({r['category'] for r in rows}), len(rows))
        self.assertEqual(len(rows), groups)
        self.assertTrue(95 <= total <= 105)

    def test_manifest_strings_roundtrip(self):
        row = dict(product_id='001', section='Бакалея', category='Мука', name='Мука "Марка", 1кг',
                   url='https://example.org/p/001', source_category='Мука', source_category_url='https://example.org/c',
                   price_unit='/шт', package='1кг', normalized_unit='руб/кг', store_id='test', store_address='Москва')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'basket.toml'
            write_manifest(p, [row], Decimal('27093'))
            data = tomllib.loads(p.read_text(encoding='utf-8'))
            self.assertEqual(data['items'][0]['product_id'], '001')
            self.assertEqual(data['items'][0]['name'], row['name'])

    def test_partial_recheck_does_not_replace_complete_week(self):
        import csv
        item = dict(product_id='001', section='Бакалея', group='Сахар', name='Сахар, 1кг',
                    url='https://example.org/001', price_unit='/шт', normalized_unit='руб/кг', purchase_units=1)
        manifest = dict(items=[item], store_address='Москва', mrot=400, budget_fraction=0.25, tolerance=0.05)
        row = dict(item, category='Сахар', regular_price=100, collected_at=now().isoformat(), availability='В наличии')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            save_observation(root, manifest, [row], [])
            save_observation(root, manifest, [dict(row, regular_price=None, availability='Не удалось проверить')], ['network'])
            with (root/'output/inflation/weekly_prices.csv').open(encoding='utf-8-sig', newline='') as f:
                weeks = list(csv.DictReader(f))
            self.assertEqual(len(weeks), 1)
            self.assertEqual(float(weeks[0]['001']), 100)
