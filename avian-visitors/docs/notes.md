# AvianVisitors Desktop — журнал разработки

Проект: адаптация AvianVisitors (BirdNET-Pi) для работы на обычном ноутбуке.
Репозиторий-источник: https://github.com/Twarner491/AvianVisitors (ветка avian-visitors)

---

## Архитектура решения

| Компонент | Технология | Назначение |
|-----------|-----------|------------|
| Аудиозахват | Python + sounddevice + numpy | Захват с микрофона, 3-сек сегменты |
| Анализ | Python + birdnet-analyzer (оригинал, Kai Hilbert/Cornell) | Распознавание видов птиц |
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

## Часть 2 — Аудиозахват + BirdNET анализ

**Статус:** ✅ готово (переписано на birdnet-analyzer, 25/25 тестов)

### Важное замечание о выборе пакета

Изначально использовался `birdnet` v0.2.16 с PyPI — сторонний пакет-обёртка,
не имеющий отношения к оригинальному BirdNET от Kai Hilbert (KIT) / Cornell Lab
of Ornithology. Он давал некорректные результаты (например, Rifleman из
Новой Зеландии на аудио из Москвы). После тестирования трёх реальных записей
пакет был заменён на оригинальный `birdnet-analyzer`, который показал
правильные результаты.

**Запрещено:** `pip install birdnet` (v0.2.16, сторонний, некорректный).
**Используется:** `pip install birdnet-analyzer` (оригинал от Kai Hilbert / Cornell).

### 2.1 birdnet-analyzer: программный API

Пакет `birdnet-analyzer` не предоставляет публичного Python API для встраивания —
основной интерфейс — CLI `birdnet-analyze`. Для интеграции в проект мы используем
внутренние модули напрямую:

| Модуль | Что даёт |
|--------|----------|
| `birdnet_analyzer.config` | Глобальные настройки (пути к модели, лейблам, SR, SIG_LENGTH) |
| `birdnet_analyzer.model` | `predict(sample)` — TFLite inference, `flat_sigmoid()` — логиты → вероятности, `load_model()` — ленивая загрузка TFLite |
| `birdnet_analyzer.audio` | `open_audio_file()`, `split_signal()`, `pad()` — работа с аудио через librosa |
| `birdnet_analyzer.species.utils` | `get_species_list(lat, lon, week, threshold)` — geo-фильтрация через metadata-модель |
| `birdnet_analyzer.analyze.utils` | `load_codes()`, `predict()`, `iterate_audio_chunks()` — внутренняя логика CLI |
| `birdnet_analyzer.utils` | `read_lines()` — чтение файлов лейблов |

**Пайплайн inference (как это работает внутри birdnet-analyzer):**

1. Аудио → `librosa.load(sr=48000, mono=True)` → ресэмплинг до 48 kHz
2. `split_signal(sig, rate, 3.0, 0.0, 1.0)` → 3-секундные чанки (с паддингом)
3. `model.predict(sample)` → логиты, shape `(batch, 6522)`
4. `flat_sigmoid(logits, sensitivity=-1, bias=1.0)` → вероятности 0..1
5. Фильтр: geo-species list + min_confidence
6. Формат лейблов: `"Genus species_Common Name"` (6522 записи)

**Geo-фильтрация** — критически важна для корректных результатов:
- `get_species_list(lat, lon, week, sf_thresh)` использует metadata-модель
  BirdNET, которая предсказывает вероятность встретить каждый вид
  в данной точке в данную неделю года.
- Москва (55.75, 37.62), лето (week 26): ~366 видов из 6522
- Для Москвы в неделю 26: House Sparrow, Great Tit, Thrush Nightingale —
  все в списке (и потому корректно определяются).

### 2.2 Файл audio_capture.py (итоговая версия)

**Класс `AudioListener`** — реалтайм-захват с микрофона:

| Параметр | Значение | Описание |
|---------|--------|-----------|
| `SAMPLE_RATE` | 48 000 | BirdNET v2.4 ожидает 48 kHz |
| `SEGMENT_DURATION` | 3.0 сек | Стандартная длина сегмента |
| `CHANNELS` | 1 (моно) | BirdNET v2.4 |
| `CONFIDENCE_THRESHOLD` | 0.25 | Порог для записи |
| `LATITUDE` | 55.75 | Москва |
| `LONGITUDE` | 37.62 | Москва |
| `week` | -1 (auto) | Автоопределение текущей недели |
| `sensitivity` | 1.0 | Sigmoid sensitivity |
| `sf_thresh` | 0.03 | Species filter threshold |
| `device` | None (default) | sounddevice автоматически выбирает |

**Как работает capture loop:**

1. `sounddevice.InputStream(blocksize=144000)` — каждый `read()` даёт ровно 3 секунды
2. Цикл: `read()` → моно-канал → `_process_segment()` → повторить
3. `_ensure_model()` — ленивая загрузка (один раз, thread-safe):
   - Устанавливает `cfg.MODEL_PATH`, `cfg.LABELS_FILE`, `cfg.SAMPLE_RATE` и т.д.
   - Загружает лейблы (6522 записи) через `utils.read_lines()`
   - Вызывает `get_species_list()` для geo-фильтрации
4. `_process_segment()`:
   - Паддит до 144000 сэмплов если короче
   - `model.predict(sample)` → логиты
   - `flat_sigmoid(logits, sensitivity=-1, bias=cfg.SIGMOID_SENSITIVITY)` → вероятности
   - Для каждого вида с confidence > threshold и в geo-species list:
     - Разделяет `"Genus species_Common Name"` → `sci_name` / `com_name`
     - `db.insert_detection(sci_name, com_name, confidence)`

**Функция `analyze_file()`** — для анализа загруженных файлов (не реалтайм):
- Принимает путь к аудио (WAV/MP3/OGG/M4A/FLAC)
- Загружает через `birdnet_analyzer.audio.open_audio_file()` (librosa)
- Разбивает на 3-секундные чанки, прогоняет через модель
- Возвращает `{segments_analyzed, detections_written, top_detections}`
- Пишет все детекции в БД

**Graceful shutdown:**
- `stop()` → `threading.Event` → цикл выходит после текущего сегмента
- `thread.join(timeout=10)` → `stream.stop()` + `stream.close()`
- Поток daemon=True — не блокирует выход из приложения

### 2.3 Результат тестирования (25/25 ✅)

```
cd /home/z/my-project/avian-visitors && python -m pytest test_audio_capture.py -v -s
```

| Тест | Результат |
|------|-----------|
| `_parse_label` — 6 случаев | ✅ |
| `_current_week` — тип и диапазон | ✅ |
| Конструктор `AudioListener` — дефолты, кастом, stats, stop | ✅ |
| `_ensure_model` — загрузка, кэш, без geo | ✅ Labels=6522, Species list=366 |
| **Реальные записи (ключевые тесты)** | |
| House Sparrow (Wikipedia) | ✅ **0.829** confidence |
| Great Tit (birdybird.m4a) | ✅ **0.981** confidence |
| Thrush Nightingale (birdybird2.m4a) | ✅ **0.708** confidence |
| Edge cases — несуществующий файл, тишина, короткий сегмент | ✅ |
| Geo-фильтрация — Москва, разные недели | ✅ |
| Порог confidence (0.5 < 0.01) | ✅ |
| Интеграция с БД — записи попадают в detections | ✅ |

---