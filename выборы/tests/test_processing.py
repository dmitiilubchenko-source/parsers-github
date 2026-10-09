import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from election_parser.data_processing import process_saved_reports

FIXTURES = Path(__file__).parent / "fixtures"


class ProcessingTests(unittest.TestCase):
    def test_process_and_deduplicate_real_responses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("pilot_tik_day1.json", "pilot_tik_day2.json", "pilot_uik1_day1.json", "pilot_uik1_day2.json", "pilot_uik1_day3.json"):
                shutil.copyfile(FIXTURES / name, root / name)
            output = root / "processed.csv"
            rows = process_saved_reports(root, output)
            self.assertEqual(len(rows), 28)
            with output.open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 28)

    def test_conflicting_source_is_reported_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(FIXTURES / "pilot_tik_day1.json", root / "pilot_tik_day1.json")
            payload = json.loads((FIXTURES / "pilot_uik1_day1.json").read_text(encoding="utf-8"))
            payload["data"]["body"]["commissionClassifiers"][0]["votersCount"] += 1
            (root / "pilot_uik1_day1.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            rows = process_saved_reports(root, root / "processed.csv")
            issues = json.loads((root / "voting_flow_issues_2026.json").read_text(encoding="utf-8"))
            self.assertEqual(len(issues), 1)
            self.assertIn("Конфликт данных", issues[0]["error"])
            self.assertFalse(any(row.commission_id == issues[0]["commission_id"] and
                                 row.time == "15:00:00" for row in rows))

    def test_decreasing_daily_count_is_reported_and_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(FIXTURES / "pilot_uik1_day1.json", root / "pilot_uik1_day1.json")
            payload = json.loads((FIXTURES / "pilot_uik1_day2.json").read_text(encoding="utf-8"))
            payload["data"]["body"]["commissionClassifiers"][-1]["votersCount"] = 0
            (root / "pilot_uik1_day2.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            rows = process_saved_reports(root, root / "processed.csv")
            issues = json.loads((root / "voting_flow_issues_2026.json").read_text(encoding="utf-8"))
            self.assertGreater(len(rows), 0)
            self.assertEqual(len(issues), 1)
            self.assertEqual({row.commission_id for row in rows}, {issues[0]["commission_id"]})
            self.assertIn("убывает", issues[0]["error"])

    def test_main_reports_take_precedence_over_old_pilot_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pilot = json.loads((FIXTURES / "pilot_uik1_day1.json").read_text(encoding="utf-8"))
            direct = json.loads(json.dumps(pilot))
            direct["data"]["body"]["commissionClassifiers"][0]["votersCount"] += 1
            commission_id = direct["data"]["body"]["commissionClassifiers"][0]["commissionClassifierId"]
            (root / "pilot_uik1_day1.json").write_text(json.dumps(pilot), encoding="utf-8")
            (root / f"report_{commission_id}_2026-09-18.json").write_text(
                json.dumps(direct), encoding="utf-8")
            rows = process_saved_reports(root, root / "processed.csv")
            issues = json.loads((root / "voting_flow_issues_2026.json").read_text(encoding="utf-8"))
            self.assertEqual(len(rows), 2)
            self.assertEqual(issues, [])

    def test_corrupt_daily_file_does_not_stop_other_uiks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = (FIXTURES / "pilot_uik1_day1.json").read_text(encoding="utf-8")
            (root / "report_good_2026-09-18.json").write_text(payload, encoding="utf-8")
            (root / "report_bad_2026-09-18.json").write_text("{broken", encoding="utf-8")
            rows = process_saved_reports(root, root / "processed.csv")
            issues = json.loads((root / "voting_flow_issues_2026.json").read_text(encoding="utf-8"))
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(issues), 1)
            self.assertIn("report_bad", issues[0]["file"])


if __name__ == "__main__":
    unittest.main()
