import csv
import json
import tempfile
import unittest
from pathlib import Path

from election_parser.party_processing import process_party_results
from election_parser.result_processing import process_results
from election_parser.report_tables import make_tables


class ReportTablesTests(unittest.TestCase):
    def test_keeps_losing_candidates_and_partial_district_is_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw, source, output = root / "raw", root / "source", root / "out"
            raw.mkdir()
            source.mkdir()
            catalog = [{"commission_id": cid, "number": number, "name": f"УИК №{number}",
                        "path": ["ЦИК", "Регион", "Округ", f"УИК №{number}"]}
                       for cid, number in (("uik", 1), ("missing", 2))]
            catalog_path = source / "uik_catalog_2026.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            fixtures = Path(__file__).parent / "fixtures"
            for prefix, name in (("results", "uik1_results_242.json"), ("party", "uik1_parties_242.json")):
                report = json.loads((fixtures / name).read_text(encoding="utf-8"))
                report["body"]["commissionClassifierId"] = "uik"
                (raw / f"{prefix}_uik_242.json").write_text(json.dumps({"data": report}), encoding="utf-8")
            process_results(raw, catalog_path, source)
            process_party_results(raw, catalog_path, source)
            files = make_tables(source, output, on_progress=lambda _: None)
            self.assertEqual(len(files), 7)
            with (output / files[0]).open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 7)
            self.assertEqual(sum(int(r["Голосов"]) for r in rows), 2100)
            self.assertTrue(any(r["Максимум_голосов_на_УИК"] == "0" for r in rows))
            with (output / files[-1]).open(encoding="utf-8-sig", newline="") as stream:
                leader = next(csv.DictReader(stream))
            self.assertIn("неполные данные", leader["Статус"])
            self.assertIn("официальный мандат не определён", leader["Статус"])


if __name__ == "__main__":
    unittest.main()
