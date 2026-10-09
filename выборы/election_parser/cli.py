"""Команды сбора и обработки сохранённых отчётов."""

import argparse
import json
import sys
from pathlib import Path

from election_parser.data_processing import OUTPUT as DATA_OUTPUT, ISSUES as DATA_ISSUES, process_saved_reports
from election_parser.result_processing import SUMMARY, CANDIDATES, DISTRICTS, CANDIDATE_TOTALS, ISSUES, process_results
from election_parser.party_processing import PARTY_UIKS, PARTY_VOTES, PARTY_TOTALS, PARTY_ISSUES, process_party_results
from election_parser.collector import CATALOG, collect_region

def show_collect_progress(node, completed: int, total: int, errors: int) -> None:
    """Печатает завершённый УИК и шкалу общего прогресса."""
    width = 30
    filled = width * completed // total
    bar = "#" * filled + "-" * (width - filled)
    print(f"УИК №{node.number} ({node.name}): получено {5 - errors}/5 ответов"
          f"; ошибок: {errors}", flush=True)
    print(f"[{bar}] {completed}/{total} УИК ({completed * 100 / total:.1f}%)", flush=True)


def show_collect_start(total: int, resumed: int) -> None:
    print(f"Найдено УИК: {total}; уже завершено в прерванном запуске: {resumed}", flush=True)


def show_missing_progress(node, completed: int, total: int, errors: int) -> None:
    width = 30
    filled = width * completed // total
    bar = "#" * filled + "-" * (width - filled)
    print(f"УИК №{node.number}: дозагрузка завершена; ошибок: {errors}", flush=True)
    print(f"[{bar}] {completed}/{total} недокачанных УИК ({completed * 100 / total:.1f}%)", flush=True)


def show_missing_start(total: int, resumed: int) -> None:
    print(f"Недокачанных УИК в последнем отчёте: {total}; уже восполнено: {resumed}", flush=True)


def show_export_progress(stage: str, completed: int, total: int, reused: int) -> None:
    label = {"reading": "Чтение исходных данных", "summary": "Подготовка общего отчёта",
             "writing": "Запись полного CSV"}[stage]
    print(f"{label}: {completed}/{total} УИК ({completed * 100 / total:.1f}%); "
          f"взято из кеша: {reused}", flush=True)


def show_processing_progress(completed: int, total: int) -> None:
    print(f"Прочитано файлов этапа: {completed}/{total} ({completed * 100 / total:.1f}%)", flush=True)


