import json
import tempfile
import unittest
from pathlib import Path

from election_parser.log_export import export_log_json


class LogExportTests(unittest.TestCase):
    def test_exports_cyrillic_events_as_json_array(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "collect.jsonl"
            events = [{"event": "start", "region": "Московская область"},
                      {"event": "error", "error": "Ошибка УИК"}]
            source.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n",
                              encoding="utf-8")
            target = export_log_json(source)
            self.assertEqual(target, source.with_suffix(".json"))
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), events)
            self.assertEqual(len(source.read_text(encoding="utf-8").splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
