"""Повторная загрузка отчётов УИК с ошибками, включая неудачные обновления."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .collector import CATALOG, DATES, LOG_DIR, save_report
from .config import RAW_DIR
from .log_export import export_log_json
from .results import PARTY_PROTOCOL, result_api_url
from .site_client import get_json, official_client
from .tree import UikNode


def failed_jobs(log_path: Path) -> list[tuple[str, str, str | None]]:
    """Уникальные неудачные отчёты; ошибки обхода дерева сюда не входят."""
    jobs = set()
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event") != "error":
            continue
        stage, commission_id = event.get("stage"), event.get("commission_id")
        if stage == "daily_report" and commission_id and event.get("date") in DATES:
            jobs.add((stage, commission_id, event["date"]))
        elif stage == "final_report" and commission_id and event.get("report", 242) == 242:
            jobs.add((stage, commission_id, None))
        elif (stage == "party_report" and commission_id and event.get("report", 242) == 242
              and event.get("protocol_num", PARTY_PROTOCOL) == PARTY_PROTOCOL):
            jobs.add((stage, commission_id, None))
    return sorted(jobs)


def retry_reports(source_log: Path) -> tuple[int, int, str]:
    source_log = Path(source_log).resolve()
    if not source_log.is_file():
        raise FileNotFoundError(source_log)
    jobs = failed_jobs(source_log)
    if not CATALOG.is_file():
        raise FileNotFoundError(f"Каталог УИК отсутствует: {CATALOG}")
    catalog = {item["commission_id"]: UikNode(item["commission_id"], item["number"],
                                               item["name"], tuple(item["path"]))
               for item in json.loads(CATALOG.read_text(encoding="utf-8"))}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    output = LOG_DIR / f"retry_{run_id}.jsonl"
    saved = errors = 0
    with output.open("x", encoding="utf-8") as journal:
        def record(event: dict) -> None:
            journal.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False) + "\n")
            journal.flush()

        record({"event": "start", "source_log": str(source_log), "jobs": len(jobs)})
        try:
            pending = []
            for stage, commission_id, day in jobs:
                target = (RAW_DIR / f"report_{commission_id}_{day}.json" if stage == "daily_report" else
                          RAW_DIR / f"results_{commission_id}_242.json" if stage == "final_report" else
                          RAW_DIR / f"party_{commission_id}_242.json")
                if commission_id not in catalog:
                    errors += 1
                    record({"event": "error", "stage": stage, "commission_id": commission_id,
                            "date": day, "error": "УИК отсутствует в каталоге"})
                    continue
                pending.append((stage, catalog[commission_id], day, target))
            if pending:
                with official_client() as client:
                    for stage, node, day, target in pending:
                        try:
                            if stage == "daily_report":
                                params = {"commissionClassifierId": node.commission_id, "date": day}
                                report = get_json(client, "/reports/453", params)
                                url = str(client.build_request("GET", "/reports/453", params=params).url)
                                payload = {"url": url, "data": report}
                            else:
                                protocol_num = PARTY_PROTOCOL if stage == "party_report" else 1
                                report = get_json(client, "/reports/242", {"commissionClassifierId": node.commission_id,
                                                                             "protocolNum": protocol_num})
                                payload = {"url": result_api_url(node.commission_id, protocol_num), "data": report}
                            status = save_report(target, payload)
                            if status != "unchanged":
                                saved += 1
                            record({"event": status, "stage": stage, "commission_id": node.commission_id,
                                    "date": day, "path": str(target)})
                            time.sleep(0.4)
                        except Exception as exc:
                            errors += 1
                            record({"event": "error", "stage": stage, "commission_id": node.commission_id,
                                    "date": day, "error": str(exc)})
        except Exception as exc:
            errors += 1
            record({"event": "error", "stage": "run", "error": str(exc)})
        finally:
            record({"event": "finish", "saved": saved, "errors": errors})
            export_log_json(output)
    return saved, errors, str(output)
