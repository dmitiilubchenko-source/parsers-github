"""Атомарное сохранение промежуточного состояния сбора."""

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import BASE_DIR

CHECKPOINT_DIR = BASE_DIR / "data" / "checkpoints"


def checkpoint_paths(region: str, limit: int) -> tuple[Path, Path]:
    key = sha256(f"{region.casefold()}\n{limit}".encode("utf-8")).hexdigest()[:16]
    return (CHECKPOINT_DIR / f"tree_{key}.json",
            CHECKPOINT_DIR / f"collect_{key}.jsonl")


def write_checkpoint(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
