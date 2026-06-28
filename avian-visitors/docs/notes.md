# AvianVisitors Desktop — журнал разработки

Проект: адаптация AvianVisitors (BirdNET-Pi) для работы на обычном ноутбуке.
Репозиторий-источник: https://github.com/Twarner491/AvianVisitors (ветка avian-visitors)

---

## Архитектура решения

| Компонент | Технология | Назначение |
|-----------|-----------|------------|
| Аудиозахват | Python + sounddevice + numpy | Захват с микрофона, 3-сек сегменты |
| Анализ | Python + birdnet (pip-пакет) | Распознавание видов птиц |
| Хранение | SQLite (stdlib) | Таблица detections |
| API | Python + FastAPI + uvicorn | JSON-эндпоинты + статика |
| Фронтенд | HTML/JS/CSS из репо (мин. правки) | Коллаж, Stats, Atlas, модалка |

Координаты по умолчанию: **Москва — 55.75°N, 37.62°E**.

## Структура проекта

```
avian-visitors/
├── main.py              # Точка входа (Часть 6)
├── database.py          # SQLite обёртка (Часть 1)
├── audio_capture.py     # Аудиозахват + BirdNET (Часть 2)
├── api.py               # FastAPI роуты (Части 3-4)
├── requirements.txt     # Зависимости (Часть 7)
├── README.md            # Документация (Часть 7)
├── frontend/            # Скопировано из avian/frontend/ (Часть 5)
│   ├── index.html
│   ├── apt.js           (адаптированный)
│   ├── styles.css
│   ├── dims.json
│   └── masks.json
├── assets/              # Скопировано из avian/assets/ (Часть 1)
│   ├── illustrations/   # 498 PNG — kachō-e стилизация
│   ├── cutouts/         # 158 PNG — фото без фона
│   └── favicon.png
└── data/                # Runtime
    └── birds.db         # SQLite, создаётся автоматически
```

## План разбивки на части

1. Скелет проекта + SQLite + database.py
2. Аудиозахват + BirdNET анализ
3. FastAPI: JSON-эндпоинты
4. Статика + эндпоинт картинок + wiki-прокси
5. Адаптация фронтенда (JS-правки)
6. main.py — единая точка входа
7. requirements.txt + README.md

---

## Часть 1 — Скелет проекта + SQLite + database.py

**Статус:** ✅ готово

### Что сделано

#### 1.1 Клонирование исходников
```bash
git clone --branch avian-visitors --depth 1 \
  https://github.com/Twarner491/AvianVisitors.git
```

#### 1.2 Структура директорий
Создана:
- `avian-visitors/frontend/` — сюда пойдёт адаптированный фронтенд (Часть 5)
- `avian-visitors/assets/illustrations/` — 498 kachō-e иллюстраций PNG
- `avian-visitors/assets/cutouts/` — 158 фото-катаутов PNG
- `avian-visitors/assets/favicon.png`
- `avian-visitors/data/` — runtime, здесь будет birds.db
- `avian-visitors/scripts/` — скрипты (заглушка для будущих утилит)
- `avian-visitors/docs/` — документация

Ассеты скопированы из `AvianVisitors/avian/assets/` целиком.

#### 1.3 database.py

Файл: `database.py` — класс `Database`.

**Схема таблицы `detections`:**

| Поле       | Тип    | Описание                                   |
|-----------|--------|-------------------------------------------|
| id        | INTEGER | PK, AUTOINCREMENT                         |
| Sci_Name  | TEXT   | Научное название (латынь)                 |
| Com_Name  | TEXT   | Обычное название (английский)             |
| Confidence| REAL   | 0..1, уверенность BirdNET                  |
| Date      | TEXT   | "YYYY-MM-DD", локальная дата              |
| Time      | TEXT   | "HH:MM:SS", локальное время               |
| File_Name | TEXT   | Пустая строка (записи не сохраняются)      |

Индексы: `idx_det_date`, `idx_det_sci`, `idx_det_sci_date`.

**Методы (маппинг 1:1 на PHP API):**

| Метод            | PHP action  | Возвращает                                                   |
|-----------------|-------------|-------------------------------------------------------------|
| `stats()`       | stats       | `{totals, today, last_hour, week, started, as_of}`          |
| `lifelist()`    | lifelist    | `{species: [{sci, com, first_seen, last_seen, n, best_conf}]}` |
| `recent(hours)` | recent      | `{hours, species: [{sci, com, n, best_conf, last_seen, top_file, top_at}]}` |
| `species_detail(sci)` | species | `{sci, summary: {com, total, first_seen, last_seen, best_conf}, detections: [{d, t, file, conf}]}` |
| `timeseries(days)` | timeseries | `{days, daily: [{date, detections, species}], by_hour: [{hour, detections}], as_of}` |
| `firstseen(limit)` | firstseen | `{species: [{sci, com, first_seen, total}], as_of}`        |
| `insert_detection(sci, com, conf, dt)` | — | Записывает детекцию                                       |

**Ключевые решения:**
- `row_factory = sqlite3.Row` — строки ведут себя как словари
- `PRAGMA journal_mode=WAL` — параллельное чтение (API) и запись (аудио-поток)
- `PRAGMA busy_timeout=2000` — не падать при конкуренции
- SQL-запросы перенесены из PHP дословно, включая форматы дат и агрегации
- `File_Name` всегда пустая строка — в десктоп-версии аудио не сохраняется

### Результат тестирования

Все 7 методов проверены:
- `stats()` → пустая статистика при пустой БД, корректные счётчики после вставок
- `lifelist()` → пустой список → список с видами и first_seen/last_seen
- `recent(hours)` → агрегация по видам, top_file/top_at корректно пустые
- `species_detail(sci)` → summary + detections[]
- `timeseries(days)` → daily + by_hour агрегации
- `firstseen(limit)` → DESC-сортировка по первой детекции
- `insert_detection()` → корректная запись с автодатой

WAL-режим и busy_timeout позволяют параллельное чтение/запись — критично
для Части 2 (письмо из аудио-потока) и Части 3 (чтение из API).

---