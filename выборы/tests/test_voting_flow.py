import json
import unittest
from pathlib import Path

from election_parser.voting_flow import parse_report, validate_progress

FIXTURES = Path(__file__).parent / "fixtures"


def read(label: str, day: int):
    payload = json.loads((FIXTURES / f"pilot_{label}_day{day}.json").read_text(encoding="utf-8"))
    return parse_report(payload, payload["url"])


class VotingFlowTests(unittest.TestCase):
    def test_three_days_of_uik1(self) -> None:
        checkpoints = [item for day in (1, 2, 3) for item in read("uik1", day)]
        validate_progress(checkpoints)
        self.assertEqual(len(checkpoints), 8)
        last_by_day = {item.election_date.isoformat(): item for item in checkpoints}
        self.assertEqual(last_by_day["2026-09-18"].voters_count, 773)
        self.assertEqual(last_by_day["2026-09-19"].voters_count, 1552)
        self.assertEqual(last_by_day["2026-09-20"].voters_count, 1880)

    def test_tik_agrees_with_uik(self) -> None:
        uik_id = read("uik1", 1)[0].commission_id
        for day in (1, 2):
            parent = [item for item in read("tik", day) if item.commission_id == uik_id]
            child = read("uik1", day)
            self.assertEqual(
                [(item.time, item.voters_count, item.voters_percent) for item in parent],
                [(item.time, item.voters_count, item.voters_percent) for item in child],
            )

    def test_rejects_incorrect_count(self) -> None:
        payload = json.loads((FIXTURES / "pilot_uik1_day1.json").read_text(encoding="utf-8"))
        payload["data"]["body"]["commissionClassifiers"][0]["votersCount"] = -1
        with self.assertRaisesRegex(ValueError, "число избирателей"):
            parse_report(payload, payload["url"])


if __name__ == "__main__":
    unittest.main()
