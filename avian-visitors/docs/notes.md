# AvianVisitors Desktop — журнал разработки

## Что это и зачем

Цель: десктопное приложение для автоматической идентификации птиц по звуку.
Микрофон ноутбука непрерывно слушает окружение, BirdNET анализирует 3-секундные
сегменты, детекции сохраняются в SQLite и отображаются на веб-дашборде в браузере.

Источник вдохновения: AvianVisitors / BirdNET-Pi
(ветка avian-visitors) — проект для Raspberry Pi с PHP-бэкендом.

### Отличие от оригинального AvianVisitors

| | Оригинал (BirdNET-Pi) | Наш проект (Desktop) |
|---|---|---|
| Платформа | Raspberry Pi (ARM) | Обычный ноутбук (x86_64) |
| Бэкенд | PHP (birdnet-api.php) | Python (FastAPI) |
| БД | SQLite через PHP | SQLite через Python (stdlib) |
| Анализ | birdnet-analyze (CLI subprocess) | birdnet-analyzer (Python API напрямую) |
| Фронтенд | HTML/JS/CSS из репо | Тот же фронтенд, минимальные JS-правки |
| Аудиозахват | arecord (ALSA) | sounddevice (PortAudio) |
| Сохранение аудио | Да, WAV-файлы на диск | Нет, только детекции в БД |
| Запуск | systemd-сервис | Один `python main.py` |
| Зависимости | системные пакеты RPi | pip install + venv |

Ключевое упрощение: оригинал написан для RPi и завязан на его экосистему
(ALSA, systemd, PHP, системные пакеты). Мы переписываем бэкенд на Python,
сохраняя фронтенд и SQL-схему 1:1, чтобы запускать на любом ноутбуке.

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

## Развёртывание в новом чате

**Проблема:** при потере контекста чата (или запуске в новой сессии) проект нужно быстро поднять из репозитория и подготовить окружение. Ниже — полный чеклист.

### 1. Git-репозиторий

```bash
cd /home/z/my-project
git clone git@github.com:<user>/avian-visitors.git
cd avian-visitors
```

Локальный путь: `/home/z/my-project/avian-visitors/`
Пакет (Python модули): `/home/z/my-project/avian-visitors/avian-visitors/`
Тесты: `/home/z/my-project/avian-visitors/avian-visitors/test_*.py`
Документация: `/home/z/my-project/avian-visitors/avian-visitors/docs/notes.md`

### 2. Python-окружение

Проект использует venv с Python 3.12. Все зависимости уже установлены в
`/home/z/.venv/`. Если venv отсутствует:

```bash
python3.12 -m venv /home/z/.venv
source /home/z/.venv/bin/activate
pip install -r requirements.txt   # после Части 7
# Или вручную:
pip install fastapi uvicorn sounddevice numpy librosa birdnet-analyzer pytest
```

**Критически важно:** `birdnet-analyzer` (оригинал от Kai Hilbert / Cornell),
НЕ `birdnet` (сторонний пакет, даёт неверные результаты).

### 3. BirdNET модель (~262 MB)

Пакет `birdnet-analyzer` из PyPI содержит только Python-код. Модель (TFLite +
лейблы + metadata) НЕ входит в pip-пакет и скачивается отдельно.

**Одноразовая установка модели:**

```python
python -c "from birdnet_analyzer.utils import ensure_model_exists; ensure_model_exists()"
# Output: "Model found!" или "Downloading..."
```

Эта команда скачивает checkpoints в `birdnet_analyzer/checkpoints/V2.4/`.
Файлы:
- `BirdNET_V2.4_Model.tflite` — основная модель (~236 MB)
- `Labels.txt` — 6522 записи вида `"Genus species_Common Name"`
- `eBird_Taxonomy_v2021.csv`
- `Species_List.csv` (нужен для мета-модели?) и другие вспомогательные

**Куда ложатся файлы (автоматически):**
```
/home/z/.venv/lib/python3.12/site-packages/birdnet_analyzer/checkpoints/V2.4/
```

**Проверка:**
```bash
python -c "from birdnet_analyzer.utils import ensure_model_exists; ensure_model_exists(); print('OK')"
```

**Альтернатива (копирование из другого окружения):**
Если модель уже скачана в другом Python-окружении (например, Python 3.13 в
`/home/z/.local/lib/python3.13/`), можно скопировать:

