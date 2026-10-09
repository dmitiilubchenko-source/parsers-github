import csv
import tempfile
import unittest
from pathlib import Path

from election_parser.csv_parts import split_csv


class CSVPartsTests(unittest.TestCase):
    def test_parts_preserve_quotes_multiline_cells_and_all_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "data.csv"
            original = [["УИК", "Описание"], *[[str(i), 'Строка\nс запятой, и "кавычками"'] for i in range(30)]]
            with source.open("w", encoding="utf-8-sig", newline="") as stream:
                csv.writer(stream).writerows(original)
            before = source.read_bytes()
            result = split_csv(source, root / "view", max_bytes=250)
            restored = []
            for part in result["parts"]:
                path = root / "view" / "data" / part["file"]
                self.assertLessEqual(path.stat().st_size, 250)
                with path.open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.reader(stream)
                    self.assertEqual(next(reader), original[0])
                    restored.extend(reader)
            self.assertEqual(restored, original[1:])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(split_csv(source, root / "view", max_bytes=250), result)


if __name__ == "__main__":
    unittest.main()
