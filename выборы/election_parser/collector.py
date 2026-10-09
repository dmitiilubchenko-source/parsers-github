"""Сборщик: дерево комиссий и отчёты УИК за три дня."""

import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import RLock
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .checkpoints import checkpoint_paths, write_checkpoint
from .log_export import export_log_json
from .config import BASE_DIR, RAW_DIR
from .official import ElectionLink, daily_urls
from .site_client import get_json, official_client
from .results import PARTY_PROTOCOL, parse_result, result_api_url, result_url
from .tree import ELECTION_ID, UikNode, discover_tree
from .voting_flow import parse_report

DATES = ("2026-09-18", "2026-09-19", "2026-09-20")
CATALOG = BASE_DIR / "data" / "processed" / "служебные" / "uik_catalog_2026.json"
LOG_DIR = BASE_DIR / "data" / "logs"
HISTORY_DIR = BASE_DIR / "data" / "history"
COMPLETENESS_DIR = BASE_DIR / "data" / "processed" / "служебные"


def adjust_pacing(pacing: dict, status: int, base_delay: float) -> float | None:
    """Замедляется при перегрузке и возвращается к исходному темпу после успехов."""
    if status in (429, 502, 503):
        pacing["successes"] = 0
        pacing["delay"] = max(base_delay, min(2.0, max(0.25, pacing["delay"] * 2)))
        return pacing["delay"]
    if 200 <= status < 300 and pacing["delay"] > base_delay:
        pacing["successes"] += 1
        if pacing["successes"] >= 30:
            pacing["successes"] = 0
            reduced = pacing["delay"] / 2
            pacing["delay"] = base_delay if reduced <= max(base_delay, 0.03125) else reduced
            return pacing["delay"]
    return None


def save_report(path: Path, payload: dict, history_dir: Path = HISTORY_DIR) -> str:
    """Сохраняет полученный JSON; прежнюю версию сохраняет при изменении."""
    old_bytes = path.read_bytes() if path.exists() else None
    if old_bytes is not None:
        try:
            old_data = json.loads(old_bytes)["data"]
            old_valid = True
        except (ValueError, KeyError, TypeError):
            old_valid = False
        if old_valid and old_data == payload["data"]:
            return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    new_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(new_bytes)
        if old_bytes is not None:
            history_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archived = history_dir / f"{path.stem}_{stamp}_{uuid4().hex[:8]}.json"
            archived.write_bytes(old_bytes)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return "updated" if old_bytes is not None else "new"


def _save_catalog(nodes: list[UikNode]) -> None:
    items_by_id = {}
    if CATALOG.exists():
        for item in json.loads(CATALOG.read_text(encoding="utf-8")):
            items_by_id[item["commission_id"]] = item
    for node in nodes:
        item = asdict(node)
        item["daily_urls"] = daily_urls(ElectionLink(ELECTION_ID, node.commission_id, 453))
        item["result_url"] = result_url(node)
        item["party_result_api_url"] = result_api_url(node.commission_id, PARTY_PROTOCOL)
        items_by_id[node.commission_id] = item
    write_checkpoint(CATALOG, list(items_by_id.values()))


def _stored_five_available(node: UikNode, raw_dir: Path) -> bool:
    paths = [*(raw_dir / f"report_{node.commission_id}_{day}.json" for day in DATES),
             raw_dir / f"results_{node.commission_id}_242.json",
             raw_dir / f"party_{node.commission_id}_242.json"]
    try:
        for path in paths:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or "data" not in payload:
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def _run_bounded(executor: ThreadPoolExecutor, nodes, workers: int, fetch_node) -> None:
    """Не создаёт задачу на каждый УИК сразу; работает на Python 3.10+."""
    pending = iter(nodes)
    active = set()

    def submit_next() -> bool:
        try:
            node = next(pending)
        except StopIteration:
            return False
        active.add(executor.submit(fetch_node, node))
        return True

    for _ in range(workers * 2):
        if not submit_next():
            break
    while active:
        finished, active = wait(active, return_when=FIRST_COMPLETED)
        for future in finished:
            future.result()
            submit_next()


