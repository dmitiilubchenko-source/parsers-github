"""Читаемые таблицы анализа из уже обработанных CSV; без запросов к сайту."""

import csv
import json
from collections import defaultdict
from itertools import groupby
from pathlib import Path

from .config import BASE_DIR
from .csv_io import write_csv

PROCESSED = BASE_DIR / "data" / "processed"
SOURCES = PROCESSED / "служебные"


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def percent(numerator, denominator):
    return round(numerator * 100 / denominator, 4) if denominator else ""


def make_tables(source_dir: Path = SOURCES, output_dir: Path = PROCESSED, on_progress=print):
    required = ("candidate_votes_2026.csv", "party_votes_2026.csv", "party_uik_results_2026.csv",
                "candidate_totals_2026.csv", "uik_catalog_2026.json")
    for filename in required:
        if not (source_dir / filename).is_file():
            raise FileNotFoundError(f"Нет обработанного файла {source_dir / filename}; сначала выполните main.py process")
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = json.loads((source_dir / "uik_catalog_2026.json").read_text(encoding="utf-8"))
    expected_region = defaultdict(int)
    expected_district = defaultdict(int)
    seen_catalog = set()
    for item in catalog:
        if item["commission_id"] in seen_catalog:
            raise ValueError("Повтор ID УИК в каталоге")
        seen_catalog.add(item["commission_id"])
        path = item["path"]
        region = path[1] if len(path) > 1 else ""
        district = path[2] if len(path) > 2 else ""
        expected_region[region] += 1
        expected_district[region, district] += 1
    del catalog
    files = []
    candidate_district_counts = defaultdict(int)
    candidate_district_votes = defaultdict(int)
    national_parties = defaultdict(lambda: [0, 0])
    regional_parties = defaultdict(lambda: [0, 0])

    # Группы в источниках process идут подряд по ID УИК; проверяем это, чтобы не удвоить подсчёт.
    for kind, source, name in (
            ("candidate", "candidate_votes_2026.csv", "01_Кандидаты_голоса_на_каждом_УИК_2026.csv"),
            ("party", "party_votes_2026.csv", "02_Партии_голоса_на_каждом_УИК_2026.csv")):
        on_progress(f"Создание {name}")
        columns = ("Регион", "Округ", "Номер_УИК", "ID_УИК", "ID_кандидата", "Участник", "Голосов",
                   "Доля_действительных_голосов_на_УИК_%", "Максимум_голосов_на_УИК", "Источник")

        def rows():
            seen = set()
            for cid, group in groupby(read_rows(source_dir / source), key=lambda row: row["commission_id"]):
                if cid in seen:
                    raise ValueError(f"Повтор группы УИК {cid} в {source}")
                seen.add(cid)
                if cid not in seen_catalog:
                    raise ValueError(f"УИК {cid} отсутствует в каталоге")
                entries = list(group)
                identities = [(row.get("candidate_id") or row["candidate_name"]) if kind == "candidate"
                              else row["party_name"] for row in entries]
                if len(identities) != len(set(identities)):
                    raise ValueError(f"Повтор участника на УИК {cid}")
                votes = [int(row["votes"]) for row in entries]
                if any(value < 0 for value in votes):
                    raise ValueError(f"Отрицательные голоса на УИК {cid}")
                total, maximum = sum(votes), max(votes)
                if kind == "candidate":
                    candidate_district_counts[entries[0]["region"], entries[0]["electoral_district"]] += 1
                    candidate_district_votes[entries[0]["region"], entries[0]["electoral_district"]] += total
                for row, value in zip(entries, votes):
                    participant = row["candidate_name"] if kind == "candidate" else row["party_name"]
                    if kind == "party":
                        national_parties[participant][0] += value
                        national_parties[participant][1] += 1
                        regional_parties[row["region"], participant][0] += value
                        regional_parties[row["region"], participant][1] += 1
                    yield dict(zip(columns, (row["region"], row["electoral_district"], row["uik_number"], cid,
                        row.get("candidate_id", ""), participant, value, percent(value, total),
                        int(value == maximum), row["source_url"])))
        write_csv(output_dir / name, columns, rows())
        files.append(name)

    region_stats = {}
    name = "03_Голосование_и_бюллетени_на_каждом_УИК_2026.csv"
    on_progress(f"Создание {name}")
    columns = ("Регион", "Округ", "Номер_УИК", "ID_УИК", "Избирателей_в_списке",
               "Бюллетеней_в_ящиках", "Действительных_бюллетеней", "Недействительных_бюллетеней",
               "Бюллетеней_в_ящиках_от_числа_избирателей_%", "Источник")

    def turnout_rows():
        seen = set()
        for row in read_rows(source_dir / "party_uik_results_2026.csv"):
            cid = row["commission_id"]
            if cid in seen:
                raise ValueError(f"Повтор протокола УИК {cid}")
            seen.add(cid)
            if cid not in seen_catalog:
                raise ValueError(f"УИК {cid} отсутствует в каталоге")
            registered, cast, valid, invalid = (int(row[k]) for k in
                ("registered_voters", "ballots_cast", "valid_ballots", "invalid_ballots"))
            if cast != valid + invalid:
                raise ValueError(f"Баланс бюллетеней не сходится на УИК {cid}")
            stats = region_stats.setdefault(row["region"], [0, 0, 0, 0, 0])
            for index, value in enumerate((1, registered, cast, valid, invalid)):
                stats[index] += value
            yield dict(zip(columns, (row["region"], row["electoral_district"], row["uik_number"], cid,
                registered, cast, valid, invalid, percent(cast, registered), row["source_url"])))
    write_csv(output_dir / name, columns, turnout_rows())
    files.append(name)
    total_valid = sum(stats[3] for stats in region_stats.values())
    if sum(v[0] for v in national_parties.values()) != total_valid:
        raise ValueError("Сумма голосов партий не совпадает с действительными бюллетенями")
    name = "04_Партии_сумма_голосов_по_собранным_УИК_2026.csv"
    write_csv(output_dir / name, ("Партия", "Голосов", "Доля_собранных_действительных_голосов_%", "Учтено_УИК"),
        [{"Партия": party, "Голосов": values[0], "Доля_собранных_действительных_голосов_%": percent(values[0], total_valid),
          "Учтено_УИК": values[1]} for party, values in sorted(national_parties.items(), key=lambda x: -x[1][0])])
    files.append(name)
    name = "05_Партии_голоса_по_регионам_2026.csv"
    write_csv(output_dir / name, ("Регион", "Партия", "Голосов", "Доля_собранных_голосов_региона_%", "Учтено_УИК"),
        [{"Регион": region, "Партия": party, "Голосов": values[0],
          "Доля_собранных_голосов_региона_%": percent(values[0], region_stats[region][3]), "Учтено_УИК": values[1]}
         for (region, party), values in sorted(regional_parties.items(), key=lambda x: (x[0][0], -x[1][0]))])
    files.append(name)
    name = "06_Голосование_и_полнота_по_регионам_2026.csv"
    columns = ("Регион", "УИК_в_каталоге", "Учтено_партийных_протоколов", "Неучтённых_УИК",
               "Избирателей_в_списках_учтённых_УИК", "Бюллетеней_в_ящиках", "Действительных",
               "Недействительных", "Бюллетеней_от_числа_избирателей_%")
    write_csv(output_dir / name, columns, [dict(zip(columns, (region, expected, stats[0], expected-stats[0],
        stats[1], stats[2], stats[3], stats[4], percent(stats[2], stats[1]))))
        for region, expected in sorted(expected_region.items())
        for stats in [region_stats.get(region, [0, 0, 0, 0, 0])]])
    files.append(name)
    districts = defaultdict(list)
    for row in read_rows(source_dir / "candidate_totals_2026.csv"):
        districts[row["region"], row["electoral_district"]].append(row)
    name = "07_Лидеры_округов_для_анализа_мандатов_2026.csv"
    columns = ("Регион", "Округ", "Лидер_по_собранным_голосам", "ID_кандидата", "Голосов_лидера",
               "Доля_собранных_голосов_%", "Второй_кандидат", "Голосов_второго", "Отрыв",
               "УИК_в_каталоге", "Учтено_УИК", "Статус")
    leaders = []
    for key, entries in sorted(districts.items()):
        if sum(int(row["votes"]) for row in entries) != candidate_district_votes[key]:
            raise ValueError(f"Сводка кандидатов не совпадает с голосами УИК для {key}")
        entries.sort(key=lambda r: (-int(r["votes"]), r["candidate_name"]))
        top = entries[0]
        second = entries[1] if len(entries) > 1 else None
        count = candidate_district_counts[key]
        tie = second is not None and int(top["votes"]) == int(second["votes"])
        status = "Равенство голосов" if tie else "Предварительный лидер; официальный мандат не определён"
        if count != expected_district[key]:
            status += "; неполные данные округа"
        leaders.append(dict(zip(columns, (*key, top["candidate_name"], top["candidate_id"], int(top["votes"]),
            percent(int(top["votes"]), sum(int(r["votes"]) for r in entries)),
            second["candidate_name"] if second else "", int(second["votes"]) if second else "",
            int(top["votes"])-int(second["votes"]) if second else "", expected_district[key], count, status))))
    write_csv(output_dir / name, columns, leaders)
    files.append(name)
    note = ("# Таблицы анализа выборов\n\nЗапуск: `python report.py`. Команда использует уже обработанные CSV из `служебные`. "
        "Для открытия больших таблиц 00, 01 и 02 используйте части примерно по 5 МБ в папке `Для_просмотра`. "
        "Все части имеют одинаковые заголовки; строки распределены без удаления данных. "
        "Только обновление частей: `python report.py --split-only`.\n\n"
        + "\n".join(f"- {name}" for name in files)
        + "\n\nВсе кандидаты и партии сохранены, включая проигравших. Максимум голосов на УИК не означает победу в округе. "
        "Таблица 07 показывает лидеров по собранным голосам: официальное распределение мандатов здесь не вычисляется. "
        "Партийные проценты рассчитаны среди действительных голосов полученных протоколов, без распространения на пропущенные УИК. "
        "Бюллетени в ящиках не равны официальному показателю числа получивших бюллетени избирателей; название столбца указывает, что считается. "
        "Недостающие УИК показаны отдельно в таблице 06. Ноль учтённых голосов при отсутствии протоколов не означает ноль голосов в источнике.\n")
    (output_dir / "ОПИСАНИЕ_ТАБЛИЦ.md").write_text(note, encoding="utf-8")
    return files
