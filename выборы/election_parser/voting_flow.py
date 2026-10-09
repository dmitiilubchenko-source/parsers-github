"""Разбор официального JSON отчёта 453 о ходе голосования."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class Checkpoint:
    election_date: date
    commission_id: str
    uik_number: int
    time: str
    voters_count: int
    voters_percent: Decimal
    source_url: str


def parse_report(payload: dict, source_url: str) -> list[Checkpoint]:
    """Возвращает только строки УИК; некорректные значения вызывают ошибку."""
    report = payload.get("data", payload)
    if str(report.get("reportType")) != "453":
        raise ValueError("Ожидался отчёт 453")
    query = parse_qs(urlparse(source_url).query)
    try:
        election_date = date.fromisoformat(query["date"][0])
        rows = report["body"]["commissionClassifiers"]
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("В отчёте отсутствует дата или список комиссий") from exc
    result: list[Checkpoint] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if str(row.get("commissionClassifierType")) != "5":
            continue
        commission_id = str(row["commissionClassifierId"])
        number = row["number"]
        count = row["votersCount"]
        percent = Decimal(str(row["votersPercent"]))
        time = str(row["time"])
        if not isinstance(number, int) or number <= 0:
            raise ValueError("Некорректный номер УИК")
        if not isinstance(count, int) or count < 0:
            raise ValueError("Некорректное число избирателей")
        if not Decimal(0) <= percent <= Decimal(100):
            raise ValueError("Процент вне диапазона 0–100")
        if len(time) != 8 or time[2] != ":" or time[5] != ":":
            raise ValueError("Некорректное время отчёта")
        key = (commission_id, time)
        if key in seen:
            raise ValueError("Дублирующийся УИК и время")
        seen.add(key)
        result.append(Checkpoint(election_date, commission_id, number, time, count, percent, source_url))
    if not result:
        raise ValueError("В отчёте нет строк УИК")
    return sorted(result, key=lambda x: (x.uik_number, x.election_date, x.time))


def validate_progress(checkpoints: list[Checkpoint]) -> None:
    """Проверяет, что накопленное число не убывает на одном УИК."""
    by_uik: dict[str, list[Checkpoint]] = {}
    for item in checkpoints:
        by_uik.setdefault(item.commission_id, []).append(item)
    for items in by_uik.values():
        ordered = sorted(items, key=lambda x: (x.election_date, x.time))
        if any(current.voters_count < previous.voters_count for previous, current in zip(ordered, ordered[1:])):
            raise ValueError(f"Число избирателей убывает на УИК {ordered[0].uik_number}")
