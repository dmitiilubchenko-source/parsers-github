import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


class ProcessCommandTests(unittest.TestCase):
    def test_full_export_is_saved_before_summary_failure(self):
        stages = []

        def export(**_kwargs):
            stages.append("full_csv")
            return {"csv": "test.csv"}

        def fail(**_kwargs):
            stages.append("daily")
            raise OSError("Файл недоступен")

        with patch("sys.argv", ["main.py", "process"]), \
             patch("election_parser.full_export.export_all", side_effect=export), \
             patch("election_parser.cli.process_saved_reports", side_effect=fail), \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "Файл недоступен"):
                main.main()
        self.assertEqual(stages, ["full_csv", "daily"])


if __name__ == "__main__":
    unittest.main()
