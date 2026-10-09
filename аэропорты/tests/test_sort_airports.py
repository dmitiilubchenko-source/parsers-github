from __future__ import annotations

import unittest

from sort_airports import select


class SortAirportsTests(unittest.TestCase):
    def test_complete_checks_selected_fields_for_all_types(self) -> None:
        records = [
            {"icao_code": "ABCD", "facility_type": "heliport", "name": "Pad", "elevation_ft": 0},
            {"icao_code": "EFGH", "facility_type": "seaplane_base", "name": "Water", "elevation_ft": None},
        ]
        config = {"airport_types": [], "require_complete": True, "fields": ["icao_code", "name", "elevation_ft"]}
        self.assertEqual(select(records, config), [{"icao_code": "ABCD", "name": "Pad", "elevation_ft": 0}])

    def test_facility_fields_and_routes(self) -> None:
        records = [{
            "icao_code": "UUEE", "facility_type": "large_airport",
            "facility_fields": {"field_elevation": "622 ft MSL", "gps_code": "UUEE"},
        }]
        config = {
            "require_complete": False,
            "fields": ["icao_code", "field_elevation", "gps_code", "routes"],
        }
        routes = {"UUEE": [{"iata": "LED"}]}
        self.assertEqual(select(records, config, routes), [{
            "icao_code": "UUEE", "field_elevation": "622 ft MSL",
            "gps_code": "UUEE", "routes": [{"iata": "LED"}],
        }])

    def test_include_exclude_and_selected_fields(self) -> None:
        records = [
            {"icao_code": "UUEE", "facility_type": "large_airport", "name": "A", "extra": "drop"},
            {"icao_code": "UUDD", "facility_type": "large_airport", "name": "B"},
        ]
        config = {
            "include_icao": ["uuee", "UUDD"],
            "exclude_icao": ["uudd"],
            "airport_types": ["large_airport"],
            "require_complete": False,
            "fields": ["icao_code", "name"],
        }
        self.assertEqual(select(records, config), [{"icao_code": "UUEE", "name": "A"}])


if __name__ == "__main__":
    unittest.main()
