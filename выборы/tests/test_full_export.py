import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from election_parser.full_export import CSV_NAME, export_all


class FullExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.catalog = self.root / "catalog.json"
        self.items = [{"commission_id": "a", "number": 1, "name": "УИК №1",
                       "path": ["ЦИК", "Регион А", "Округ 1"]},
                      {"commission_id": "b", "number": 1, "name": "УИК №1",
                       "path": ["ЦИК", "Регион Б", "Округ 2"]}]
        self.catalog.write_text(json.dumps(self.items, ensure_ascii=False), encoding="utf-8")
        self.output = self.root / "out"

    def tearDown(self):
        self.folder.cleanup()

    def export(self):
        return export_all(self.raw, self.catalog, self.output, workers=2)

    def rows(self):
        with (self.output / CSV_NAME).open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_preserves_all_fields_late_columns_and_unvalidated_answers(self):
        payload = {"url": "http://example.test", "status": "original",
                   "data": {"body": {"protocolId": "duplicate", "records": [
                       {"infoText": "Иванов", "value": 42, "extra": None}]},
                            "empty": [], "signed": False, "key/with~slash": "значение"}}
        for cid in ("a", "b"):
            if cid == "b":
                payload["only_in_second"] = "новое поле"
            (self.raw / f"results_{cid}_242.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = self.export()
        rows = self.rows()
        self.assertEqual(len(rows), 2)  # Одинаковый номер УИК в разных регионах не объединяется.
        self.assertEqual(rows[0]["candidates/data/body/records/0/value"], "42")
        self.assertEqual(rows[0]["candidates/data/body/records/0/extra"], "null")
        self.assertEqual(rows[0]["candidates/data/empty"], "[]")
        self.assertEqual(rows[0]["candidates/data/signed"], "false")
        self.assertEqual(rows[0]["candidates/data/key~1with~0slash"], "значение")
        self.assertEqual(rows[0]["candidates/status"], "original")
        self.assertEqual(rows[0]["checks/candidates/validation"], "issue")
        self.assertEqual(rows[1]["candidates/only_in_second"], "новое поле")
        self.assertEqual(rows[0]["checks/parties/status"], "missing")
        self.assertEqual(result["duplicate_protocol_ids"]["candidates"], 1)
        self.assertEqual(result["incomplete_uiks"], 2)

    def test_reuses_cache_and_refreshes_changed_or_removed_file(self):
        path = self.raw / "party_a_242.json"
        path.write_text(json.dumps({"url": "test", "data": {"value": 10}}), encoding="utf-8")
        self.export()
        with patch("election_parser.full_export.build_row", side_effect=AssertionError("Повторное чтение")):
            result = self.export()
        self.assertEqual(result["reused_cached_uiks"], 2)
        path.write_text(json.dumps({"url": "test", "data": {"value": 123, "new": "да"}}), encoding="utf-8")
        result = self.export()
        self.assertEqual(result["reused_cached_uiks"], 1)
        self.assertEqual(self.rows()[0]["parties/data/value"], "123")
        self.assertEqual(self.rows()[0]["parties/data/new"], "да")
        path.unlink()
        self.export()
        self.assertEqual(self.rows()[0]["checks/parties/status"], "missing")

    def test_preserves_corrupt_text_and_rejects_duplicate_catalog_ids(self):
        text = '{"неполный":'
        (self.raw / "results_a_242.json").write_text(text, encoding="utf-8")
        self.export()
        self.assertEqual(self.rows()[0]["checks/candidates/raw_text"], text)
        self.assertEqual(self.rows()[0]["checks/candidates/status"], "invalid_json")
        self.catalog.write_text(json.dumps([self.items[0], self.items[0]]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "повторяется ID"):
            self.export()


if __name__ == "__main__":
    unittest.main()