def check_completeness(nodes: list[UikNode], raw_dir: Path = RAW_DIR) -> dict:
    """Сверяет пять сохранённых ответов для каждого найденного УИК."""
    incomplete, validation_issues = [], []
    for node in nodes:
        missing = []
        for day in DATES:
            path = raw_dir / f"report_{node.commission_id}_{day}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                report = payload["data"]
                source = payload["url"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                missing.append({"report": 453, "date": day, "reason": str(exc)})
                continue
            try:
                rows = parse_report(report, source)
                if any(row.commission_id != node.commission_id for row in rows):
                    raise ValueError("ID комиссии не совпадает")
            except Exception as exc:
                validation_issues.append({"commission_id": node.commission_id, "uik_number": node.number,
                                          "report": 453, "date": day, "reason": str(exc)})
        for protocol_num, prefix in ((1, "results"), (PARTY_PROTOCOL, "party")):
            path = raw_dir / f"{prefix}_{node.commission_id}_242.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                report = payload["data"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                missing.append({"report": 242, "protocol_num": protocol_num, "reason": str(exc)})
                continue
            try:
                parse_result(report, node, protocol_num)
            except Exception as exc:
                validation_issues.append({"commission_id": node.commission_id, "uik_number": node.number,
                                          "report": 242, "protocol_num": protocol_num,
                                          "reason": str(exc)})
        if missing:
            incomplete.append({"commission_id": node.commission_id, "uik_number": node.number,
                               "missing_or_invalid": missing})
    return {"found_uiks": len(nodes), "complete_uiks": len(nodes) - len(incomplete),
            "incomplete_uiks": incomplete, "validation_issues": validation_issues}


def collect_region(region: str = "Республика Адыгея", limit: int = 6, delay: float = 0.0,
                   workers: int = 2,
                   on_progress: Callable[[UikNode, int, int, int], None] | None = None,
                   on_start: Callable[[int, int], None] | None = None,
                   fresh: bool = False,
                   ) -> tuple[list[UikNode], dict[str, int], list[dict], str, str]:
    """Сохраняет ответы и отдельный журнал событий для каждого запуска."""
    if delay < 0:
        raise ValueError("Пауза между запросами не может быть отрицательной")
    if not 1 <= workers <= 8:
        raise ValueError("Число одновременных УИК должно быть от 1 до 8")
    failures: list[dict] = []
    counts = {"new": 0, "updated": 0, "unchanged": 0}
    http_errors = {429: 0, 502: 0, 503: 0}
    pacing = {"delay": delay, "next_at": 0.0, "successes": 0}
    nodes: list[UikNode] = []
    completed = 0
    tree_checkpoint, collect_checkpoint = checkpoint_paths(region, limit)
    if fresh:
        tree_checkpoint.unlink(missing_ok=True)
        collect_checkpoint.unlink(missing_ok=True)
    checkpoint_ids: set[str] = set()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    log_path = LOG_DIR / f"collect_{run_id}.jsonl"
    completeness_path = COMPLETENESS_DIR / f"completeness_{run_id}.json"
    with log_path.open("x", encoding="utf-8") as journal:
        journal_lock = RLock()

        def record(event: dict) -> None:
            with journal_lock:
                journal.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False) + "\n")
                journal.flush()

        def error(details: dict) -> None:
            with journal_lock:
                failures.append(details)
                record({"event": "error", **details})

        def success(event: dict) -> None:
            with journal_lock:
                counts[event["event"]] += 1
                record(event)

        def pace() -> None:
            with journal_lock:
                now = time.monotonic()
                slot = max(now, pacing["next_at"])
                pacing["next_at"] = slot + pacing["delay"]
            if slot > now:
                time.sleep(slot - now)

        record({"event": "start", "region": region, "limit": limit, "delay_seconds": delay,
                "workers": workers})
        try:
            with official_client() as client:
                def observe_response(response) -> None:
                    with journal_lock:
                        if response.status_code in http_errors:
                            http_errors[response.status_code] += 1
                        new_delay = adjust_pacing(pacing, response.status_code, delay)
                        if new_delay is not None:
                            record({"event": "throttle" if response.status_code in (429, 502, 503) else "recover",
                                    "http_status": response.status_code, "delay_seconds": new_delay})

                client.event_hooks["response"].append(observe_response)
                nodes = discover_tree(client, region=region, limit=limit, on_error=error,
                                      checkpoint_path=tree_checkpoint)
                _save_catalog(nodes)
                if collect_checkpoint.exists():
                    checkpoint_text = collect_checkpoint.read_text(encoding="utf-8")
                    if checkpoint_text and not checkpoint_text.endswith("\n"):
                        checkpoint_text = checkpoint_text.rsplit("\n", 1)[0] + "\n"
                        collect_checkpoint.write_text(checkpoint_text, encoding="utf-8")
                    lines = checkpoint_text.splitlines()
                    state = json.loads(lines[0]) if lines else {}
                    if (state.get("version") != 1 or state.get("limit") != limit
                            or str(state.get("region", "")).casefold() != region.casefold()):
                        raise ValueError(f"Контрольная точка сбора не подходит: {collect_checkpoint}")
                    for line in lines[1:]:
                        event = json.loads(line)
                        if event.get("event") == "invalidate":
                            checkpoint_ids.discard(event["commission_id"])
                        else:
                            checkpoint_ids.add(event["commission_id"])
                    if not checkpoint_ids <= {node.commission_id for node in nodes}:
                        raise ValueError("Контрольная точка содержит неизвестные УИК")
                else:
                    collect_checkpoint.parent.mkdir(parents=True, exist_ok=True)
                    collect_checkpoint.write_text(json.dumps({"version": 1, "region": region,
                                                              "limit": limit}, ensure_ascii=False) + "\n",
                                                  encoding="utf-8")
                for node in nodes:
                    if node.commission_id in checkpoint_ids and not _stored_five_available(node, RAW_DIR):
                        checkpoint_ids.remove(node.commission_id)
                        with collect_checkpoint.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps({"event": "invalidate",
                                                     "commission_id": node.commission_id}) + "\n")
                completed = len(checkpoint_ids)
                if on_start is not None:
                    on_start(len(nodes), completed)
                def fetch_node(node: UikNode) -> None:
                    nonlocal completed
                    node_errors = 0
                    for day in DATES:
                        path = RAW_DIR / f"report_{node.commission_id}_{day}.json"
                        try:
                            pace()
                            report = get_json(client, "/reports/453", {"commissionClassifierId": node.commission_id, "date": day})
                            source = str(client.build_request("GET", "/reports/453", params={"commissionClassifierId": node.commission_id, "date": day}).url)
                            status = save_report(path, {"url": source, "data": report})
                            success({"event": status, "stage": "daily_report", "commission_id": node.commission_id,
                                     "date": day})
                        except Exception as exc:
                            node_errors += 1
                            error({"stage": "daily_report", "uik_number": node.number,
                                   "commission_id": node.commission_id, "date": day, "error": str(exc)})
                    path = RAW_DIR / f"results_{node.commission_id}_242.json"
                    try:
                        pace()
                        report = get_json(client, "/reports/242", {"commissionClassifierId": node.commission_id, "protocolNum": 1})
                        status = save_report(path, {"url": result_api_url(node.commission_id), "data": report})
                        success({"event": status, "stage": "final_report", "commission_id": node.commission_id,
                                 "report": 242})
                    except Exception as exc:
                        node_errors += 1
                        error({"stage": "final_report", "uik_number": node.number,
                               "commission_id": node.commission_id, "report": 242, "error": str(exc)})
                    path = RAW_DIR / f"party_{node.commission_id}_242.json"
                    try:
                        pace()
                        report = get_json(client, "/reports/242", {"commissionClassifierId": node.commission_id,
                                                                     "protocolNum": PARTY_PROTOCOL})
                        status = save_report(path, {"url": result_api_url(node.commission_id, PARTY_PROTOCOL),
                                                    "data": report})
                        success({"event": status, "stage": "party_report", "commission_id": node.commission_id,
                                 "report": 242, "protocol_num": PARTY_PROTOCOL})
                    except Exception as exc:
                        node_errors += 1
                        error({"stage": "party_report", "uik_number": node.number,
                               "commission_id": node.commission_id, "report": 242,
                               "protocol_num": PARTY_PROTOCOL, "error": str(exc)})
                    with journal_lock:
                        completed += 1
                        if node_errors == 0:
                            checkpoint_ids.add(node.commission_id)
                            with collect_checkpoint.open("a", encoding="utf-8") as stream:
                                stream.write(json.dumps({"commission_id": node.commission_id}) + "\n")
                        if on_progress is not None:
                            on_progress(node, completed, len(nodes), node_errors)

                with ThreadPoolExecutor(max_workers=workers) as executor:
                    pending = (node for node in nodes if node.commission_id not in checkpoint_ids)
                    _run_bounded(executor, pending, workers, fetch_node)
                completeness = check_completeness(nodes)
                completeness.update({"region": region, "limit": limit,
                                     "run_errors": len(failures),
                                     "session_refreshes": getattr(client, "_session_refresh_count", 0),
                                     "tree_errors": sum(item.get("stage") == "tree" for item in failures),
                                     "completed_in_run": len(checkpoint_ids),
                                     "complete": not failures and completeness["complete_uiks"] == len(nodes)})
                write_checkpoint(completeness_path, completeness)
                record({"event": "completeness", "complete_uiks": completeness["complete_uiks"],
                        "found_uiks": len(nodes), "complete": completeness["complete"],
                        "validation_issues": len(completeness["validation_issues"]),
                        "session_refreshes": completeness["session_refreshes"],
                        "path": str(completeness_path)})
                if completeness["tree_errors"]:
                    tree_checkpoint.unlink(missing_ok=True)
                if completeness["complete"]:
                    collect_checkpoint.unlink(missing_ok=True)
                    tree_checkpoint.unlink(missing_ok=True)
        except Exception as exc:
            error({"stage": "run", "error": str(exc)})
            raise RuntimeError(f"Сбор прерван; журнал: {log_path}") from exc
        finally:
            record({"event": "finish", "found_uiks": len(nodes),
                    "new_reports": counts["new"], "updated_reports": counts["updated"],
                    "unchanged_reports": counts["unchanged"], "errors": len(failures),
                    "http_429": http_errors[429],
                    "http_502": http_errors[502],
                    "http_503": http_errors[503],
                    "final_delay_seconds": pacing["delay"]})
            export_log_json(log_path)
    return nodes, counts, failures, str(log_path), str(completeness_path)
