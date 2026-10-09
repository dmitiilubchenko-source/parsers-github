import json
import unittest
from pathlib import Path

from election_parser.results import parse_result
from election_parser.tree import UikNode


class ResultTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).parent / "fixtures" / "uik1_results_242.json"
        self.report = json.loads(fixture.read_text(encoding="utf-8"))
        self.node = UikNode(self.report["body"]["commissionClassifierId"], 1, "УИК №1", ("ЦИК России", "Республика Адыгея"))

    def test_real_protocol_balances(self):
        result = parse_result(self.report, self.node)
        self.assertEqual(result["protocol"]["10"]["votes"], 2100)
        self.assertEqual(sum(x["votes"] for x in result["candidates"]), 2100)

    def test_rejects_wrong_commission(self):
        self.report["body"]["commissionClassifierId"] = "wrong"
        with self.assertRaises(ValueError):
            parse_result(self.report, self.node)

    def test_rejects_tampered_votes(self):
        self.report["body"]["records"][0]["value"] = "999"
        with self.assertRaises(ValueError):
            parse_result(self.report, self.node)


if __name__ == "__main__":
    unittest.main()