```bash
cp -r /home/z/.local/lib/python3.13/site-packages/birdnet_analyzer/checkpoints \
      /home/z/.venv/lib/python3.12/site-packages/birdnet_analyzer/checkpoints
```

Но `ensure_model_exists()` предпочтительнее — она скачает актуальную версию
прямо из источника.

### 4. Тестовые аудиофайлы (реальные записи птиц)

Тесты `test_audio_capture.py` используют 2 реальные записи с пением птиц.
Они хранятся в:

```
/home/z/my-project/avian-visitors/test_audio/
├── birdybird.m4a    (~60 KB, 6.6 сек — Большая синица, confidence 0.981)
├── birdybird.wav    (конвертированный, 618 KB, 48kHz mono s16)
├── birdybird2.m4a   (~87 KB, 9.8 сек — Славка-черногрудка, confidence 0.708)
└── birdybird2.wav   (конвертированный, 914 KB, 48kHz mono s16)
```

Тесты ищут m4a-файлы по симлинкам:
```
/home/z/my-project/upload/birdybird.m4a  →  ../avian-visitors/test_audio/birdybird.m4a
/home/z/my-project/upload/birdybird2.m4a →  ../avian-visitors/test_audio/birdybird2.m4a
```

**Исходные файлы на Яндекс Диске (для повторного скачивания):**
- birdybird.m4a: https://disk.yandex.ru/d/DQNjtRviDerDNw
- birdybird2.m4a: https://disk.yandex.ru/d/sU4SIQJj_nomRg

**Как скачать с Яндекс Диска (headless):**
1. Открыть ссылку в `agent-browser`
2. `snapshot -i` → найти кнопку «Скачать» (ref)
3. `network route "https://downloader.disk.yandex.ru/*"` — перехват запроса
4. `click @<ref>` → кликнуть «Скачать»
5. `network requests --filter downloader` — получить прямой URL
6. `curl -L -o filename.m4a "<прямой_URL>"`

**Конвертация m4a → wav (если нужны):**
```bash
ffmpeg -i birdybird.m4a -ar 48000 -ac 1 -sample_fmt s16 birdybird.wav -y
```

**Ожидаемые результаты BirdNET:**

| Файл | Вид | Confidence |
|------|-----|------------|
| birdybird.m4a | Parus major (Great Tit / Большая синица) | 0.981 |
| birdybird2.m4a | Luscinia luscinia (Thrush Nightingale / Славка-черногрудка) | 0.708 |

### 5. Быстрая проверка после развёртывания

```bash
cd /home/z/my-project/avian-visitors
source /home/z/.venv/bin/activate

# 1. Модель на месте?
python -c "from birdnet_analyzer.utils import ensure_model_exists; ensure_model_exists()"

# 2. Тестовые аудио на месте?
ls -la /home/z/my-project/upload/birdybird.m4a /home/z/my-project/upload/birdybird2.m4a

# 3. Все тесты проходят?
python -m pytest avian-visitors/test_audio_capture.py avian-visitors/test_api.py -v
# Ожидание: 61 passed, 0 failed, 0 skipped
```

### 6. ENV-переменные

| Переменная | Default | Описание |
|-----------|---------|----------|
| `AVIAN_NO_AUDIO` | unset | `=1` отключает AudioListener (тесты, CI) |
| `AVIAN_LAT` | 55.75 | Широта для geo-фильтрации |
| `AVIAN_LON` | 37.62 | Долгота для geo-фильтрации |
| `AVIAN_CONFIDENCE` | 0.25 | Порог уверенности BirdNET |

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

## Часть 3 — FastAPI: JSON-эндпоинты

**Статус:** ✅ готово (36/36 тестов)

### Что сделано

#### 3.1 api.py — FastAPI приложение

Файл: `api.py` — FastAPI app с 8 эндпоинтами + lifespan.

**Эндпоинты:**

