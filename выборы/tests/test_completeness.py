import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from election_parser.collector import adjust_pacing, check_completeness, collect_region
from election_parser.tree import UikNode


class ResumeTests(unittest.TestCase):
    def test_throttle_recovers_after_successful_responses(self) -> None:
        pacing = {"delay": 0.0, "successes": 0}
        self.assertEqual(adjust_pacing(pacing, 502, 0.0), 0.25)
        for _ in range(120):
            adjust_pacing(pacing, 200, 0.0)
        self.assertEqual(pacing["delay"], 0.0)
        pacing = {"delay": 3.0, "successes": 0}
        self.assertEqual(adjust_pacing(pacing, 502, 3.0), 3.0)

    def test_reports_all_five_missing_answers(self) -> None:
        node = UikNode("missing", 1, "УИК №1", ("ЦИК", "УИК №1"))
        with tempfile.TemporaryDirectory() as folder:
            report = check_completeness([node], Path(folder))
        self.assertEqual(report["complete_uiks"], 0)
        self.assertEqual(len(report["incomplete_uiks"][0]["missing_or_invalid"]), 5)

    def test_keeps_fetched_answers_even_when_validation_fails(self) -> None:
        node = UikNode("raw", 1, "УИК №1", ("ЦИК", "УИК №1"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for day in ("2026-09-18", "2026-09-19", "2026-09-20"):
                (root / f"report_raw_{day}.json").write_text(
                    json.dumps({"url": f"http://example.test?date={day}", "data": {}}), encoding="utf-8")
            for prefix in ("results", "party"):
                (root / f"{prefix}_raw_242.json").write_text(
                    json.dumps({"url": "http://example.test", "data": {}}), encoding="utf-8")
            report = check_completeness([node], root)
        self.assertEqual(report["complete_uiks"], 1)
        self.assertEqual(len(report["validation_issues"]), 5)

    def test_resumes_only_unfinished_uik(self) -> None:
        nodes = [UikNode("done", 1, "УИК №1", ("ЦИК", "УИК №1")),
                 UikNode("pending", 2, "УИК №2", ("ЦИК", "УИК №2"))]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tree_checkpoint = root / "tree.json"
            collect_checkpoint = root / "collect.jsonl"
            collect_checkpoint.write_text(
                json.dumps({"version": 1, "region": "test", "limit": 0}) + "\n" +
                json.dumps({"commission_id": "done"}) + "\n", encoding="utf-8")
            raw = root / "raw"
            raw.mkdir()
            for day in ("2026-09-18", "2026-09-19", "2026-09-20"):
                (raw / f"report_done_{day}.json").write_text(json.dumps({"data": {}}), encoding="utf-8")
            for prefix in ("results", "party"):
                (raw / f"{prefix}_done_242.json").write_text(json.dumps({"data": {}}), encoding="utf-8")
            calls = []
            starts = []

            def get_report(_client, path, params):
                calls.append((path, params))
                return {"data": {}}

            def checkpoints(_report, _url):
                return [SimpleNamespace(commission_id="pending")]

            with httpx.Client(base_url="http://example.test") as client, \
                 patch("election_parser.collector.checkpoint_paths", return_value=(tree_checkpoint, collect_checkpoint)), \
                 patch("election_parser.collector.LOG_DIR", root / "logs"), \
                 patch("election_parser.collector.RAW_DIR", raw), \
                 patch("election_parser.collector.COMPLETENESS_DIR", root / "processed"), \
                 patch("election_parser.collector.official_client", return_value=nullcontext(client)), \
                 patch("election_parser.collector.discover_tree", return_value=nodes), \
                 patch("election_parser.collector._save_catalog"), \
                 patch("election_parser.collector.get_json", side_effect=get_report), \
                 patch("election_parser.collector.parse_report", side_effect=checkpoints), \
                 patch("election_parser.collector.parse_result", side_effect=ValueError("неверный баланс")) as validate, \
                 patch("election_parser.collector.save_report", return_value="unchanged"), \
                 patch("election_parser.collector.check_completeness",
                       return_value={"found_uiks": 2, "complete_uiks": 2,
                                     "incomplete_uiks": [], "validation_issues": []}):
                found, counts, failures, _, report_path = collect_region(
                    "test", 0, workers=2, on_start=lambda total, done: starts.append((total, done)))
            self.assertEqual(starts, [(2, 1)])
            self.assertEqual(len(calls), 5)
            validate.assert_not_called()
            self.assertTrue(all(params["commissionClassifierId"] == "pending" for _, params in calls))
            self.assertEqual((len(found), counts["unchanged"], failures), (2, 5, []))
            self.assertFalse(collect_checkpoint.exists())
            self.assertTrue(json.loads(Path(report_path).read_text(encoding="utf-8"))["complete"])

    def test_missing_file_invalidates_completed_checkpoint(self) -> None:
        node = UikNode("stale", 1, "УИК №1", ("ЦИК", "УИК №1"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkpoint = root / "collect.jsonl"
            checkpoint.write_text(json.dumps({"version": 1, "region": "test", "limit": 0}) + "\n" +
                                  json.dumps({"commission_id": "stale"}) + "\n", encoding="utf-8")
            starts, calls = [], []

            def get_report(_client, path, params):
                calls.append((path, params))
                return {}

            with httpx.Client(base_url="http://example.test") as client, \
                 patch("election_parser.collector.checkpoint_paths", return_value=(root / "tree.json", checkpoint)), \
                 patch("election_parser.collector.LOG_DIR", root / "logs"), \
                 patch("election_parser.collector.COMPLETENESS_DIR", root / "processed"), \
                 patch("election_parser.collector.RAW_DIR", root / "raw"), \
                 patch("election_parser.collector.official_client", return_value=nullcontext(client)), \
                 patch("election_parser.collector.discover_tree", return_value=[node]), \
                 patch("election_parser.collector._save_catalog"), \
                 patch("election_parser.collector.get_json", side_effect=get_report), \
                 patch("election_parser.collector.save_report", return_value="unchanged"), \
                 patch("election_parser.collector.check_completeness",
                       return_value={"found_uiks": 1, "complete_uiks": 1,
                                     "incomplete_uiks": [], "validation_issues": []}):
                collect_region("test", 0, workers=1,
                               on_start=lambda total, done: starts.append((total, done)))
            self.assertEqual(starts, [(1, 0)])
            self.assertEqual(len(calls), 5)


if __name__ == "__main__":
    unittest.main()
