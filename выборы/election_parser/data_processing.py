"""Обрабатывает сохранённые ответы отчёта 453 отдельно от их загрузки."""

import csv
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .checkpoints import write_checkpoint
from .config import BASE_DIR, RAW_DIR
from .voting_flow import Checkpoint, parse_report, validate_progress

OUTPUT = BASE_DIR / "data" / "processed" / "служебные" / "voting_flow_2026.csv"
ISSUES = BASE_DIR / "data" / "processed" / "служебные" / "voting_flow_issues_2026.json"


def _is_direct(item: Checkpoint) -> bool:
    return parse_qs(urlparse(item.source_url).query).get("commissionClassifierId") == [item.commission_id]


def process_saved_reports(raw_dir: Path = RAW_DIR, output: Path = OUTPUT,
                          issues_path: Path | None = None, *, on_progress=None) -> list[Checkpoint]:
    paths = sorted(raw_dir.glob("report_*.json"))
    if not paths:
        paths = sorted(raw_dir.glob("pilot_*_day*.json"))
    if not paths:
        raise FileNotFoundError("Нет сохранённых отчётов 453")
    merged: dict[tuple[str, str, str], Checkpoint] = {}
    conflicts: set[tuple[str, str, str]] = set()
    issues: list[dict] = []
    for index, path in enumerate(paths):
        if on_progress and index % 1000 == 0:
            on_progress(index, len(paths))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            parsed = parse_report(payload, payload["url"])
        except Exception as exc:
            issues.append({"file": str(path), "error": str(exc)})
            continue
        for item in parsed:
            key = (item.election_date.isoformat(), item.commission_id, item.time)
            if key in conflicts:
                continue
            previous = merged.get(key)
            if previous is not None and (previous.voters_count, previous.voters_percent, previous.uik_number) != (
                item.voters_count, item.voters_percent, item.uik_number
            ):
                issues.append({"commission_id": item.commission_id, "uik_number": item.uik_number,
                               "error": f"Конфликт данных для {key}",
                               "source_urls": [previous.source_url, item.source_url]})
                conflicts.add(key)
                merged.pop(key)
                continue
            if previous is None or _is_direct(item):
                merged[key] = item
    if on_progress:
        on_progress(len(paths), len(paths))
    by_uik: dict[str, list[Checkpoint]] = {}
    for item in merged.values():
        by_uik.setdefault(item.commission_id, []).append(item)
    result = []
    for commission_id, items in by_uik.items():
        result.extend(items)
        try:
            validate_progress(items)
        except ValueError as exc:
            issues.append({"commission_id": commission_id, "uik_number": items[0].uik_number,
                           "error": str(exc), "source_urls": sorted({item.source_url for item in items})})
    result.sort(key=lambda item: (item.uik_number, item.election_date, item.time))
    output.parent.mkdir(parents=True, exist_ok=True)
    issue_output = issues_path or output.with_name(ISSUES.name)
    temporary = output.with_name(f"{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("date", "time", "uik_number", "commission_id", "voters_count_cumulative", "voters_percent", "source_url"))
            for item in result:
                writer.writerow((item.election_date, item.time, item.uik_number, item.commission_id, item.voters_count, item.voters_percent, item.source_url))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    write_checkpoint(issue_output, issues)
    return result
