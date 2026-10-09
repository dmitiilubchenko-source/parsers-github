"""Преобразует проверенные итоговые протоколы УИК в таблицы для анализа."""

import json
from pathlib import Path

from .csv_io import write_csv, require_unique
from .checkpoints import write_checkpoint
from .collector import CATALOG
from .config import BASE_DIR, RAW_DIR
from .results import parse_result, result_api_url
from .tree import UikNode

OUTPUT_DIR = BASE_DIR / "data" / "processed" / "служебные"
SUMMARY = OUTPUT_DIR / "uik_results_2026.csv"
PROTOCOL = OUTPUT_DIR / "protocol_rows_2026.csv"
CANDIDATES = OUTPUT_DIR / "candidate_votes_2026.csv"
DISTRICTS = OUTPUT_DIR / "district_totals_2026.csv"
CANDIDATE_TOTALS = OUTPUT_DIR / "candidate_totals_2026.csv"
ISSUES = OUTPUT_DIR / "processing_issues_2026.json"


def process_results(raw_dir: Path = RAW_DIR, catalog_path: Path = CATALOG,
                    output_dir: Path = OUTPUT_DIR, *, on_progress=None) -> tuple[int, int, list[dict]]:
    """Повторно проверяет JSON и связывает его с каталогом по ID комиссии."""
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Нет каталога УИК: {catalog_path}")
    catalog = {}
    for item in json.loads(catalog_path.read_text(encoding="utf-8")):
        commission_id = item["commission_id"]
        if commission_id in catalog:
            raise ValueError(f"Повтор ID комиссии в каталоге: {commission_id}")
        catalog[commission_id] = UikNode(commission_id, item["number"], item["name"], tuple(item["path"]))
    paths = sorted(raw_dir.glob("results_*_242.json"))
    if not paths:
        raise FileNotFoundError("Нет сохранённых итоговых протоколов 242")
    summary_rows, protocol_rows, candidate_rows, issues = [], [], [], []
    seen = set()
    protocol_sources: dict[str, tuple[str, Path]] = {}
    rejected_protocols: set[str] = set()
    for index, path in enumerate(paths):
        if on_progress and index % 1000 == 0:
            on_progress(index, len(paths))
        try:
            commission_id = path.name.removeprefix("results_").removesuffix("_242.json")
            if commission_id not in catalog:
                raise ValueError("УИК отсутствует в каталоге")
            if commission_id in seen:
                raise ValueError("Повтор файла итогового протокола УИК")
            payload = json.loads(path.read_text(encoding="utf-8"))
            node = catalog[commission_id]
            result = parse_result(payload["data"], node)
            if result["uik_number"] != node.number:
                raise ValueError("Номер УИК не совпадает с каталогом")
            protocol_id = result["protocol_id"]
            if protocol_id in rejected_protocols:
                raise ValueError(f"Повтор ID протокола {protocol_id}; все его копии исключены")
            if protocol_id in protocol_sources:
                previous_id, previous_path = protocol_sources[protocol_id]
                summary_rows[:] = [row for row in summary_rows if row["commission_id"] != previous_id]
                protocol_rows[:] = [row for row in protocol_rows if row["commission_id"] != previous_id]
                candidate_rows[:] = [row for row in candidate_rows if row["commission_id"] != previous_id]
                rejected_protocols.add(protocol_id)
                issues.append({"file": str(previous_path), "error": f"ID протокола {protocol_id} повторён в {path}"})
                raise ValueError(f"ID протокола {protocol_id} повторён в {previous_path}; все копии исключены")
            common = {"region": node.path[1] if len(node.path) > 1 else "",
                      "electoral_district": node.path[2] if len(node.path) > 2 else "",
                      "commission_path": " / ".join(node.path),
                      "uik_number": node.number, "commission_id": commission_id,
                      "source_url": result_api_url(commission_id),
                      "protocol_id": result["protocol_id"], "created_at": result["created_at"]}
            p = result["protocol"]
            summary_rows.append({**common, "registered_voters": p["1"]["votes"],
                                 "ballots_received": p["2"]["votes"],
                                 "ballots_issued_early": p["3"]["votes"],
                                 "ballots_issued_at_station": p["4"]["votes"],
                                 "ballots_issued_outside": p["5"]["votes"],
                                 "ballots_cancelled": p["6"]["votes"],
                                 "ballots_in_portable_boxes": p["7"]["votes"],
                                 "ballots_in_stationary_boxes": p["8"]["votes"],
                                 "invalid_ballots": p["9"]["votes"],
                                 "valid_ballots": p["10"]["votes"],
                                 "ballots_lost": p["11"]["votes"],
                                 "ballots_unaccounted": p["12"]["votes"],
                                 "candidate_count": len(result["candidates"])})
            protocol_rows.extend({**common, "row_number": number,
                                  "row_name": p[str(number)]["name"], "value": p[str(number)]["votes"]}
                                 for number in range(1, 13))
            candidate_rows.extend({**common, "row_number": item["row"],
                                   "candidate_id": item["participant_id"],
                                   "candidate_name": item["name"], "votes": item["votes"]}
                                  for item in result["candidates"])
            protocol_sources[protocol_id] = (commission_id, path)
            seen.add(commission_id)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            issues.append({"file": str(path), "error": str(exc)})
    if on_progress:
        on_progress(len(paths), len(paths))
    common_columns = ("region", "electoral_district", "commission_path", "uik_number", "commission_id",
                      "source_url", "protocol_id", "created_at")
    require_unique(summary_rows, ("commission_id",))
    require_unique(protocol_rows, ("commission_id", "row_number"))
    require_unique(candidate_rows, ("commission_id", "row_number"))
    districts: dict[tuple[str, str], dict] = {}
    for row in summary_rows:
        key = (row["region"], row["electoral_district"])
        total = districts.setdefault(key, {"region": key[0], "electoral_district": key[1],
                                           "scope": "collected_uiks_only", "uiks_with_results": 0,
                                           "registered_voters": 0, "ballots_cast": 0,
                                           "valid_ballots": 0, "invalid_ballots": 0})
        total["uiks_with_results"] += 1
        total["registered_voters"] += row["registered_voters"]
        total["ballots_cast"] += row["ballots_in_portable_boxes"] + row["ballots_in_stationary_boxes"]
        total["valid_ballots"] += row["valid_ballots"]
        total["invalid_ballots"] += row["invalid_ballots"]
    candidate_totals: dict[tuple[str, str, str, str], dict] = {}
    for row in candidate_rows:
        identity = ("id", row["candidate_id"]) if row["candidate_id"] else ("name", row["candidate_name"])
        key = (row["region"], row["electoral_district"], *identity)
        total = candidate_totals.setdefault(key, {"region": key[0], "electoral_district": key[1],
                                                "scope": "collected_uiks_only", "row_number": row["row_number"],
                                                "candidate_id": row["candidate_id"],
                                                "candidate_name": row["candidate_name"], "votes": 0,
                                                "uiks_reporting": 0})
        if total["row_number"] != row["row_number"]:
            total["row_number"] = ""
        total["votes"] += row["votes"]
        total["uiks_reporting"] += 1
    for key, total in districts.items():
        candidate_votes = sum(item["votes"] for item in candidate_totals.values()
                              if (item["region"], item["electoral_district"]) == key)
        if candidate_votes != total["valid_ballots"]:
            raise ValueError(f"Сумма голосов участников не совпадает с действительными бюллетенями в округе {key}")
    write_csv(output_dir / SUMMARY.name, common_columns + (
        "registered_voters", "ballots_received", "ballots_issued_early",
        "ballots_issued_at_station", "ballots_issued_outside", "ballots_cancelled",
        "ballots_in_portable_boxes", "ballots_in_stationary_boxes", "invalid_ballots",
        "valid_ballots", "ballots_lost", "ballots_unaccounted", "candidate_count"), summary_rows)
    write_csv(output_dir / PROTOCOL.name, common_columns + ("row_number", "row_name", "value"), protocol_rows)
    write_csv(output_dir / CANDIDATES.name, common_columns + ("row_number", "candidate_id", "candidate_name", "votes"), candidate_rows)
    write_csv(output_dir / DISTRICTS.name, ("region", "electoral_district", "scope", "uiks_with_results",
                                              "registered_voters", "ballots_cast", "valid_ballots", "invalid_ballots"),
               list(districts.values()))
    write_csv(output_dir / CANDIDATE_TOTALS.name, ("region", "electoral_district", "scope", "row_number",
                                                     "candidate_id", "candidate_name", "votes", "uiks_reporting"),
               sorted(candidate_totals.values(), key=lambda item: (item["region"], item["electoral_district"],
                                                                     -item["votes"], item["candidate_name"])))
    write_checkpoint(output_dir / ISSUES.name, issues)
    return len(summary_rows), len(candidate_rows), issues
