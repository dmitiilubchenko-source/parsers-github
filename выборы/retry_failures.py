"""Повторить неудачные отчёты из журнала: python retry_failures.py ПУТЬ_К_ЖУРНАЛУ."""

import argparse
from pathlib import Path

from election_parser.collector import LOG_DIR
from election_parser.retry import retry_reports


def main() -> None:
    parser = argparse.ArgumentParser(description="Повторно получить только отчёты УИК с ошибками")
    parser.add_argument("log", nargs="?", type=Path,
                        help="Журнал collect_*.jsonl; по умолчанию последний журнал сбора или дозагрузки")
    args = parser.parse_args()
    if args.log is None:
        logs = sorted(LOG_DIR.glob("collect_*.jsonl"), key=lambda path: (path.stat().st_mtime_ns, path.name))
        if not logs:
            parser.error("Нет журнала основного сбора в data/logs")
        source = logs[-1]
    else:
        source = args.log
    saved, errors, output = retry_reports(source)
    print(f"Сохранено ответов: {saved}; ошибок: {errors}; новый журнал: {output}; "
          f"JSON-копия: {Path(output).with_suffix('.json')}")


if __name__ == "__main__":
    main()
