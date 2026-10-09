"""Таблицы протоколов УИК по федеральным спискам партий."""

import json
from pathlib import Path

from .checkpoints import write_checkpoint
from .collector import CATALOG
from .config import RAW_DIR
from .result_processing import OUTPUT_DIR
from .csv_io import require_unique, write_csv
from .results import PARTY_PROTOCOL, parse_result, result_api_url
from .tree import UikNode

PARTY_UIKS = OUTPUT_DIR / "party_uik_results_2026.csv"
PARTY_PROTOCOL_ROWS = OUTPUT_DIR / "party_protocol_rows_2026.csv"
PARTY_VOTES = OUTPUT_DIR / "party_votes_2026.csv"
PARTY_TOTALS = OUTPUT_DIR / "party_totals_2026.csv"
PARTY_ISSUES = OUTPUT_DIR / "party_processing_issues_2026.json"


def process_party_results(raw_dir: Path = RAW_DIR, catalog_path: Path = CATALOG,
                          output_dir: Path = OUTPUT_DIR, *, on_progress=None) -> tuple[int, int, list[dict]]:
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Нет каталога УИК: {catalog_path}")
    catalog = {}
    for item in json.loads(catalog_path.read_text(encoding="utf-8")):
        commission_id = item["commission_id"]
        if commission_id in catalog:
            raise ValueError(f"Повтор ID комиссии в каталоге: {commission_id}")
        catalog[commission_id] = UikNode(commission_id, item["number"], item["name"], tuple(item["path"]))
    paths = sorted(raw_dir.glob("party_*_242.json"))
    if not paths:
        raise FileNotFoundError("Нет сохранённых партийных протоколов УИК")
    entries, issues = [], []
    by_protocol: dict[str, list[tuple[Path, dict, UikNode]]] = {}
    for index, path in enumerate(paths):
        if on_progress and index % 1000 == 0:
            on_progress(index, len(paths))
        try:
            commission_id = path.name.removeprefix("party_").removesuffix("_242.json")
            node = catalog[commission_id]
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = parse_result(payload["data"], node, PARTY_PROTOCOL)
            by_protocol.setdefault(result["protocol_id"], []).append((path, result, node))
        except (OSError, KeyError, ValueError, TypeError, AttributeError, IndexError) as exc:
            issues.append({"file": str(path), "error": str(exc)})
    if on_progress:
        on_progress(len(paths), len(paths))
    for protocol_id, copies in by_protocol.items():
        if len(copies) != 1:
            issues.extend({"file": str(path), "error": f"Повтор ID протокола {protocol_id}; все копии исключены"}
                          for path, _, _ in copies)
        else:
            entries.append(copies[0])
    by_protocol.clear()
    uik_rows, protocol_rows, party_rows = [], [], []
    for index in range(len(entries)):
        path, result, node = entries[index]
        entries[index] = None  # Освобождаем исходный протокол по мере создания строк CSV.
        common = {"region": node.path[1] if len(node.path) > 1 else "",
                  "electoral_district": node.path[2] if len(node.path) > 2 else "",
                  "commission_path": " / ".join(node.path), "uik_number": node.number,
                  "commission_id": node.commission_id, "protocol_id": result["protocol_id"],
                  "created_at": result["created_at"],
                  "source_url": result_api_url(node.commission_id, PARTY_PROTOCOL)}
        p = result["protocol"]
        uik_rows.append({**common, "registered_voters": p["1"]["votes"],
                         "ballots_cast": p["7"]["votes"] + p["8"]["votes"],
                         "invalid_ballots": p["9"]["votes"], "valid_ballots": p["10"]["votes"],
                         "party_count": len(result["parties"])})
        protocol_rows.extend({**common, "row_number": n, "row_name": p[str(n)]["name"],
                              "value": p[str(n)]["votes"]} for n in range(1, 13))
        party_rows.extend({**common, "row_number": x["row"], "party_name": x["name"],
                           "votes": x["votes"]} for x in result["parties"])
    require_unique(uik_rows, ("commission_id",))
    require_unique(protocol_rows, ("commission_id", "row_number"))
    require_unique(party_rows, ("commission_id", "row_number"))
    totals = {}
    for row in party_rows:
        key = row["party_name"]
        total = totals.setdefault(key, {"scope": "collected_uiks_only", "row_number": row["row_number"],
                                        "party_name": key, "votes": 0, "uiks_reporting": 0})
        if total["row_number"] != row["row_number"]:
            total["row_number"] = ""
        total["votes"] += row["votes"]
        total["uiks_reporting"] += 1
    if sum(x["votes"] for x in totals.values()) != sum(x["valid_ballots"] for x in uik_rows):
        raise ValueError("Сумма голосов партий не равна действительным бюллетеням в собранных УИК")
    common_columns = ("region", "electoral_district", "commission_path", "uik_number",
                      "commission_id", "protocol_id", "created_at", "source_url")
    write_csv(output_dir / PARTY_UIKS.name, common_columns + (
        "registered_voters", "ballots_cast", "invalid_ballots", "valid_ballots", "party_count"), uik_rows)
    write_csv(output_dir / PARTY_PROTOCOL_ROWS.name, common_columns + ("row_number", "row_name", "value"), protocol_rows)
    write_csv(output_dir / PARTY_VOTES.name, common_columns + ("row_number", "party_name", "votes"), party_rows)
    write_csv(output_dir / PARTY_TOTALS.name, ("scope", "row_number", "party_name", "votes", "uiks_reporting"),
               sorted(totals.values(), key=lambda item: (-item["votes"], item["row_number"])))
    output_dir.mkdir(parents=True, exist_ok=True)
    write_checkpoint(output_dir / PARTY_ISSUES.name, issues)
    return len(uik_rows), len(party_rows), issues
