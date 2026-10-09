"""Небольшие части больших CSV для просмотра в редакторе без потери строк."""

import csv
import io
import json
import shutil
from pathlib import Path
from uuid import uuid4

from .report_tables import PROCESSED


def _remove_generated(directory: Path, root: Path) -> None:
    target = directory.resolve()
    if target.parent != root.resolve():
        raise RuntimeError("Временная папка частей вне ожидаемого каталога")
    shutil.rmtree(target)


def split_csv(source: Path, view_dir: Path, max_bytes: int = 5_000_000) -> dict:
    if max_bytes < 1:
        raise ValueError("Размер части должен быть положительным")
    view_dir.mkdir(parents=True, exist_ok=True)
    destination = view_dir / source.stem
    stat = source.stat()
    signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "max_bytes": max_bytes}
    manifest_path = destination / "Список_частей.json"
    if manifest_path.is_file():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old.get("source_signature") == signature and all(
                (destination / part["file"]).is_file() and
                (destination / part["file"]).stat().st_size == part["bytes"] for part in old["parts"]):
            return old
    stage = view_dir / f".parts-{uuid4().hex}"
    stage.mkdir()
    output = None
    try:
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)

        def encode(row):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(row)
            return buffer.getvalue().encode("utf-8")

        parts, count = [], 0
        with source.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader)
            header_bytes = b"\xef\xbb\xbf" + encode(header)
            part_rows = part_size = 0

            def new_part():
                nonlocal output, part_rows, part_size
                if output is not None:
                    output.close()
                    parts[-1].update(rows=part_rows, bytes=part_size)
                name = f"Часть_{len(parts) + 1:03d}.csv"
                output = (stage / name).open("wb")
                output.write(header_bytes)
                parts.append({"file": name})
                part_rows, part_size = 0, len(header_bytes)

            new_part()
            for row in reader:
                if len(row) != len(header):
                    raise ValueError(f"Неравное количество полей в строке {count + 2} файла {source.name}")
                content = encode(row)
                if part_rows and part_size + len(content) > max_bytes:
                    new_part()
                output.write(content)
                part_size += len(content)
                part_rows += 1
                count += 1
            output.close()
            output = None
            parts[-1].update(rows=part_rows, bytes=part_size)
        current = source.stat()
        if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise RuntimeError("Исходный CSV изменился во время деления; повторите запуск")
        result = {"source": str(source.resolve()), "source_signature": signature,
                  "rows": count, "columns": len(header), "parts": parts}
        (stage / "Список_частей.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if destination.exists():
            _remove_generated(destination, view_dir)
        stage.rename(destination)
        return result
    finally:
        if output is not None:
            output.close()
        if stage.exists():
            _remove_generated(stage, view_dir)


def make_view_parts(processed: Path = PROCESSED, on_progress=print) -> list[dict]:
    results = []
    for prefix in ("00_", "01_", "02_"):
        paths = list(processed.glob(prefix + "*.csv"))
        if len(paths) != 1:
            raise ValueError(f"Ожидался один CSV {prefix} в {processed}; найдено {len(paths)}")
        on_progress(f"Подготовка частей: {paths[0].name}")
        result = split_csv(paths[0], processed / "Для_просмотра")
        results.append(result)
        on_progress(f"Готово: {len(result['parts'])} частей; {result['rows']} строк")
    return results