def main() -> None:
    if len(sys.argv) == 1 or sys.argv[1] not in {"collect", "collect-missing", "process", "export-all"}:
        parser = argparse.ArgumentParser(description="Сбор и обработка официальных отчётов УИК")
        parser.add_argument("command", choices=("collect", "collect-missing", "process", "export-all"))
        parser.parse_args()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "export-all":
        from election_parser.full_export import OUTPUT_DIR, export_all

        export_parser = argparse.ArgumentParser(description="Полный CSV всех полей сохранённых ответов УИК")
        export_parser.add_argument("--workers", type=int, default=4, help="Потоков чтения: 1–8; по умолчанию 4")
        export_parser.add_argument("--limit", type=int, default=0, help="0 — весь каталог; другое число — тестовый образец")
        export_parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="Каталог результата и кеша")
        options = export_parser.parse_args(sys.argv[2:])
        try:
            result = export_all(output_dir=options.output_dir, workers=options.workers,
                                limit=options.limit, on_progress=show_export_progress)
        except Exception as exc:
            export_parser.exit(1, f"Ошибка полного экспорта: {exc}\n")
        print(f"Полный CSV: {result['csv']}; строк: {result['rows']}; столбцов: {result['columns']}")
        print(f"Полнота: {result['complete_uiks']}/{result['catalog_uiks']} УИК; "
              f"замечаний: {result['source_validation_issues']}; время: {result['elapsed_seconds']:.1f} с")
        print(f"Общий отчёт: {options.output_dir / 'Общий_отчёт_полнота_2026.md'}")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "collect-missing":
        from election_parser.missing_collector import collect_missing

        missing_parser = argparse.ArgumentParser(description="Дозагрузить только отсутствующие ответы УИК")
        missing_parser.add_argument("--workers", type=int, default=2,
                                    help="Одновременных УИК: от 1 до 8; по умолчанию 2")
        missing_parser.add_argument("--delay", type=float, default=0.0,
                                    help="Пауза между запросами в секундах; по умолчанию 0")
        options = missing_parser.parse_args(sys.argv[2:])
        try:
            counts, failures, log_path, completeness_path = collect_missing(
                workers=options.workers, delay=options.delay,
                on_progress=show_missing_progress, on_start=show_missing_start)
        except Exception as exc:
            missing_parser.exit(1, f"Ошибка дозагрузки: {exc}\n")
        report = json.loads(Path(completeness_path).read_text(encoding="utf-8"))
        print(f"Новых ответов: {counts['new']}; обновлено: {counts['updated']}; "
              f"без изменений: {counts['unchanged']}; ошибок: {len(failures)}")
        print(f"Журнал: {log_path}; JSON-копия: {Path(log_path).with_suffix('.json')}")
        print(f"Полнота: {report['complete_uiks']}/{report['found_uiks']} УИК; "
              f"полный запуск: {'да' if report['complete'] else 'нет'}; отчёт: {completeness_path}")
        print(f"Замечаний при разборе ответов: {len(report['validation_issues'])}")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        collect_parser = argparse.ArgumentParser(description="Обойти дерево комиссий и скачать отчёты УИК")
        collect_parser.add_argument("--region", default="Республика Адыгея", help="Регион в дереве ЦИК или all для всей страны")
        collect_parser.add_argument("--limit", type=int, default=6, help="Максимум УИК; 0 означает все УИК выбранной территории")
        collect_parser.add_argument("--delay", type=float, default=0.0, help="Пауза между ответами в секундах; по умолчанию 0")
        collect_parser.add_argument("--workers", type=int, default=2, help="Одновременных УИК: от 1 до 8; по умолчанию 2")
        collect_parser.add_argument("--fresh", action="store_true", help="Начать заново, игнорируя незавершённый запуск")
        options = collect_parser.parse_args(sys.argv[2:])
        try:
            nodes, counts, failures, log_path, completeness_path = collect_region(
                options.region, options.limit, options.delay, options.workers,
                on_progress=show_collect_progress, on_start=show_collect_start,
                fresh=options.fresh,
            )
        except Exception as exc:
            collect_parser.exit(1, f"Ошибка обхода: {exc}\n")
        print(f"Найдено УИК: {len(nodes)}; новых ответов: {counts['new']}; "
              f"обновлено: {counts['updated']}; без изменений: {counts['unchanged']}; "
              f"ошибок: {len(failures)}")
        print(f"Каталог: {CATALOG}; журнал запуска: {log_path}; JSON-копия: {Path(log_path).with_suffix('.json')}")
        report = json.loads(Path(completeness_path).read_text(encoding="utf-8"))
        print(f"Полнота: {report['complete_uiks']}/{report['found_uiks']} УИК; "
              f"полный запуск: {'да' if report['complete'] else 'нет'}; отчёт: {completeness_path}")
        print(f"Замечаний при разборе ответов: {len(report['validation_issues'])}; "
              f"обновлений сессии: {report['session_refreshes']}")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "process":
        command = "process"
        argparse.ArgumentParser(description="Обработать сохранённые JSON и создать общий CSV").parse_args(sys.argv[2:])
        try:
            from election_parser.full_export import export_all

            print("Формирование общего CSV всех исходных полей...", flush=True)
            full_report = export_all(on_progress=show_export_progress)
            print(f"Общий CSV сохранён: {full_report['csv']}", flush=True)
            print("Обработка дневных отчётов...", flush=True)
            rows = process_saved_reports(on_progress=show_processing_progress)
            daily_count = len(rows)
            del rows
            daily_issues = json.loads(DATA_ISSUES.read_text(encoding="utf-8"))
            print(f"Дневных точек: {daily_count}. Обработка протоколов кандидатов...", flush=True)
            uiks, candidates, issues = process_results(on_progress=show_processing_progress)
            print(f"Протоколов кандидатов: {uiks}. Обработка партийных протоколов...", flush=True)
            party_uiks, parties, party_issues = process_party_results(on_progress=show_processing_progress)
            print(f"Обработано {daily_count} контрольных точек, {uiks} итоговых протоколов, "
                  f"{candidates} строк кандидатов, {party_uiks} партийных протоколов, "
                  f"{parties} строк партий; замечаний по дневным данным: {len(daily_issues)}; "
                  f"проблем итоговых протоколов: {len(issues) + len(party_issues)}")
            print(f"Файлы: {DATA_OUTPUT}; {SUMMARY}; {CANDIDATES}; {DISTRICTS}; "
                  f"{CANDIDATE_TOTALS}; {PARTY_UIKS}; {PARTY_VOTES}; {PARTY_TOTALS}; "
                  f"ошибки: {DATA_ISSUES}, {ISSUES}, {PARTY_ISSUES}")
            print(f"Общий CSV: {full_report['csv']}; строк: {full_report['rows']}; "
                  f"столбцов: {full_report['columns']}; "
                  f"полнота: {full_report['complete_uiks']}/{full_report['catalog_uiks']} УИК")
        except Exception as exc:
            raise SystemExit(f"Ошибка {command}: {exc}") from exc
        return
