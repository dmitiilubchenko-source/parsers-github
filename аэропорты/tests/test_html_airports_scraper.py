from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "html_airports_scraper.py"
SPEC = importlib.util.spec_from_file_location("html_airports_scraper", SCRIPT)
assert SPEC and SPEC.loader
SCRAPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRAPER
SPEC.loader.exec_module(SCRAPER)


class HtmlAirportScraperTests(unittest.TestCase):
    def cached_html(self, icao: str) -> str:
        return (ROOT / "tests" / "fixtures" / "html" / icao / "runways.html").read_text(
            encoding="utf-8"
        )

    def test_uuee_facility_and_runways(self) -> None:
        html = self.cached_html("UUEE")
        record = SCRAPER.parse_airport_document(
            html,
            "UUEE",
            "https://ourairports.com/airports/UUEE/runways.html",
            "https://ourairports.com/airports/UUEE/",
            "https://ourairports.com/airports/UUEE/runways.html",
        )

        self.assertEqual(record["_id"], "UUEE")
        self.assertEqual(record["iata_code"], "SVO")
        self.assertEqual(record["country_code"], "RU")
        self.assertEqual(record["region_code"], "RU-MOS")
        self.assertEqual(record["runways_count"], 3)
        self.assertEqual(record["runways"][0]["name"], "06C/24C")
        self.assertEqual(record["runways"][0]["length_ft"], 11647)
        self.assertTrue(record["runways"][0]["lighted"])

    def test_not_lighted_is_false_and_removed_from_surface(self) -> None:
        html = self.cached_html("AGAF")
        runways = SCRAPER.parse_runways_page(html)

        self.assertEqual(len(runways), 1)
        self.assertFalse(runways[0]["lighted"])
        self.assertNotIn("lighted", runways[0]["surface"].lower())
        self.assertNotIn(", not", runways[0]["surface"].lower())

    def test_all_cached_pages_have_matching_facility_data(self) -> None:
        cache = ROOT / "tests" / "fixtures" / "html"
        sample_icaos = [
            "AGAF", "AGAR", "AGAT", "AGBA", "AGBT",
            "AGEV", "AGGA", "AGGB", "AGGC", "AGGE", "UUEE",
        ]
        pages = [cache / icao / "runways.html" for icao in sample_icaos]

        for page in pages:
            with self.subTest(icao=page.parent.name):
                icao = page.parent.name
                record = SCRAPER.parse_facility_page(
                    page.read_text(encoding="utf-8"), icao
                )
                self.assertEqual(record["icao_code"], icao)
                self.assertTrue(record["name"])

    def test_local_csv_fallback_builds_missing_html_airport(self) -> None:
        dataset = SCRAPER.LocalCsvDataset(ROOT / "tests" / "fixtures" / "local_input")
        record = dataset.record("AGVH")

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["icao_code"], "AGVH")
        self.assertEqual(record["source_type"], "LOCAL_CSV")
        self.assertTrue(record["name"])
        self.assertIsInstance(record["runways"], list)

    def test_local_only_process_does_not_use_downloader(self) -> None:
        dataset = SCRAPER.LocalCsvDataset(ROOT / "tests" / "fixtures" / "local_input")
        record, status = SCRAPER.process_one(
            "AGVH", None, ROOT / "tests" / "fixtures" / "html", dataset, local_only=True
        )

        self.assertIsNotNone(record)
        self.assertTrue(status["success"])
        self.assertEqual(status["source_type"], "LOCAL_CSV")


if __name__ == "__main__":
    unittest.main()
