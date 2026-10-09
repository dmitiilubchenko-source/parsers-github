import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx

from election_parser.retry import failed_jobs, retry_reports


class RetryTests(unittest.TestCase):
    def test_deduplicates_failed_reports_and_ignores_tree_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "collect.jsonl"
            events = [
                {"event": "error", "stage": "daily_report", "commission_id": "uik", "date": "2026-09-18"},
                {"event": "error", "stage": "daily_report", "commission_id": "uik", "date": "2026-09-18"},
                {"event": "error", "stage": "final_report", "commission_id": "uik", "report": 242},
                {"event": "error", "stage": "party_report", "commission_id": "uik", "report": 242,
                 "protocol_num": 2},
                {"event": "error", "stage": "tree", "commission_id": "tik"},
            ]
            log.write_text("\n".join(json.dumps(x) for x in events), encoding="utf-8")
            self.assertEqual(failed_jobs(log), [("daily_report", "uik", "2026-09-18"),
                                                ("final_report", "uik", None),
                                                ("party_report", "uik", None)])

    def test_missing_and_retry_logs_keep_final_protocol_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "collect_missing.jsonl"
            events = [
                {"event": "error", "stage": "final_report", "commission_id": "a"},
                {"event": "error", "stage": "party_report", "commission_id": "b"},
                {"event": "error", "stage": "party_report", "commission_id": "c",
                 "report": 242, "protocol_num": 2},
                {"event": "error", "stage": "party_report", "commission_id": "bad",
                 "report": 453, "protocol_num": 2},
            ]
            log.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            self.assertEqual(failed_jobs(log), [("final_report", "a", None),
                                               ("party_report", "b", None),
                                               ("party_report", "c", None)])

    def test_existing_report_is_rechecked_after_refresh_error(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "pilot_uik1_day1.json").read_text(encoding="utf-8"))
        report = fixture["data"]
        commission_id = report["body"]["commissionClassifiers"][0]["commissionClassifierId"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log = root / "collect.jsonl"
            log.write_text(json.dumps({"event": "error", "stage": "daily_report",
                                       "commission_id": commission_id, "date": "2026-09-18"}), encoding="utf-8")
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps([{"commission_id": commission_id, "number": 1,
                                            "name": "УИК №1", "path": ["ЦИК", "УИК №1"]}]), encoding="utf-8")
            raw = root / "raw"
            raw.mkdir()
            (raw / f"report_{commission_id}_2026-09-18.json").write_text(
                json.dumps({"data": report}), encoding="utf-8")
            with httpx.Client(base_url="http://example.test") as client, \
                 patch("election_parser.retry.CATALOG", catalog), patch("election_parser.retry.RAW_DIR", raw), \
                 patch("election_parser.retry.LOG_DIR", root / "logs"), \
                 patch("election_parser.retry.official_client", return_value=nullcontext(client)), \
                 patch("election_parser.retry.get_json", return_value=report), \
                 patch("election_parser.retry.time.sleep"):
                saved, errors, output = retry_reports(log)
            self.assertEqual((saved, errors), (0, 0))
            self.assertIn('"event": "unchanged"', Path(output).read_text(encoding="utf-8"))

    def test_failed_daily_report_is_downloaded(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "pilot_uik1_day1.json").read_text(encoding="utf-8"))
        report = fixture["data"]
        commission_id = report["body"]["commissionClassifiers"][0]["commissionClassifierId"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log = root / "collect.jsonl"
            log.write_text(json.dumps({"event": "error", "stage": "daily_report",
                                       "commission_id": commission_id, "date": "2026-09-18"}), encoding="utf-8")
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps([{"commission_id": commission_id, "number": 1,
                                            "name": "УИК №1", "path": ["ЦИК", "УИК №1"]}]), encoding="utf-8")
            raw = root / "raw"
            with httpx.Client(base_url="http://example.test") as client, \
                 patch("election_parser.retry.CATALOG", catalog), patch("election_parser.retry.RAW_DIR", raw), \
                 patch("election_parser.retry.LOG_DIR", root / "logs"), \
                 patch("election_parser.retry.official_client", return_value=nullcontext(client)), \
                 patch("election_parser.retry.get_json", return_value=report), \
                 patch("election_parser.retry.time.sleep"):
                saved, errors, output = retry_reports(log)
            self.assertEqual((saved, errors), (1, 0))
            self.assertTrue((raw / f"report_{commission_id}_2026-09-18.json").exists())
            self.assertIn('"event": "new"', Path(output).read_text(encoding="utf-8"))

    def test_inconsistent_final_report_is_saved_as_received(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log = root / "collect.jsonl"
            log.write_text(json.dumps({"event": "error", "stage": "final_report",
                                       "commission_id": "uik", "report": 242}), encoding="utf-8")
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps([{"commission_id": "uik", "number": 1,
                                            "name": "УИК №1", "path": ["ЦИК", "УИК №1"]}]), encoding="utf-8")
            raw = root / "raw"
            with httpx.Client(base_url="http://example.test") as client, \
                 patch("election_parser.retry.CATALOG", catalog), patch("election_parser.retry.RAW_DIR", raw), \
                 patch("election_parser.retry.LOG_DIR", root / "logs"), \
                 patch("election_parser.retry.official_client", return_value=nullcontext(client)), \
                 patch("election_parser.retry.get_json", return_value={}):
                saved, errors, _ = retry_reports(log)
            self.assertEqual((saved, errors), (1, 0))
            payload = json.loads((raw / "results_uik_242.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["data"], {})


if __name__ == "__main__":
    unittest.main()