| Эндпоинт | Метод | Параметры | Описание |
|----------|-------|-----------|----------|
| `/api/health` | GET | — | Базовый health check |
| `/api/listener/status` | GET | — | Диагностика AudioListener (сегменты, uptime) |
| `/api/stats` | GET | — | Общая статистика |
| `/api/recent` | GET | `hours` (1..1000000, default 24) | Виды за последние N часов |
| `/api/lifelist` | GET | — | Все виды, когда-либо обнаруженные |
| `/api/species` | GET | `sci` (required) | Детали по одному виду (404 если нет) |
| `/api/timeseries` | GET | `days` (1..90, default 30) | Дневные + часовые агрегации |
| `/api/firstseen` | GET | `limit` (1..50, default 10) | Новейшие добавления к lifelist |
| `/api/upload` | POST | `file`, `confidence?`, `latitude?`, `longitude?` | Загрузка аудио → BirdNET анализ |

**Архитектурные решения:**

1. **Модульные синглтоны:** `db` (Database) и `listener` (AudioListener | None) создаются на уровне модуля. Все эндпоинты используют один и тот же объект Database.

2. **Lifespan context manager:** При старте приложения lifespan инициализирует БД (`db.init()`) и стартует AudioListener. При остановке — останавливает listener и закрывает БД. Это гарантирует корректную инициализацию при использовании uvicorn.

3. **ENV-переменные для настройки:**
   - `AVIAN_NO_AUDIO=1` — отключает AudioListener (для тестов и CI)
   - `AVIAN_LAT`, `AVIAN_LON` — координаты (default: Москва 55.75, 37.62)
   - `AVIAN_CONFIDENCE` — порог уверенности (default: 0.25)

4. **Upload endpoint:**
   - Валидация расширения файла (wav, mp3, ogg, m4a, flac)
   - Параметры через Form fields (confidence, latitude, longitude) с fallback на env vars
   - Файл сохраняется во временный файл, анализируется через `analyze_file()`, затем удаляется
   - Стриминг загрузки чанками по 1 МБ (не загружает весь файл в память)
   - При ошибке анализа — 500 с деталями

5. **Валидация параметров:** FastAPI Query с `ge`/`le` для числовых параметров (hours, days, limit, confidence). Неправильные значения → 422 Unprocessable Entity.

#### 3.2 Исправление бага в database.py

Обнаружен баг: `species_detail()` для несуществующего вида возвращал `summary` не как None, а как `{'com': None, 'total': 0, ...}`. Причина: SQL `SELECT ... COUNT(*) ... WHERE Sci_Name = ?` без GROUP BY возвращает одну строку с `total=0` даже при пустой таблице.

**Фикс:** Добавлены `GROUP BY Sci_Name HAVING COUNT(*) > 0` — теперь при отсутствии записей `fetchone()` возвращает `None`, и эндпоинт корректно отвечает 404.

#### 3.3 Исправление thread-safety в database.py

Добавлен `check_same_thread=False` в `sqlite3.connect()`. Это необходимо потому что:
- FastAPI (uvicorn) обрабатывает запросы в пуле потоков
- AudioListener пишет в БД из отдельного потока
- Без этого флага SQLite выбрасывает `ProgrammingError` при доступе из другого потока

Безопасность обеспечивается WAL-режимом + `busy_timeout=2000` (из Части 1).

#### 3.4 test_api.py — 36 тестов

| Категория | Тесты | Что проверяют |
|-----------|-------|---------------|
| TestHealth (3) | health_ok, listener_disabled, listener_running | Health check + статус слушателя |
| TestStats (2) | empty_db, populated_db | Пустая и заполненная статистика |
| TestRecent (5) | empty, default_hours, custom, fields, clamping | Параметры + структура ответа |
| TestLifelist (3) | empty, populated, fields | Lifelist + сортировка |
| TestSpecies (4) | missing_param, not_found, found, order | 422/404/200 + порядок детекций |
| TestTimeseries (3) | empty, populated, clamping | days validation + структура |
| TestFirstseen (4) | empty, populated, limit, fields | DESC сортировка + limit |
| TestUpload (6) | no_file, bad_ext, success, params, error, cleanup | Валидация + мок-анализ + cleanup |
| TestResponseFormat (6) | stats, recent, lifelist, species, timeseries, firstseen | Полная совместимость с PHP API форматом |

**Особенности тестов:**
- `os.environ["AVIAN_NO_AUDIO"] = "1"` — отключает AudioListener в lifespan
- `sys.modules["sounddevice"] = MagicMock()` — мокает sounddevice (нет реального микрофона)
- `analyze_file` мокируется через `patch("api.analyze_file")` — не загружает TFLite модель
- Реальная in-memory SQLite для каждого теста — полная изоляция
- populated_client fixture вставляет 4 детекции 3 видов за 2 дня

