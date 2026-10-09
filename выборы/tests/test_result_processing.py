import csv
import json
import tempfile
import unittest
from pathlib import Path

from election_parser.result_processing import process_results


class ResultProcessingTests(unittest.TestCase):
    def test_joins_by_commission_id_and_skips_invalid_protocol(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "uik1_results_242.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw, out = root / "raw", root / "processed"
            raw.mkdir()
            catalog = []
            for suffix, region in (("a", "Регион А"), ("b", "Регион Б"), ("bad", "Регион Б")):
                commission_id = f"commission-{suffix}"
                catalog.append({"commission_id": commission_id, "number": 1,
                                "name": "УИК №1", "path": ["ЦИК России", region, "УИК №1"]})
                report = json.loads(json.dumps(fixture))
                report["body"]["commissionClassifierId"] = commission_id
                report["body"]["protocolId"] = f"protocol-{suffix}"
                if suffix == "bad":
                    report["body"]["records"][0]["value"] = "999"
                (raw / f"results_{commission_id}_242.json").write_text(
                    json.dumps({"data": report}), encoding="utf-8")
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
            uiks, candidates, issues = process_results(raw, catalog_path, out)
            self.assertEqual((uiks, candidates, len(issues)), (2, 14, 1))
            with (out / "uik_results_2026.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({row["commission_id"] for row in rows}, {"commission-a", "commission-b"})
            self.assertEqual({row["region"] for row in rows}, {"Регион А", "Регион Б"})
            self.assertTrue(all(row["valid_ballots"] == "2100" for row in rows))
            with (out / "candidate_votes_2026.csv").open(encoding="utf-8-sig", newline="") as stream:
                votes = list(csv.DictReader(stream))
            self.assertEqual(len(votes), 14)
            with (out / "candidate_totals_2026.csv").open(encoding="utf-8-sig", newline="") as stream:
                totals = list(csv.DictReader(stream))
            self.assertEqual(len(totals), 14)
            self.assertEqual(sum(int(row["votes"]) for row in totals), 4200)
            with (out / "district_totals_2026.csv").open(encoding="utf-8-sig", newline="") as stream:
                districts = list(csv.DictReader(stream))
            self.assertEqual(sum(int(row["ballots_cast"]) for row in districts), 4200)

    def test_duplicate_protocol_id_excludes_both_copies(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "uik1_results_242.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "raw"
            raw.mkdir()
            catalog = []
            for commission_id in ("uik-a", "uik-b"):
                report = json.loads(json.dumps(fixture))
                report["body"]["commissionClassifierId"] = commission_id
                catalog.append({"commission_id": commission_id, "number": 1,
                                "name": "УИК №1", "path": ["ЦИК", commission_id]})
                (raw / f"results_{commission_id}_242.json").write_text(json.dumps({"data": report}), encoding="utf-8")
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            uiks, candidates, issues = process_results(raw, catalog_path, root / "out")
            self.assertEqual((uiks, candidates, len(issues)), (0, 0, 2))

    def test_candidate_totals_follow_names_when_row_order_changes(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "uik1_results_242.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "raw"
            raw.mkdir()
            catalog = []
            for index, commission_id in enumerate(("uik-a", "uik-b")):
                report = json.loads(json.dumps(fixture))
                report["body"]["commissionClassifierId"] = commission_id
                report["body"]["protocolId"] = f"protocol-{commission_id}"
                if index:
                    for record in report["body"]["records"]:
                        if record["infoPrintNum"] in ("13", "14"):
                            record["infoPrintNum"] = "14" if record["infoPrintNum"] == "13" else "13"
                catalog.append({"commission_id": commission_id, "number": index + 1,
                                "name": f"УИК №{index + 1}",
                                "path": ["ЦИК", "Регион", "Округ", f"УИК №{index + 1}"]})
                (raw / f"results_{commission_id}_242.json").write_text(
                    json.dumps({"data": report}), encoding="utf-8")
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
            uiks, _, issues = process_results(raw, catalog_path, root / "out")
            with (root / "out" / "candidate_totals_2026.csv").open(encoding="utf-8-sig", newline="") as stream:
                totals = list(csv.DictReader(stream))
            self.assertEqual((uiks, issues), (2, []))
            self.assertEqual(len(totals), 7)
            self.assertTrue(all(row["uiks_reporting"] == "2" for row in totals))
            self.assertEqual(sum(int(row["votes"]) for row in totals), 4200)

    def test_candidate_id_joins_name_variants(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "uik1_results_242.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "raw"
            raw.mkdir()
            catalog = []
            for index, commission_id in enumerate(("uik-a", "uik-b")):
                report = json.loads(json.dumps(fixture))
                report["body"]["commissionClassifierId"] = commission_id
                report["body"]["protocolId"] = f"protocol-{commission_id}"
                for record in report["body"]["records"]:
                    number = int(record["infoPrintNum"])
                    if number >= 13:
                        record["id"] = f"candidate-{number}"
                        if index and number == 13:
                            record["infoText"] += " (вариант написания)"
                catalog.append({"commission_id": commission_id, "number": index + 1,
                                "name": f"УИК №{index + 1}",
                                "path": ["ЦИК", "Регион", "Округ", f"УИК №{index + 1}"]})
                (raw / f"results_{commission_id}_242.json").write_text(json.dumps({"data": report}), encoding="utf-8")
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
            process_results(raw, catalog_path, root / "out")
            with (root / "out" / "candidate_totals_2026.csv").open(encoding="utf-8-sig", newline="") as stream:
                totals = list(csv.DictReader(stream))
            self.assertEqual(len(totals), 7)
            self.assertEqual(next(row for row in totals if row["candidate_id"] == "candidate-13")["uiks_reporting"], "2")


if __name__ == "__main__":
    unittest.main()
