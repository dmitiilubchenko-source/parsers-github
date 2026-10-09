"""Быстрая дозагрузка ответов, отсутствующих по последнему отчёту полноты."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable
from uuid import uuid4

from .checkpoints import checkpoint_paths, write_checkpoint
from .collector import (COMPLETENESS_DIR, DATES, LOG_DIR, RAW_DIR, _run_bounded,
                        adjust_pacing, check_completeness, save_report)
from .log_export import export_log_json
from .results import PARTY_PROTOCOL, result_api_url
from .site_client import get_json, official_client
from .tree import UikNode


def _latest_national_report() -> tuple[Path, dict]:
    for path in sorted(COMPLETENESS_DIR.glob("completeness_*.json"),
                       key=lambda item: item.stat().st_mtime, reverse=True):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("region", "").casefold() == "all" and report.get("limit") == 0:
            return path, report
    raise FileNotFoundError("Нет отчёта полноты для --region all --limit 0")


def _job(node: UikNode, missing: dict) -> tuple[str, Path, str, dict, str | None]:
    report = missing["report"]
    if report == 453:
        day = missing["date"]
        if day not in DATES:
            raise ValueError(f"Неизвестная дата в отчёте полноты: {day}")
        params = {"commissionClassifierId": node.commission_id, "date": day}
        return ("daily_report", RAW_DIR / f"report_{node.commission_id}_{day}.json",
                "/reports/453", params, day)
    if report == 242 and missing.get("protocol_num") in (1, PARTY_PROTOCOL):
        protocol = missing["protocol_num"]
        stage = "final_report" if protocol == 1 else "party_report"
        prefix = "results" if protocol == 1 else "party"
        params = {"commissionClassifierId": node.commission_id, "protocolNum": protocol}
        return (stage, RAW_DIR / f"{prefix}_{node.commission_id}_242.json",
                "/reports/242", params, None)
    raise ValueError(f"Неизвестный отчёт в отчёте полноты: {missing}")


def _available(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(payload, dict) and "data" in payload and "url" in payload
    except (OSError, ValueError, TypeError):
        return False


def collect_missing(*, workers: int = 2, delay: float = 0.0,
                    on_progress: Callable[[UikNode, int, int, int], None] | None = None,
                    on_start: Callable[[int, int], None] | None = None,
                    ) -> tuple[dict[str, int], list[dict], str, str]:
    """Дозагружает только отсутствующие ответы из последнего полного обхода дерева."""
    if not 1 <= workers <= 8:
        raise ValueError("Число одновременных УИК должно быть от 1 до 8")
    if delay < 0:
        raise ValueError("Пауза между запросами не может быть отрицательной")
    source, previous = _latest_national_report()
    incomplete = previous["incomplete_uiks"]
    if previous["found_uiks"] != previous["complete_uiks"] + len(incomplete):
        raise ValueError(f"Несогласованный отчёт полноты: {source}")
    nodes = [UikNode(item["commission_id"], item["uik_number"],
                     f"УИК №{item['uik_number']}", ()) for item in incomplete]
    jobs_by_id = {}
    for node, item in zip(nodes, incomplete):
        jobs = [_job(node, missing) for missing in item["missing_or_invalid"]]
        jobs_by_id[node.commission_id] = [job for job in jobs if not _available(job[1])]
    pending = [node for node in nodes if jobs_by_id[node.commission_id]]
    resumed = len(nodes) - len(pending)
    counts = {"new": 0, "updated": 0, "unchanged": 0}
    failures: list[dict] = []
    pacing = {"delay": delay, "next_at": 0.0, "successes": 0}
    http_errors = {429: 0, 502: 0, 503: 0}
    completed = resumed
    session_refreshes = 0
    lock = RLock()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    log_path = LOG_DIR / f"collect_missing_{run_id}.jsonl"
    completeness_path = COMPLETENESS_DIR / f"completeness_{run_id}.json"

    with log_path.open("x", encoding="utf-8") as journal:
        def record(event: dict) -> None:
            with lock:
                journal.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event},
                                         ensure_ascii=False) + "\n")
                journal.flush()

        def pace() -> None:
            with lock:
                now = time.monotonic()
                slot = max(now, pacing["next_at"])
                pacing["next_at"] = slot + pacing["delay"]
            if slot > now:
                time.sleep(slot - now)

        record({"event": "start", "mode": "missing_only", "source_report": str(source),
                "found_uiks": previous["found_uiks"], "incomplete_uiks": len(nodes),
                "pending_uiks": len(pending), "workers": workers, "delay_seconds": delay})
        if on_start is not None:
            on_start(len(nodes), resumed)
        try:
            if pending:
                with official_client() as client:
                    def observe_response(response) -> None:
                        with lock:
                            if response.status_code in http_errors:
                                http_errors[response.status_code] += 1
                            new_delay = adjust_pacing(pacing, response.status_code, delay)
                            if new_delay is not None:
                                record({"event": "throttle" if response.status_code in (429, 502, 503) else "recover",
                                        "http_status": response.status_code, "delay_seconds": new_delay})

                    client.event_hooks["response"].append(observe_response)

                    def fetch_node(node: UikNode) -> None:
                        nonlocal completed
                        node_errors = 0
                        for stage, path, endpoint, params, day in jobs_by_id[node.commission_id]:
                            try:
                                pace()
                                report = get_json(client, endpoint, params)
                                url = (str(client.build_request("GET", endpoint, params=params).url)
                                       if day else result_api_url(node.commission_id, params["protocolNum"]))
                                status = save_report(path, {"url": url, "data": report})
                                with lock:
                                    counts[status] += 1
                                record({"event": status, "stage": stage,
                                        "commission_id": node.commission_id, "date": day,
                                        "report": int(endpoint.rsplit("/", 1)[-1]),
                                        "protocol_num": params.get("protocolNum")})
                            except Exception as exc:
                                node_errors += 1
                                details = {"stage": stage, "commission_id": node.commission_id,
                                           "uik_number": node.number, "date": day, "error": str(exc),
                                           "report": int(endpoint.rsplit("/", 1)[-1]),
                                           "protocol_num": params.get("protocolNum")}
                                with lock:
                                    failures.append(details)
                                record({"event": "error", **details})
                        with lock:
                            completed += 1
                            if on_progress is not None:
                                on_progress(node, completed, len(nodes), node_errors)

                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        _run_bounded(executor, pending, workers, fetch_node)
                    session_refreshes = getattr(client, "_session_refresh_count", 0)

            checked = check_completeness(nodes, RAW_DIR)
            still_missing = checked["incomplete_uiks"]
            targeted_ids = {node.commission_id for node in nodes}
            validation = [issue for issue in previous.get("validation_issues", [])
                          if issue.get("commission_id") not in targeted_ids]
            validation.extend(checked["validation_issues"])
            result = {"found_uiks": previous["found_uiks"],
                      "complete_uiks": previous["found_uiks"] - len(still_missing),
                      "incomplete_uiks": still_missing, "validation_issues": validation,
                      "region": "all", "limit": 0, "mode": "missing_only",
                      "source_report": str(source), "run_errors": len(failures),
                      "tree_errors": previous.get("tree_errors", 0),
                      "session_refreshes": session_refreshes,
                      "complete": not still_missing and not previous.get("tree_errors", 0) and not failures}
            write_checkpoint(completeness_path, result)
            record({"event": "completeness", "complete_uiks": result["complete_uiks"],
                    "found_uiks": result["found_uiks"], "complete": result["complete"],
                    "validation_issues": len(validation), "path": str(completeness_path)})
            if result["complete"]:
                tree_checkpoint, collect_checkpoint = checkpoint_paths("all", 0)
                tree_checkpoint.unlink(missing_ok=True)
                collect_checkpoint.unlink(missing_ok=True)
        except Exception as exc:
            record({"event": "error", "stage": "run", "error": str(exc)})
            raise RuntimeError(f"Дозагрузка прервана; журнал: {log_path}") from exc
        finally:
            record({"event": "finish", "new_reports": counts["new"],
                    "updated_reports": counts["updated"], "unchanged_reports": counts["unchanged"],
                    "errors": len(failures), "http_429": http_errors[429],
                    "http_502": http_errors[502], "http_503": http_errors[503],
                    "final_delay_seconds": pacing["delay"]})
            export_log_json(log_path)
    return counts, failures, str(log_path), str(completeness_path)
