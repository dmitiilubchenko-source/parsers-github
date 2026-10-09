"""Создаёт читаемую JSON-копию построчного журнала."""

import json
import os
from pathlib import Path
from uuid import uuid4


def export_log_json(source: Path) -> Path:
    """Атомарно сохраняет события JSONL как массив JSON рядом с оригиналом."""
    target = source.with_suffix(".json")
    temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
    try:
        with source.open("r", encoding="utf-8") as input_file, temporary.open("w", encoding="utf-8") as output:
            output.write("[\n")
            first = True
            for line in input_file:
                if not line.strip():
                    continue
                event = json.loads(line)
                if not first:
                    output.write(",\n")
                output.write("  " + json.dumps(event, ensure_ascii=False))
                first = False
            output.write("\n]\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
