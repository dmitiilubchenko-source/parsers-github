"""Атомарная запись CSV и проверка уникальности строк."""

import csv
import os
from pathlib import Path
from typing import Iterable
from uuid import uuid4


def write_csv(path: Path, columns: tuple[str, ...], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def require_unique(rows: Iterable[dict], fields: tuple[str, ...]) -> None:
    seen = set()
    for row in rows:
        key = tuple(row[field] for field in fields)
        if key in seen:
            raise ValueError(f"Повтор строк в выходных данных по ключу {fields}")
        seen.add(key)


