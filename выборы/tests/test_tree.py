import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from election_parser.tree import discover_tree


class TreeTests(unittest.TestCase):
    def test_walks_from_cik_to_six_uiks(self) -> None:
        nodes = json.loads((Path(__file__).parent / "fixtures" / "tree_adigea.json").read_text(encoding="utf-8"))

        def fake_get_json(_client, _path, params):
            return nodes[params.get("classifierId", "root")]

        with patch("election_parser.tree.get_json", side_effect=fake_get_json):
            found = discover_tree(None, region="Республика Адыгея", limit=6)
        self.assertEqual([node.number for node in found], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(node.path[0] == "ЦИК России" for node in found))
        self.assertTrue(all(node.name.startswith("УИК") for node in found))

    def test_failed_branch_is_logged_and_siblings_continue(self) -> None:
        nodes = json.loads((Path(__file__).parent / "fixtures" / "tree_adigea.json").read_text(encoding="utf-8"))
        nodes["root"]["children"].insert(0, {"externalId": "broken", "name": "Тестовый регион",
                                               "type": 2, "hasChildren": True})
        errors = []

        def fake_get_json(_client, _path, params):
            key = params.get("classifierId", "root")
            if key == "broken":
                raise RuntimeError("сетевая ошибка")
            return nodes[key]

        with patch("election_parser.tree.get_json", side_effect=fake_get_json):
            found = discover_tree(None, region="all", limit=0, on_error=errors.append)
        self.assertEqual([node.number for node in found], [1, 2, 3, 4, 5, 6])
        self.assertEqual(errors[0]["commission_id"], "broken")

    def test_resumes_tree_from_saved_stack(self) -> None:
        nodes = json.loads((Path(__file__).parent / "fixtures" / "tree_adigea.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "tree.json"

            def interrupted(_client, _path, params):
                if params.get("classifierId"):
                    raise KeyboardInterrupt
                return nodes["root"]

            with patch("election_parser.tree.get_json", side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    discover_tree(None, region="Республика Адыгея", limit=6,
                                  checkpoint_path=checkpoint)
            self.assertTrue(checkpoint.is_file())

            def resumed(_client, _path, params):
                self.assertIn("classifierId", params)
                return nodes[params["classifierId"]]

            with patch("election_parser.tree.get_json", side_effect=resumed):
                found = discover_tree(None, region="Республика Адыгея", limit=6,
                                      checkpoint_path=checkpoint)
            self.assertEqual([node.number for node in found], [1, 2, 3, 4, 5, 6])
            self.assertTrue(json.loads(checkpoint.read_text(encoding="utf-8"))["complete"])


if __name__ == "__main__":
    unittest.main()
