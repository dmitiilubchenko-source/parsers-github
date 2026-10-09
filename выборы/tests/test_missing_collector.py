import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx

from election_parser.missing_collector import collect_missing


class MissingCollectorTests(unittest.TestCase):
    def test_fetches_only_missing_reports_without_tree_or_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            processed, raw = root / "processed", root / "raw"
            processed.mkdir()
            raw.mkdir()
            for day in ("2026-09-18", "2026-09-19", "2026-09-20"):
                (raw / f"report_pending_{day}.json").write_text(
                    json.dumps({"url": f"http://example.test?date={day}", "data": {}}), encoding="utf-8")
            (raw / "party_pending_242.json").write_text(
                json.dumps({"url": "http://example.test", "data": {}}), encoding="utf-8")
            missing = [{"report": 242, "protocol_num": 1}]
            baseline = {"region": "all", "limit": 0, "found_uiks": 2, "complete_uiks": 1,
                        "incomplete_uiks": [{"commission_id": "pending", "uik_number": 7,
                                             "missing_or_invalid": missing}],
                        "validation_issues": [{"commission_id": "done", "reason": "старое замечание"}],
                        "tree_errors": 0}
            (processed / "completeness_old.json").write_text(
                json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
            calls = []

            def fake_get(_client, path, params):
                calls.append((path, params))
                return {}

            with httpx.Client(base_url="http://example.test") as client, \
                 patch("election_parser.missing_collector.COMPLETENESS_DIR", processed), \
                 patch("election_parser.missing_collector.RAW_DIR", raw), \
                 patch("election_parser.missing_collector.LOG_DIR", root / "logs"), \
                 patch("election_parser.missing_collector.checkpoint_paths",
                       return_value=(root / "tree.json", root / "collect.jsonl")), \
                 patch("election_parser.missing_collector.official_client", return_value=nullcontext(client)), \
                 patch("election_parser.missing_collector.get_json", side_effect=fake_get):
                counts, failures, log_path, report_path = collect_missing(workers=1)

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1]["protocolNum"], 1)
            self.assertEqual(counts["new"], 1)
            self.assertEqual(failures, [])
            report = json.loads(Path(report_path).read_text(encoding="utf-8"))
            self.assertEqual((report["complete_uiks"], report["found_uiks"]), (2, 2))
            self.assertIn("старое замечание", [item.get("reason") for item in report["validation_issues"]])
            self.assertTrue(Path(log_path).with_suffix(".json").is_file())

    def test_skips_answers_saved_by_interrupted_missing_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            processed, raw = root / "processed", root / "raw"
            processed.mkdir()
            raw.mkdir()
            (processed / "completeness_old.json").write_text(json.dumps({
                "region": "all", "limit": 0, "found_uiks": 1, "complete_uiks": 0,
                "incomplete_uiks": [{"commission_id": "pending", "uik_number": 7,
                                     "missing_or_invalid": [{"report": 242, "protocol_num": 2}]}],
                "validation_issues": []}), encoding="utf-8")
            (raw / "party_pending_242.json").write_text(
                json.dumps({"url": "http://example.test", "data": {}}), encoding="utf-8")
            with patch("election_parser.missing_collector.COMPLETENESS_DIR", processed), \
                 patch("election_parser.missing_collector.RAW_DIR", raw), \
                 patch("election_parser.missing_collector.LOG_DIR", root / "logs"), \
                 patch("election_parser.missing_collector.checkpoint_paths",
                       return_value=(root / "tree.json", root / "collect.jsonl")), \
                 patch("election_parser.missing_collector.official_client") as client, \
                 patch("election_parser.missing_collector.check_completeness",
                       return_value={"incomplete_uiks": [], "validation_issues": []}):
                counts, failures, _, report_path = collect_missing()
            client.assert_not_called()
            self.assertEqual((counts["new"], failures), (0, []))
            self.assertTrue(json.loads(Path(report_path).read_text(encoding="utf-8"))["complete"])


if __name__ == "__main__":
    unittest.main()
