import csv
import json
import tempfile
import unittest
from pathlib import Path

from election_parser.party_processing import process_party_results
from election_parser.results import PARTY_PROTOCOL, parse_result
from election_parser.tree import UikNode


class PartyProtocolTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent / "fixtures" / "uik1_parties_242.json"
        self.report = json.loads(path.read_text(encoding="utf-8"))
        self.node = UikNode(self.report["body"]["commissionClassifierId"], 1, "УИК №1",
                            ("ЦИК России", "Республика Адыгея", "Адыгейский", "УИК №1"))

    def test_party_votes_match_valid_ballots(self):
        result = parse_result(self.report, self.node, PARTY_PROTOCOL)
        self.assertEqual(result["protocol_num"], 2)
        self.assertEqual(len(result["parties"]), 10)
        self.assertEqual(sum(x["votes"] for x in result["parties"]), result["protocol"]["10"]["votes"])

    def test_rejects_party_report_as_candidate_protocol(self):
        with self.assertRaises(ValueError):
            parse_result(self.report, self.node)

    def test_processing_excludes_duplicate_party_protocol_id(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "raw"
            raw.mkdir()
            catalog = []
            for commission_id in ("uik-a", "uik-b"):
                report = json.loads(json.dumps(self.report))
                report["body"]["commissionClassifierId"] = commission_id
                catalog.append({"commission_id": commission_id, "number": 1, "name": "УИК №1",
                                "path": ["ЦИК", "Регион", "Округ", "УИК №1"]})
                (raw / f"party_{commission_id}_242.json").write_text(
                    json.dumps({"data": report}), encoding="utf-8")
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            uiks, rows, issues = process_party_results(raw, catalog_path, root / "out")
            self.assertEqual((uiks, rows, len(issues)), (0, 0, 2))
            with (root / "out" / "party_votes_2026.csv").open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 0)

    def test_processing_exports_every_party(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "raw"
            raw.mkdir()
            (raw / f"party_{self.node.commission_id}_242.json").write_text(
                json.dumps({"data": self.report}), encoding="utf-8")
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps([{"commission_id": self.node.commission_id,
                                            "number": 1, "name": self.node.name,
                                            "path": list(self.node.path)}]), encoding="utf-8")
            uiks, rows, issues = process_party_results(raw, catalog, root / "out")
            self.assertEqual((uiks, rows, issues), (1, 10, []))
            with (root / "out" / "party_votes_2026.csv").open(encoding="utf-8-sig", newline="") as stream:
                votes = list(csv.DictReader(stream))
            self.assertEqual(sum(int(row["votes"]) for row in votes),
                             parse_result(self.report, self.node, PARTY_PROTOCOL)["protocol"]["10"]["votes"])

    def test_party_totals_follow_names_when_row_order_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "raw"
            raw.mkdir()
            catalog = []
            for index, commission_id in enumerate(("uik-a", "uik-b")):
                report = json.loads(json.dumps(self.report))
                report["body"]["commissionClassifierId"] = commission_id
                report["body"]["protocolId"] = f"protocol-{commission_id}"
                if index:
                    for record in report["body"]["records"]:
                        if record["infoPrintNum"] in ("13", "14"):
                            record["infoPrintNum"] = "14" if record["infoPrintNum"] == "13" else "13"
                catalog.append({"commission_id": commission_id, "number": index + 1,
                                "name": f"УИК №{index + 1}",
                                "path": ["ЦИК", "Регион", "Округ", f"УИК №{index + 1}"]})
                (raw / f"party_{commission_id}_242.json").write_text(json.dumps({"data": report}), encoding="utf-8")
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
            uiks, _, issues = process_party_results(raw, catalog_path, root / "out")
            with (root / "out" / "party_totals_2026.csv").open(encoding="utf-8-sig", newline="") as stream:
                totals = list(csv.DictReader(stream))
            self.assertEqual((uiks, issues), (2, []))
            self.assertEqual(len(totals), 10)
            self.assertTrue(all(row["uiks_reporting"] == "2" for row in totals))


if __name__ == "__main__":
    unittest.main()
