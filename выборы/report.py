"""Создать разные таблицы анализа из уже обработанных данных: python report.py."""

from election_parser.report_tables import make_tables
from election_parser.csv_parts import make_view_parts
import argparse


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Таблицы анализа и небольшие части CSV для просмотра")
    parser.add_argument("--split-only", action="store_true", help="Создать только части готовых таблиц 00, 01, 02")
    options = parser.parse_args()
    try:
        if not options.split_only:
            tables = make_tables()
            print(f"Готово: {len(tables)} таблиц в data/processed. Описание: ОПИСАНИЕ_ТАБЛИЦ.md")
        make_view_parts()
    except Exception as exc:
        raise SystemExit(f"Ошибка формирования таблиц: {exc}") from exc
    print("Небольшие CSV для открытия: data/processed/Для_просмотра")
