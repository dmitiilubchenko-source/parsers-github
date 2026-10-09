"""Полный CSV текущих исходных ответов: одна строка на УИК, без отбора полей."""

import base64
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .collector import CATALOG, DATES
from .config import BASE_DIR, RAW_DIR
from .results import parse_result
from .tree import UikNode
from .voting_flow import parse_report, validate_progress

OUTPUT_DIR = BASE_DIR / "data" / "processed"
CSV_NAME = "00_Все_данные_УИК_2026.csv"
VERSION = 1
BASE_COLUMNS = ("commission_id", "uik_number", "uik_name", "region", "commission_path",
                "saved_reports", "all_five_saved", "validation_issue_count")
REPORTS = tuple((f"daily_{day}", f"report_{{id}}_{day}.json", day) for day in DATES) + (
    ("candidates", "results_{id}_242.json", 1), ("parties", "party_{id}_242.json", 2))


def flatten(value, prefix: str, output: dict) -> None:
    """Пути полей в формате JSON Pointer; индексы массивов сохраняют порядок."""
    if isinstance(value, dict) and value:
        for key, child in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            flatten(child, f"{prefix}/{escaped}", output)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            flatten(child, f"{prefix}/{index}", output)
    elif value is None or isinstance(value, (dict, list, bool)):
        output[prefix] = json.dumps(value, ensure_ascii=False)
    else:
        output[prefix] = value


def input_signature(item: dict, raw_dir: Path) -> str:
    parts = [VERSION, item]
    for _, filename, _ in REPORTS:
        path = raw_dir / filename.format(id=item["commission_id"])
        try:
            stat = path.stat()
            parts.append([stat.st_size, stat.st_mtime_ns])
        except FileNotFoundError:
            parts.append(None)
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def build_row(item: dict, raw_dir: Path) -> dict:
    node = UikNode(item["commission_id"], item["number"], item["name"], tuple(item["path"]))
    row = {"commission_id": node.commission_id, "uik_number": node.number, "uik_name": node.name,
           "region": node.path[1] if len(node.path) > 1 else "", "commission_path": " / ".join(node.path)}
    flatten(item, "catalog", row)
    saved = 0
    issues = 0
    daily_points = []
    for prefix, filename, kind in REPORTS:
        path = raw_dir / filename.format(id=node.commission_id)
        row[f"checks/{prefix}/file"] = path.name
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            row[f"checks/{prefix}/status"] = "missing"
            continue
        except OSError as exc:
            row[f"checks/{prefix}/status"] = "unreadable"
            row[f"checks/{prefix}/error"] = str(exc)
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeError as exc:
            row[f"checks/{prefix}/status"] = "invalid_encoding"
            row[f"checks/{prefix}/raw_bytes_base64"] = base64.b64encode(content).decode("ascii")
            row[f"checks/{prefix}/error"] = str(exc)
            continue
        try:
            payload = json.loads(text)
        except ValueError as exc:
            row[f"checks/{prefix}/status"] = "invalid_json"
            row[f"checks/{prefix}/raw_text"] = text
            row[f"checks/{prefix}/error"] = str(exc)
            continue
        # В CSV попадают все поля, даже если проверка содержимого не проходит.
        flatten(payload, prefix, row)
        if not isinstance(payload, dict) or "data" not in payload or "url" not in payload:
            row[f"checks/{prefix}/status"] = "invalid_envelope"
            continue
        saved += 1
        row[f"checks/{prefix}/status"] = "saved"
        try:
            if isinstance(kind, str):
                points = parse_report(payload, payload["url"])
                if any(point.commission_id != node.commission_id or point.uik_number != node.number
                       or point.election_date.isoformat() != kind for point in points):
                    raise ValueError("Дата, ID или номер УИК не совпадает с файлом и каталогом")
                daily_points.extend(points)
            else:
                parse_result(payload["data"], node, kind)
            row[f"checks/{prefix}/validation"] = "ok"
        except Exception as exc:
            issues += 1
            row[f"checks/{prefix}/validation"] = "issue"
            row[f"checks/{prefix}/validation_error"] = str(exc)
    try:
        validate_progress(daily_points)
    except ValueError as exc:
        issues += 1
        row["daily_sequence/validation_error"] = str(exc)
    row.update(saved_reports=saved, all_five_saved=int(saved == 5), validation_issue_count=issues)
    return row


