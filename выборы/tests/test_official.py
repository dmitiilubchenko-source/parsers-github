import unittest

from election_parser.official import daily_urls, parse_link


class OfficialLinkTests(unittest.TestCase):
    def test_voting_flow_points_to_results(self) -> None:
        link = parse_link(
            "http://www.izbirkom.ru/election/587813923/commission/"
            "7c0e12f5-ed9c-4a14-8784-ccfa7aa5f1cc/voting-flow?type=2&report=453"
        )
        self.assertEqual(link.report_id, 453)
        self.assertIn("type=2&report=453", daily_urls(link)["2026-09-18"])
        self.assertIn("type=4&report=453", daily_urls(link)["2026-09-19"])
        self.assertIn("type=6&report=453", daily_urls(link)["2026-09-20"])


if __name__ == "__main__":
    unittest.main()
