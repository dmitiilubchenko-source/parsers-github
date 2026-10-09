from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "clean_complete_airports.py"
SPEC = importlib.util.spec_from_file_location("clean_complete_airports", SCRIPT)
assert SPEC and SPEC.loader
CLEANER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CLEANER
SPEC.loader.exec_module(CLEANER)


def complete_record() -> dict:
    return {
        "_id": "UUEE",
        "icao_code": "UUEE",
        "name": "Sheremetyevo",
        "facility_type": "large_airport",
        "latitude_deg": 55.9726,
        "longitude_deg": 37.4146,
        "elevation_ft": 622,
        "country_code": "RU",
        "country_name": "Russia",
        "region_code": "RU-MOS",
        "region_name": "Moscow Oblast",
        "runways": [
            {
                "name": "06C/24C",
                "length_ft": 11647,
                "width_ft": 197,
                "surface": "concrete",
                "lighted": True,
            }
        ],
    }


class CompleteAirportCleanerTests(unittest.TestCase):
    def test_accepts_complete_airport(self) -> None:
        self.assertTrue(CLEANER.is_complete_airport(complete_record()))

    def test_rejects_optional_non_airport_type(self) -> None:
        record = complete_record()
        record["facility_type"] = "heliport"
        self.assertFalse(CLEANER.is_complete_airport(record))

    def test_rejects_missing_airport_field(self) -> None:
        record = complete_record()
        record["region_code"] = None
        self.assertFalse(CLEANER.is_complete_airport(record))

    def test_rejects_missing_runway_field(self) -> None:
        record = complete_record()
        record["runways"][0]["width_ft"] = None
        self.assertFalse(CLEANER.is_complete_airport(record))

    def test_false_lighted_is_valid_value(self) -> None:
        record = complete_record()
        record["runways"][0]["lighted"] = False
        self.assertTrue(CLEANER.is_complete_airport(record))


if __name__ == "__main__":
    unittest.main()