def _prepare(item: dict, old_signature: str | None, raw_dir: Path):
    signature = input_signature(item, raw_dir)
    if signature == old_signature:
        return item["commission_id"], signature, None
    for _ in range(2):
        row = build_row(item, raw_dir)
        after = input_signature(item, raw_dir)
        if after == signature:
            return item["commission_id"], signature, row
        signature = after
    raise RuntimeError(f"Файлы УИК {item['commission_id']} изменяются во время экспорта")


def export_all(raw_dir: Path = RAW_DIR, catalog_path: Path = CATALOG,
               output_dir: Path = OUTPUT_DIR, *, workers: int = 4, limit: int = 0,
               on_progress=None) -> dict:
    """Читает исходные ответы один раз, кеширует строки и формирует CSV потоково."""
    if not 1 <= workers <= 8 or limit < 0:
        raise ValueError("workers должен быть от 1 до 8, limit — неотрицательным")
    started = time.monotonic()
    items = json.loads(catalog_path.read_text(encoding="utf-8"))
    ids = [item["commission_id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("В каталоге повторяется ID комиссии; экспорт остановлен")
    if limit:
        items = items[:limit]
    if not items:
        raise ValueError("Каталог УИК пуст")
    output_dir.mkdir(parents=True, exist_ok=True)
    service_dir = output_dir / "служебные"
    service_dir.mkdir(exist_ok=True)
    cache_path = service_dir / "all_data_cache.sqlite3"
    reused = 0
    finished = 0
    stage = output_dir / f".export-{uuid4().hex}"
    stage.mkdir()
    try:
        with closing(sqlite3.connect(cache_path)) as database, database:
            database.execute("CREATE TABLE IF NOT EXISTS rows (id TEXT PRIMARY KEY, signature TEXT, payload TEXT)")
            database.execute("CREATE TABLE IF NOT EXISTS active (id TEXT PRIMARY KEY, position INTEGER)")
            database.execute("DELETE FROM active")
            database.executemany("INSERT INTO active VALUES (?, ?)",
                                 ((item["commission_id"], index) for index, item in enumerate(items)))
            database.commit()
            signatures = dict(database.execute("SELECT id, signature FROM rows"))
            iterator = iter(items)
            if on_progress:
                on_progress("reading", 0, len(items), 0)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                active = set()

                def submit_next():
                    try:
                        item = next(iterator)
                    except StopIteration:
                        return
                    active.add(executor.submit(_prepare, item, signatures.get(item["commission_id"]), raw_dir))

                for _ in range(workers * 2):
                    submit_next()
                while active:
                    done, active = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        cid, signature, row = future.result()
                        if row is None:
                            reused += 1
                        else:
                            database.execute("INSERT OR REPLACE INTO rows VALUES (?, ?, ?)",
                                             (cid, signature, json.dumps(row, ensure_ascii=False)))
                        finished += 1
                        if finished % 100 == 0 or finished == len(items):
                            database.commit()
                            if on_progress:
                                on_progress("reading", finished, len(items), reused)
                        submit_next()
            query = "SELECT rows.payload FROM active JOIN rows USING(id) ORDER BY active.position"
            columns = set(BASE_COLUMNS)
            coverage = {}
            status_counts = {prefix: Counter() for prefix, _, _ in REPORTS}
            validation_counts = {prefix: 0 for prefix, _, _ in REPORTS}
            complete = 0
            issues = 0
            protocols = {"candidates": {}, "parties": {}}
            duplicate_protocols = {"candidates": set(), "parties": set()}
            if on_progress:
                on_progress("summary", 0, len(items), reused)
            for index, (payload,) in enumerate(database.execute(query), 1):
                row = json.loads(payload)
                columns.update(row)
                region = coverage.setdefault(row["region"], {"region": row["region"], "catalog_uiks": 0,
                    "all_five_saved": 0, "validation_issues": 0,
                    **{f"{prefix}_saved": 0 for prefix, _, _ in REPORTS}})
                region["catalog_uiks"] += 1
                region["all_five_saved"] += row["all_five_saved"]
                region["validation_issues"] += row["validation_issue_count"]
                complete += row["all_five_saved"]
                issues += row["validation_issue_count"]
                for prefix, _, _ in REPORTS:
                    status = row[f"checks/{prefix}/status"]
                    status_counts[prefix][status] += 1
                    region[f"{prefix}_saved"] += int(status == "saved")
                    validation_counts[prefix] += int(row.get(f"checks/{prefix}/validation") == "issue")
                for prefix in protocols:
                    protocol = row.get(f"{prefix}/data/body/protocolId")
                    if protocol:
                        if protocol in protocols[prefix]:
                            duplicate_protocols[prefix].add(protocol)
                        protocols[prefix][protocol] = row["commission_id"]
                if on_progress and (index % 1000 == 0 or index == len(items)):
                    on_progress("summary", index, len(items), reused)
            ordered_columns = list(BASE_COLUMNS) + sorted(columns - set(BASE_COLUMNS))
            if on_progress:
                on_progress("writing", 0, len(items), reused)
            with (stage / CSV_NAME).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=ordered_columns)
                writer.writeheader()
                for index, (payload,) in enumerate(database.execute(query), 1):
                    writer.writerow(json.loads(payload))
                    if on_progress and (index % 1000 == 0 or index == len(items)):
                        on_progress("writing", index, len(items), reused)
            with (stage / "all_data_columns_2026.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("column", "meaning"))
                for column in ordered_columns:
                    writer.writerow((column, "Поле исходного JSON; /число — индекс массива, ~1 — /, ~0 — ~"
                                     if "/" in column else "Показатель каталога или полноты УИК"))
            with (stage / "coverage_by_region_2026.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                regions = sorted(coverage.values(), key=lambda row: row["region"])
                writer = csv.DictWriter(stream, fieldnames=list(regions[0]))
                writer.writeheader()
                writer.writerows(regions)
            summary = {"created_at": datetime.now(timezone.utc).isoformat(),
                       "scope": "current_saved_reports_for_catalog_uiks", "catalog_uiks": len(items),
                       "limited_sample": bool(limit), "rows": len(items), "columns": len(ordered_columns),
                       "complete_uiks": complete, "incomplete_uiks": len(items) - complete,
                       "source_validation_issues": issues, "reports": status_counts,
                       "report_validation_issues": validation_counts,
                       "duplicate_protocol_ids": {key: len(value) for key, value in duplicate_protocols.items()},
                       "reused_cached_uiks": reused, "elapsed_seconds": round(time.monotonic() - started, 2),
                       "csv": str(output_dir / CSV_NAME), "cache": str(cache_path)}
            (stage / "general_report_2026.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            description = (f"# Отчёт по сохранённым данным\n\n"
                f"В каталоге: {len(items)} УИК. В полном CSV: {len(items)} строк и {len(ordered_columns)} столбцов.\n\n"
                f"Все пять ответов сохранены для {complete} УИК; неполных: {len(items) - complete}. "
                f"Замечаний по содержимому: {issues}.\n\n"
                "Каждая строка — один УИК, определяемый ID комиссии. Все поля текущих JSON сохранены; "
                "отсутствующие ответы отмечены статусом missing, повреждённые — отдельным статусом. "
                "Пустая ячейка означает отсутствие поля; null, [], {} сохранены текстом. "
                "Пути столбцов соответствуют JSON Pointer; номера в путях — позиции в массиве источника. "
                "Номера позиций кандидатов могут обозначать разных людей в разных округах: перед суммированием "
                "нужно сверять их ID и названия.\n\n"
                "Общий CSV сохраняет и ответы с замечаниями. Национальные суммы голосов здесь не вычисляются; "
                "для проверенных сводок используются отдельные таблицы команды process. "
                "Полнота относится к найденному каталогу, независимое официальное число УИК не сверено. "
                "Адреса и населённые пункты не добавлялись, если их нет в источнике. "
                "Исторические версии из data/history не входят в этот экспорт.\n\n"
                "Полнота по регионам: служебные/coverage_by_region_2026.csv. "
                "Список столбцов: служебные/all_data_columns_2026.csv.\n")
            (stage / "general_report_2026.md").write_text(description, encoding="utf-8")
            for name in (CSV_NAME, "all_data_columns_2026.csv", "coverage_by_region_2026.csv",
                         "general_report_2026.md", "general_report_2026.json"):
                destination = output_dir / name if name == CSV_NAME else service_dir / name
                if name == "general_report_2026.md":
                    destination = output_dir / "Общий_отчёт_полнота_2026.md"
                os.replace(stage / name, destination)
            return summary
    finally:
        resolved_stage = stage.resolve()
        if resolved_stage.parent != output_dir.resolve() or not resolved_stage.name.startswith(".export-"):
            raise RuntimeError("Небезопасный путь временного каталога экспорта")
        shutil.rmtree(resolved_stage)
