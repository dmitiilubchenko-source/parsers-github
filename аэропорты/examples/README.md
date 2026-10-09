# Примеры: аэропорты
`airports_sample.csv/json` — первые 20 записей из готовой выгрузки.
`summary_full_run.json` и `html_summary_full_run.json` — статистика полного сохранённого запуска, а не этой выборки.
`input/` — исходные CSV для этих же 20 аэропортов и справочники стран/регионов.

Для локальной демонстрации из папки «аэропорты»:
```powershell
Copy-Item examples/input/*.csv data/
python assignment.py
```
Это создаст результат только по демонстрационной выборке.
Для полного запуска скопируйте исходные CSV из оригинальной папки «аэропорты/data» в `data/`; `airline_routes.json` нужен для дополнения маршрутов в `sort_airports.py`.
Для фильтрации `sort_airports.py` нужны результаты HTML-сбора в `outputs/html/`.
