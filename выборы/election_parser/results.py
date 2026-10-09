"""Проверка итоговых протоколов УИК по кандидатам и партиям из JSON ЦИК."""

import re

from .tree import ELECTION_ID, UikNode
from .site_client import SERVICE

REPORT_ID = 242
CANDIDATE_PROTOCOL = 1
PARTY_PROTOCOL = 2


def result_url(node: UikNode) -> str:
    return (f"http://www.izbirkom.ru/election/{ELECTION_ID}/commission/"
            f"{node.commission_id}/voting-flow?type=7&report={REPORT_ID}")


def result_api_url(commission_id: str, protocol_num: int = CANDIDATE_PROTOCOL) -> str:
    return f"{SERVICE}/reports/{REPORT_ID}?commissionClassifierId={commission_id}&protocolNum={protocol_num}"


def parse_result(report: dict, node: UikNode, protocol_num: int = CANDIDATE_PROTOCOL) -> dict:
    if protocol_num not in (CANDIDATE_PROTOCOL, PARTY_PROTOCOL):
        raise ValueError("Неизвестный номер протокола")
    if str(report.get("reportType")) != str(REPORT_ID):
        raise ValueError("Неожиданный тип итогового отчёта")
    body = report.get("body") or {}
    if body.get("commissionClassifierId") != node.commission_id or body.get("protocolNum") != protocol_num:
        raise ValueError("Протокол относится к другой комиссии или номеру")
    if not body.get("protocolId"):
        raise ValueError("У итогового протокола отсутствует ID")
    records = {}
    participant_ids = {}
    for item in body.get("records") or []:
        raw_number, raw_votes = str(item.get("infoPrintNum", "")), str(item.get("value", ""))
        if not re.fullmatch(r"\d+", raw_number) or not re.fullmatch(r"\d+", raw_votes):
            raise ValueError("Отчёт содержит нечисловую или скрытую строку")
        number = int(raw_number)
        if number in records:
            raise ValueError(f"Повтор строки протокола {number}")
        name = str(item.get("infoText") or "").strip()
        if not name:
            raise ValueError(f"Нет названия строки {number}")
        records[number] = {"name": name, "votes": int(raw_votes)}
        participant_ids[number] = str(item["id"]) if item.get("id") else None
    if len(records) < 13 or set(records) != set(range(1, len(records) + 1)):
        raise ValueError("Неполный итоговый протокол")
    protocol = {str(n): records[n] for n in range(1, 13)}
    participants = [{"row": n, "participant_id": participant_ids[n], **records[n]}
                    for n in range(13, len(records) + 1)]
    names = [item["name"] for item in participants]
    if len(names) != len(set(names)):
        raise ValueError("Повтор имени участника в одном протоколе")
    ids = [item["participant_id"] for item in participants if item["participant_id"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Повтор ID участника в одном протоколе")
    value = lambda n: records[n]["votes"]
    if sum(item["votes"] for item in participants) != value(10):
        raise ValueError("Голоса участников не равны действительным бюллетеням")
    if value(9) + value(10) != value(7) + value(8):
        raise ValueError("Бюллетени в урнах не сходятся")
    if value(2) + value(12) != value(3) + value(4) + value(5) + value(6) + value(11):
        raise ValueError("Баланс полученных и выданных бюллетеней не сходится")
    return {"commission_id": node.commission_id, "uik_number": node.number,
            "commission_path": list(node.path), "report_id": REPORT_ID,
            "protocol_num": protocol_num,
            "protocol_id": body.get("protocolId"), "created_at": report.get("createdAt"),
            "protocol": protocol,
            "candidates" if protocol_num == CANDIDATE_PROTOCOL else "parties": participants}
