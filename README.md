# Как запускать парсеры

Нужны **Python 3.11+**, интернет и **Google Chrome** для выборов и METRO. Команды ниже — для PowerShell на Windows.

## 1. Установка

Скачайте репозиторий и распакуйте архив. Откройте PowerShell **в папке нужного проекта**: `аэропорты`, `выборы` или `цены в магазине`.

В каждой выбранной папке выполните один раз:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. Аэропорты

Для полного сбора поместите исходные файлы `airports.csv`, `runways.csv`, `airport-frequencies.csv`, `countries.csv`, `regions.csv` из оригинального проекта в папку `аэропорты/data/`.

```powershell
# Собрать данные по всем ICAO из входного справочника
.\.venv\Scripts\python.exe scripts/html_airports_scraper.py
# Подготовить итоговую таблицу с часовыми поясами и ВПП
.\.venv\Scripts\python.exe assignment.py
```

Результаты: `outputs/html/` и `outputs/assignment/`. Охват ограничен объектами с корректным ICAO во входном справочнике.

Для небольшой демонстрации вместо полных исходных файлов:

```powershell
Copy-Item examples/input/*.csv data/
.\.venv\Scripts\python.exe assignment.py
```

Демонстрация обработает только **20 аэропортов**. Не выполняйте копирование примера поверх полных входных данных.

## 3. Выборы

```powershell
# Полный сбор по стране
.\.venv\Scripts\python.exe main.py collect --region all --limit 0 --workers 2
# Обработка и создание таблиц
.\.venv\Scripts\python.exe main.py process
.\.venv\Scripts\python.exe report.py
# Отдельная таблица партийных протоколов, строки 01–22
.\.venv\Scripts\python.exe assignment.py
```

Результаты: `data/processed/` и `data/assignment/`. Для пробного сбора замените первую команду на `main.py collect --region "Республика Адыгея" --limit 6 --workers 2` с тем же Python из `.venv`.

## 4. Цены METRO

```powershell
# Корзина 500 товаров
.\.venv\Scripts\python.exe main.py
# Или полный обнаруженный веб-каталог
.\.venv\Scripts\python.exe main.py --full
# Или фиксированная продуктовая корзина для недельного сравнения
.\.venv\Scripts\python.exe main.py --inflation --collect
```

Выберите один нужный режим. При первом запуске выберите магазин в открывшемся Chrome и следуйте подсказкам терминала. Для сравнения цен используйте один и тот же магазин; недельную проверку запускайте вручную раз в неделю. Работайте без VPN.

Результаты: `output/`; продуктовая корзина — `output/inflation/`.

## Готовые примеры

Небольшие результаты уже лежат в `examples/` каждого проекта, краткие выводы — в [МИНИ_ВЫВОДЫ.md](МИНИ_ВЫВОДЫ.md). Полные выгрузки и профили браузера не включены. `.gitignore` исключает рабочие данные новых запусков из Git.
