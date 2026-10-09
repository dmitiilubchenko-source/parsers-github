import json
import tempfile
import unittest
from pathlib import Path

from election_parser.collector import save_report


class RefreshTests(unittest.TestCase):
    def test_unchanged_response_keeps_file_and_creates_no_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path, history = root / "report.json", root / "history"
            old = {"url": "old", "data": {"votes": 10}}
            path.write_text(json.dumps(old), encoding="utf-8")
            before = path.read_bytes()
            status = save_report(path, {"url": "new", "data": {"votes": 10}}, history)
            self.assertEqual(status, "unchanged")
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(history.exists())

    def test_changed_response_archives_previous_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path, history = root / "report.json", root / "history"
            old = {"url": "old", "data": {"votes": 10}}
            path.write_text(json.dumps(old), encoding="utf-8")
            status = save_report(path, {"url": "new", "data": {"votes": 11}}, history)
            self.assertEqual(status, "updated")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["data"]["votes"], 11)
            archives = list(history.glob("report_*.json"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(json.loads(archives[0].read_text(encoding="utf-8")), old)

    def test_corrupt_previous_file_is_replaced_even_if_new_data_is_null(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path, history = root / "report.json", root / "history"
            path.write_text("not json", encoding="utf-8")
            status = save_report(path, {"url": "source", "data": None}, history)
            self.assertEqual(status, "updated")
            self.assertIsNone(json.loads(path.read_text(encoding="utf-8"))["data"])
            self.assertEqual(len(list(history.glob("report_*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
