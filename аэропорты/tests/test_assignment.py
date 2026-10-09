import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.airport_parser.enrichment import enrich


class AssignmentTests(unittest.TestCase):
    def test_closed_airport_and_moscow_timezone(self):
        r = enrich(dict(facility_type='closed', latitude_deg=55.97, longitude_deg=37.41))
        self.assertEqual(r['status'], 'close')
        self.assertEqual(r['timezone'], 'Europe/Moscow')

    def test_missing_coordinates_are_not_guessed(self):
        r = enrich(dict(facility_type='small_airport', latitude_deg=None, longitude_deg=None))
        self.assertEqual(r['status'], 'open')
        self.assertIsNone(r['timezone'])
