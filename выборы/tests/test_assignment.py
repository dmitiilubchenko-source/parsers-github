import json
import tempfile
import unittest
from pathlib import Path

from election_parser.assignment import load_row, federal_district


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((Path(__file__).parent/'fixtures/uik1_parties_242.json').read_text(encoding='utf-8'))
        self.item = dict(commission_id=self.data['body']['commissionClassifierId'], name='УИК №1', number=1,
                         path=['ЦИК', 'Республика Адыгея (Адыгея)', 'ОИК', 'ТИК', 'УИК №1'])

    def load(self, data):
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d)
            if data is not None:
                (raw/f"party_{self.item['commission_id']}_242.json").write_text(json.dumps({'data': data}), encoding='utf-8')
            return load_row(self.item, raw)[0]

    def test_all_22_printed_numbers_and_hierarchy(self):
        row = self.load(self.data)
        self.assertEqual(row['Статус данных'], 'complete')
        self.assertEqual((row['ОИК'], row['ТИК']), ('ОИК', 'ТИК'))
        self.assertTrue(all(isinstance(row[f'Строка {n:02d}'], int) for n in range(1, 23)))

    def test_missing_report_retains_uik_with_empty_fields(self):
        row = self.load(None)
        self.assertEqual(row['ID УИК'], self.item['commission_id'])
        self.assertEqual(row['Статус данных'], 'missing')
        self.assertIsNone(row['Строка 01'])

    def test_missing_printed_row_is_not_zero(self):
        self.data['body']['records'] = [r for r in self.data['body']['records'] if str(r['infoPrintNum']) != '22']
        row = self.load(self.data)
        self.assertEqual(row['Статус данных'], 'incomplete')
        self.assertIsNone(row['Строка 22'])

    def test_district_aliases_and_unmatched_branch(self):
        self.assertEqual(federal_district('Кемеровская область - Кузбасс'), 'Сибирский федеральный округ')
        self.assertEqual(federal_district('город Москва'), 'Центральный федеральный округ')
        self.assertIn('Вне', federal_district('Город Байконур (Республика Казахстан)'))
