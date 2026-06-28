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

## Часть 2 — Аудиозахват + BirdNET анализ

**Статус:** ✅ готово (переписано + протестировано)

### Исправленные баги из первоначальной версии

Предыдущая сессия написала audio_capture.py с тремя критическими багами,
обнаруженными при тестировании:

1. **`_process_segment` никогда не вызывался** — условие
   `np.sin(2π * ...)[-1] > 0` всегда было false (sin(2π) = 0).
2. **Модель загружалась заново на каждый сегмент** — `_load_model()` вызывался
   внутри `_process_segment()` без кэширования.
3. **Неверный API birdnet** — версия `"v2.4"` (нужно `"2.4"`), атрибуты
   `model.model_sr` не существуют (нужно `model.get_sample_rate()`),
   `AcousticPredictionSession(model)` не принимает модель напрямую.

### 2.1 Реальное API birdnet v0.2.16

| Вызов | Результат |
|-------|---------|
| `load('acoustic', '2.4', 'tf')` | Загрузка модели (кэшируется) |
| `model.get_sample_rate()` | 48000 Гц |
| `model.get_segment_size_s()` | 3.0 сек |
| `model.get_segment_size_samples()` | 144000 |
| `model.n_species` | 6522 вида |
| `model.species_list` | OrderedSet[str] — формат `"Genus species_Common Name"` |
| `model.predict_arrays((audio, sr), top_k=None, default_confidence_threshold=0.0)` | → `AcousticDataPredictionResult` |
| `result.species_probs` | shape `(n_inputs, n_segments, n_species)` — берём `[0, 0]` |
| `AcousticPredictionSession(model)` | Не работает напрямую (AssertionError) |

**Выбор архитектуры:** `model.predict_arrays()` напрямую для каждого сегмента.
Сессии (`predict_session`) требуют сложной инициализации и не нужны
для поточного по-сегментного вызова.

**Важно:** `species_list` содержит `"Latin_Common"` формат через подчёркивание.
Код разделяет: `raw.index("_")` → `sci_name` и `com_name`.

### 2.2 Файл audio_capture.py (итоговая версия)

Класс `AudioListener`:

| Параметр | Значение | Описание |
|---------|--------|-----------|
| `SAMPLE_RATE` | 48 000 | BirdNET v2.4 ожидает 48 kHz |
| `SEGMENT_DURATION` | 3.0 сек | Стандартная длина сегмента |
| `CHANNELS` | 1 (моно) | BirdNET v2.4 |
| `CONFIDENCE_THRESHOLD` | 0.25 | Порог для записи |
| `LATITUDE` | 55.75 | Москва |
| `LONGITUDE` | 37.62 | Москва |
| `device` | None (default) | sounddevice автоматически выбирает |

**Как работает capture loop:**

1. `sounddevice.InputStream(blocksize=144000)` — каждый `read()` даёт ровно 3 секунды
2. Цикл: `read()` → моно-канал → `_process_segment()` → повторить
3. `_process_segment()`:
   - `_ensure_model()` — ленивая загрузка модели (один раз, thread-safe)
   - `model.predict_arrays((audio, sr), top_k=None, default_confidence_threshold=0.0)`
   - `result.species_probs[0, 0]` — вероятности всех 6522 видов
   - Для каждого вида с confidence > 0.25:
     - Разделяет `"Genus species_Common Name"` → `sci_name` / `com_name`
     - `db.insert_detection(sci_name, com_name, confidence)`
   - Логирует каждую детекцию

**Performance:** inference ~2 сек/сегмент на CPU (TensorFlow).
Тишина — 0 детекций, белый шум — 0-1 детекций > 0.25.

**Graceful shutdown:**
- `stop()` → `threading.Event` → цикл выходит после текущего сегмента
- `thread.join(timeout=10)` → `stream.stop()` + `stream.close()`
- Поток daemon=True — не блокирует выход из приложения

### Результат тестирования (23/23 ✅)

```
python3 test_audio_capture.py
```

| Тест | Проверок | Результат |
|------|---------|-----------|
| 1. Загрузка модели | 5 | ✅ SR=48000, 3.0s, 144000 samples, 6522 species |
| 2. Inference на тишине | 3 | ✅ shape (1,1,N), тишина: max_conf=0.007 < 0.1 |
| 3. Inference на шуме | — | ✅ max_conf=0.505, 1 вид > 0.25, 10 видов > 0.01 |
| 4. _process_segment → DB | 10 | ✅ Тишина: 0 записей, шум: 1 запись, sci/com разделены |
| 5. Жизненный цикл | 4 | ✅ start/stop/stats API |

---