### Результат тестирования

```
cd /home/z/my-project/avian-visitors/avian-visitors && python -m pytest test_api.py -v
======================== 36 passed in 1.53s =========================
```

Все 36 тестов проходят. Время выполнения — 1.53 секунды.

---

## Часть 4 — Статика + эндпоинт картинок + wiki-прокси

**Статус:** ✅ готово (47/47 тестов, из них 11 новых)

### Что сделано

#### 4.1 Статические файлы и favicon

- `StaticFiles(directory="frontend/", html=True)` смонтирован на `/`
- Файлы `index.html`, `apt.js`, `styles.css`, `dims.json`, `masks.json`
  отдаются из `frontend/` (будет скопировано в Части 5)
- `/favicon.png` — отдельный route, отдает `assets/favicon.png`
- `html=True` — запрос `/` отдаёт `index.html`

**Важно:** StaticFiles монтируется **после** всех `/api/*` routes, иначе
статика перехватила бы API-запросы.

#### 4.2 `/api/cutout` — резолвер картинок птиц

Порт оригинального `cutout.php`, но БЕЗ динамического Wikipedia+rembg фоллбека
(шаги 3-4 из PHP). Десктоп-версия использует только локальные ассеты.

**Цепочка поиска (lookup chain):**

| Приоритет | Путь | Описание |
|-----------|------|----------|
| 1 | `assets/illustrations/<slug>-<pose>.png` | kachō-e, pose-specific (pose 2+ = flight) |
| 2 | `assets/illustrations/<slug>.png` | kachō-e, perched (pose 1, default) |
| 3 | `assets/cutouts/<slug>.png` | background-removed photo (fallback) |
| 4 | 404 | ничего не найдено |

**Slug-конверсия:** `"Parus major"` → `"parus-major"` (нижний регистр,
пробелы → дефисы).

**Валидация `sci`:** regex `^[A-Za-z]{2,40}(?: [a-z]{2,40}){1,3}$` —
отклоняет path-traversal (`../etc/passwd`), SQL-инъекции, пустые строки.

**Ассеты:** 498 illustrations (из них ~250 с pose-2) + 158 cutouts.

**Cache-Control:** `public, max-age=86400` (24ч, как в оригинале).

#### 4.3 `/api/wiki` — Wikipedia summary proxy

Порт оригинального `wiki.php`. Проксирует запрос к:
```
https://en.wikipedia.org/api/rest_v1/page/summary/<sci>
```

**Возвращает:**
```json
{
  "extract": "The great tit (Parus major) is a passerine bird...",
  "thumbnail": {"source": "https://upload.wikimedia.org/..."},
  "title": "Parus major"
}
```

**SSRF-защита:** thumbnail URL проверяется regex — только хосты
`*.wikimedia.org` и `*.wikipedia.org`. Если Wikipedia вернёт poisoned URL
на другой домен, thumbnail будет `null`.

**Грейсфул деградация:** если Wikipedia недоступен (timeout, network error) —
возвращает `{extract: null, thumbnail: null, title: null}` с 200 OK.
Тесты проверяют это.

**User-Agent:** настраивается через `AV_USER_AGENT` env var
(по умолчанию `AvianVisitors/1.0`).

**Cache-Control:** `public, max-age=86400` (24ч).

#### 4.4 Новые зависимости

- `httpx` — async HTTP client для Wikipedia proxy (уже был установлен).

#### 4.5 test_api.py — 11 новых тестов

| Класс | Тесты | Что проверяют |
|-------|-------|---------------|
| TestCutout (6) | illustration_found, cutout_fallback, pose_fallback, not_found, invalid_sci, sci_to_slug_helper | PNG 200, цепочка lookup, pose-2 → pose-1 fallback, 404, 400 на невалидный sci, slug-конверсия |
| TestWiki (5) | parus_major, invalid_sci, missing_species, cache_header, ssrf_protection | Реальный Wikipedia ответ 200, 400 на invalid, nulls на несуществующий вид, Cache-Control header, regex SSRF |

### Результат тестирования

```
cd /home/z/my-project/avian-visitors && python -m pytest avian-visitors/test_api.py -v
======================== 47 passed in 3.09s =========================
```

Всего 47 тестов (36 из Части 3 + 11 новых). Все проходят за 3.09 сек.

---