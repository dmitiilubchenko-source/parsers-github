"""Обход опубликованного дерева комиссий от ЦИК до УИК."""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import httpx

from .site_client import get_json
from .checkpoints import write_checkpoint

ELECTION_ID = "587813923"
ROOT_ID = "f0130cef-663a-4c57-b8ff-17054f8fa34e"


@dataclass(frozen=True)
class UikNode:
    commission_id: str
    number: int
    name: str
    path: tuple[str, ...]


def discover_tree(client: httpx.Client, *, region: str, limit: int = 6,
                  on_error: Callable[[dict], None] | None = None,
                  checkpoint_path: Path | None = None) -> list[UikNode]:
    if limit < 0:
        raise ValueError("Лимит УИК не может быть отрицательным")
    if checkpoint_path is not None and checkpoint_path.exists():
        state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if (str(state.get("region", "")).casefold() != region.casefold()
                or state.get("limit") != limit or state.get("version") != 1):
            raise ValueError(f"Контрольная точка дерева не подходит: {checkpoint_path}")
        if state.get("errors", 0):
            checkpoint_path.unlink()
            state = None
    else:
        state = None
    if state is not None:
        found = [UikNode(item["commission_id"], item["number"], item["name"], tuple(item["path"]))
                 for item in state["found"]]
        if state["complete"]:
            return found
        stack = [(item["node"], tuple(item["parents"])) for item in state["stack"]]
        visited = set(state["visited"])
        tree_errors = state.get("errors", 0)
    else:
        root = get_json(client, "/commissionClassifiers", {"electionsId": ELECTION_ID})
        if root.get("externalId") != ROOT_ID:
            raise ValueError("Получено неожиданное корневое дерево")
        roots = (root.get("children", []) if region.casefold() == "all" else
                 [child for child in root.get("children", []) if region.casefold() in child.get("name", "").casefold()])
        if not roots or (region.casefold() != "all" and len(roots) != 1):
            raise ValueError(f"Регион должен совпасть ровно с одной веткой, найдено: {len(roots)}")
        stack = [(child, (root["name"],)) for child in reversed(roots)]
        visited: set[str] = set()
        found: list[UikNode] = []
        tree_errors = 0

    def persist(complete: bool = False) -> None:
        if checkpoint_path is not None:
            write_checkpoint(checkpoint_path, {
                "version": 1, "region": region, "limit": limit, "complete": complete,
                "errors": tree_errors,
                "stack": [{"node": node, "parents": parents} for node, parents in stack],
                "visited": sorted(visited), "found": [asdict(node) for node in found],
            })

    persist()
    steps = 0
    last_saved = time.monotonic()
    while stack and (limit == 0 or len(found) < limit):
        node, parents = stack.pop()
        steps += 1
        node_id = node.get("externalId")
        if not node_id or node_id in visited:
            continue
        visited.add(node_id)
        node_path = (*parents, node["name"])
        if int(node.get("type", -1)) == 5:
            try:
                found.append(UikNode(node_id, int(node["number"]), node["name"], node_path))
            except (KeyError, TypeError, ValueError) as exc:
                if on_error is None:
                    raise
                tree_errors += 1
                on_error({"stage": "tree", "commission_id": node_id,
                          "path": list(node_path), "error": str(exc)})
        elif node.get("hasChildren"):
            try:
                expanded = get_json(client, "/commissionClassifiers", {"electionsId": ELECTION_ID, "classifierId": node_id})
                if expanded.get("externalId") != node_id:
                    raise ValueError(f"Неверный ответ для комиссии {node_id}")
            except Exception as exc:
                if on_error is None:
                    raise
                tree_errors += 1
                on_error({"stage": "tree", "commission_id": node_id,
                          "path": list(node_path), "error": str(exc)})
            else:
                for child in reversed(expanded.get("children", [])):
                    stack.append((child, node_path))
        if steps % 1000 == 0 or time.monotonic() - last_saved >= 15:
            persist()
            last_saved = time.monotonic()
    if not found:
        raise ValueError("В выбранной ветке не найдены УИК")
    persist(complete=True)
    return found
