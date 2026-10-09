"""Ссылки на официальные дневные отчёты кампании 2026 года."""

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class ElectionLink:
    election_id: str
    commission_id: str
    report_id: int


def parse_link(url: str) -> ElectionLink:
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname not in {"izbirkom.ru", "www.izbirkom.ru"} or len(parts) < 4:
        raise ValueError("Нужна ссылка на страницу комиссии портала izbirkom.ru")
    if parts[0] != "election" or parts[2] != "commission":
        raise ValueError("В ссылке не найдены идентификаторы выборов и комиссии")
    report_id = int(parse_qs(parsed.query).get("report", [0])[0])
    return ElectionLink(parts[1], parts[3], report_id)


def daily_urls(link: ElectionLink) -> dict[str, str]:
    """Отчёты хода голосования для трёх дат кампании 2026 года."""
    if link.election_id != "587813923":
        raise ValueError("Даты и типы отчётов проверены только для выборов 587813923")
    base = f"http://www.izbirkom.ru/election/{link.election_id}/commission/{link.commission_id}/voting-flow"
    return {
        "2026-09-18": f"{base}?type=2&report=453",
        "2026-09-19": f"{base}?type=4&report=453",
        "2026-09-20": f"{base}?type=6&report=453",
    }

