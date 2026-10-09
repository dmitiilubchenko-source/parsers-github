"""CSV snapshots and comparisons; no database."""

import json
from pathlib import Path

import pandas as pd

HEADERS = {
    "run_id": "Идентификатор замера", "collected_at": "Дата парсинга",
    "run_mode": "Тип замера", "run_status": "Статус замера",
    "retailer": "Сеть", "city": "Город", "store_id": "Идентификатор магазина",
    "store_address": "Адрес магазина", "product_id": "Идентификатор товара",
    "name": "Название", "section": "Раздел", "category": "Категория",
    "category_rule": "Основание категории", "source_category": "Категория METRO",
    "source_category_url": "Ссылка категории", "regular_price": "Обычная цена",
    "price_unit": "Единица цены", "price_type": "Тип цены",
    "price_evidence": "Основание цены", "package": "Упаковка",
    "availability": "Наличие", "url": "Ссылка на товар",
    "price_text": "Исходный текст цены",
    "normalized_price": "Цена за кг/л", "normalized_unit": "Единица нормализованной цены",
    "package_quantity": "Масса/объём для пересчёта", "normalization_note": "Основание пересчёта",
}


def read_snapshot(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return frame.rename(columns={value: key for key, value in HEADERS.items()})


def compare(current: pd.DataFrame, previous: pd.DataFrame,
            complete_categories: set[str]) -> pd.DataFrame:
    """Only infer disappearance for a fully visited source category."""
    keys = ["store_id", "product_id"]
    for frame in (current, previous):
        if frame.duplicated(keys).any():
            raise ValueError("Повтор идентификатора товара в одном магазине")
    joined = previous.merge(current, on=keys, how="outer", suffixes=("_previous", ""),
                            indicator=True)
    rows = []
    for item in joined.to_dict("records"):
        side = item["_merge"]
        def value(field):
            return item.get(field + "_previous", "") if side == "left_only" else item.get(field, "")
        old = pd.to_numeric(item.get("regular_price_previous"), errors="coerce")
        new = pd.to_numeric(item.get("regular_price"), errors="coerce")
        same_unit = item.get("price_unit_previous") == item.get("price_unit")
        same_package = item.get("package_previous", "") == item.get("package", "")
        same_price_type = item.get("price_type_previous") == item.get("price_type")
        comparable = (side == "both" and pd.notna(old) and pd.notna(new) and old > 0
                      and same_unit and same_package and same_price_type)
        if side == "right_only":
            status = "Новый товар"
        elif side == "left_only":
            status = ("Не найден в каталоге" if value("source_category_url") in complete_categories
                      else "Не удалось проверить")
        elif value("availability") == "Нет в наличии":
            status = "Нет в наличии"
            comparable = False
        elif not same_unit or not same_package:
            status = "Изменилась упаковка или единица цены"
        elif not comparable:
            status = "Нет сопоставимой обычной цены"
        elif new > old:
            status = "Подорожал"
        elif new < old:
            status = "Подешевел"
        else:
            status = "Цена не изменилась"
        rows.append({
            "Идентификатор магазина": item["store_id"],
            "Идентификатор товара": item["product_id"], "Название": value("name"),
            "Категория": value("category"), "Статус": status,
            "Предыдущая дата": item.get("collected_at_previous", ""),
            "Текущая дата": item.get("collected_at", ""),
            "Предыдущая цена": old if pd.notna(old) else None,
            "Текущая цена": new if pd.notna(new) else None,
            "Изменение, руб": round(new - old, 2) if comparable else None,
            "Изменение, %": round((new / old - 1) * 100, 2) if comparable else None,
        })
    return pd.DataFrame(rows)


def save_run(output: Path, run_id: str, rows: list[dict], report: dict) -> Path:
    from normalization import normalize
    rows = [dict(row, **normalize(row)) for row in rows]
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / f"prices_{run_id}.csv"
    if snapshot.exists():
        raise FileExistsError(snapshot)
    frame = pd.DataFrame(rows, columns=list(HEADERS))
    frame["run_mode"] = {"probe": "Пробный", "full": "Полный", "basket": "Корзина 500", "recovery": "Повторный сбор"}.get(report["mode"], report["mode"])
    frame["run_status"] = {"complete": "Завершён", "partial": "Неполный", "failed": "Ошибка"}.get(report["status"], report["status"])
    previous_path = None
    if rows:
        current_store = rows[0]["store_id"]
        for candidate in sorted(output.glob("prices_*.csv"), reverse=True):
            metadata = candidate.with_suffix(".json")
            if not metadata.exists():
                continue
            prior_report = json.loads(metadata.read_text(encoding="utf-8"))
            if (prior_report.get("status") == "complete" and prior_report.get("mode") == report['mode']
                    and report['mode'] in {'full', 'basket'}
                    and prior_report.get('basket_id') == report.get('basket_id')
                    and prior_report.get("store_id") == current_store):
                previous_path = candidate
                break
        if previous_path and report["mode"] in {"full", "basket"}:
            changes = compare(frame, read_snapshot(previous_path), set(report["complete_categories"]))
            changes.to_csv(output / f"changes_{run_id}.csv", index=False, encoding="utf-8-sig")
    frame.rename(columns=HEADERS).to_csv(snapshot, index=False, encoding="utf-8-sig")
    report["previous_snapshot"] = str(previous_path) if previous_path else None
    snapshot.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot
