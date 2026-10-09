import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from election_parser.party_processing import process_party_results
from election_parser.result_processing import process_results


class ProtocolIOTests(unittest.TestCase):
    def test_unreadable_file_does_not_drop_other_protocols(self):
        for prefix, fixture_name, process in (
                ("results", "uik1_results_242.json", process_results),
                ("party", "uik1_parties_242.json", process_party_results)):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                raw = root / "raw"
                raw.mkdir()
                report = json.loads((Path(__file__).parent / "fixtures" / fixture_name).read_text(encoding="utf-8"))
                items = []
                for cid in ("good", "bad"):
                    items.append({"commission_id": cid, "number": 1, "name": "УИК №1",
                                  "path": ["ЦИК", "Регион", "Округ"]})
                    report["body"]["commissionClassifierId"] = cid
                    (raw / f"{prefix}_{cid}_242.json").write_text(json.dumps({"data": report}), encoding="utf-8")
                catalog = root / "catalog.json"
                catalog.write_text(json.dumps(items), encoding="utf-8")
                original_read = Path.read_text

                def read(path, *args, **kwargs):
                    if path.name == f"{prefix}_bad_242.json":
                        raise OSError("Ошибка чтения")
                    return original_read(path, *args, **kwargs)

                with patch.object(Path, "read_text", read):
                    uiks, _, issues = process(raw, catalog, root / "out")
                self.assertEqual(uiks, 1)
                self.assertEqual(len(issues), 1)
                self.assertEqual(issues[0]["error"], "Ошибка чтения")


if __name__ == "__main__":
    unittest.main()
