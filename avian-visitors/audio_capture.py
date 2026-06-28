"""
audio_capture.py — Аудиозахват + BirdNET анализ.

Непрерывный захват с микрофона → 3-секундные сегменты →
BirdNET v2.4 анализ → SQLite через database.py.

Использует:
  - sounddevice — захват аудио с микрофона
  - numpy — буферизация
  - birdnet (pip-пакет v0.2.16) — акустическая модель
  - database.Database — запись результатов

Координаты по умолчанию: Москва 55.75°N, 37.62°E.
Порог confidence: 0.25.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

import numpy as np
import sounddevice

from database import Database

logger = logging.getLogger("avian.audio")

# ─── Константы ────────────────────────────────────────────────────────

# BirdNET v2.4 модель ожидает 48 kHz
SAMPLE_RATE = 48_000
# Длина сегмента для анализа (секунды)
SEGMENT_DURATION = 3.0
# Количество сэмплов в сегменте
SEGMENT_SAMPLES = int(SAMPLE_RATE * SEGMENT_DURATION)
# Модель BirdNET v2.4 ожидает моно
CHANNELS = 1
# Порог confidence для записи детекции
CONFIDENCE_THRESHOLD = 0.25
# Координаты: Москва
LATITUDE = 55.75
LONGITUDE = 37.62


class AudioListener:
    """
    Захват звука с микрофона и анализ через BirdNET.

    Работает в фоновом потоке. Запуск через start(), остановка через stop().

    Usage::

        db = Database()
        db.init()
        listener = AudioListener(db)
        listener.start()
        # ... работает в фоне ...
        listener.stop()
        db.close()
    """

    def __init__(
        self,
        db: Database,
        sample_rate: int = SAMPLE_RATE,
        segment_duration: float = SEGMENT_DURATION,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        latitude: float = LATITUDE,
        longitude: float = LONGITUDE,
        device=None,
    ):
        self.db = db
        self.sample_rate = sample_rate
        self.segment_duration = segment_duration
        self.segment_samples = int(sample_rate * segment_duration)
        self.confidence_threshold = confidence_threshold
        self.latitude = latitude
        self.longitude = longitude
        self.device = device  # None = default mic

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Ленивая загрузка модели (загружается один раз)
        self._model = None
        self._model_lock = threading.Lock()

        # Счётчики для диагностики
        self._segments_processed = 0
        self._detections_written = 0
        self._start_time: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Запускает захват и анализ в фоновом потоке."""
        if self._running:
            logger.warning("AudioListener already running")
            return

        self._running = True
        self._stop_event.clear()
        self._start_time = time.time()
        self._segments_processed = 0
        self._detections_written = 0

        self._thread = threading.Thread(
            target=self._capture_loop,
            name="audio-capture",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Audio capture started: %d Hz, %.1fs segments, "
            "confidence > %.2f, lat=%.2f lon=%.2f",
            self.sample_rate,
            self.segment_duration,
            self.confidence_threshold,
            self.latitude,
            self.longitude,
        )

    def stop(self):
        """Останавливает захват (graceful, ждёт завершения текущего сегмента)."""
        if not self._running:
            return
        logger.info("Stopping audio capture...")
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._running = False
        elapsed = time.time() - self._start_time if self._start_time else 0
        logger.info(
            "Audio capture stopped. Processed %d segments, "
            "wrote %d detections in %.1fs",
            self._segments_processed,
            self._detections_written,
            elapsed,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict:
        return {
            "segments_processed": self._segments_processed,
            "detections_written": self._detections_written,
            "uptime_seconds": (
                time.time() - self._start_time
                if self._start_time else 0
            ),
        }

    # ------------------------------------------------------------------
    # Internal: lazy model loading (thread-safe, once)
    # ------------------------------------------------------------------

    def _ensure_model(self):
        """
        Загружает BirdNET v2.4 акустическую модель один раз.
        При первом запуске модель скачивается (~90 MB) и кэшируется.
        """
        if self._model is not None:
            return

        with self._model_lock:
            if self._model is not None:
                return

            from birdnet import load as birdnet_load

            logger.info("Loading BirdNET acoustic model (2.4, backend=tf)...")
            self._model = birdnet_load("acoustic", "2.4", "tf")
            logger.info(
                "Model loaded. SR=%d Hz, segment=%.1fs, %d species",
                self._model.get_sample_rate(),
                self._model.get_segment_size_s(),
                self._model.n_species,
            )

    # ------------------------------------------------------------------
    # Internal: capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self):
        """
        Основной цикл захвата.

        sounddevice.InputStream с blocksize = 3 сек — каждый read()
        возвращает ровно один сегмент для BirdNET.
        """
        stream = None
        try:
            stream = sounddevice.InputStream(
                samplerate=self.sample_rate,
                channels=CHANNELS,
                dtype="float32",
                device=self.device,
                blocksize=self.segment_samples,
            )
            dev_info = stream.device[0] if stream.device else None
            dev_name = dev_info["name"] if dev_info else "default"
            logger.info(
                "Audio device opened: %s @ %d Hz, blocksize=%d",
                dev_name,
                stream.samplerate,
                self.segment_samples,
            )

            while self._running:
                try:
                    data, overflow = stream.read(self.segment_samples)
                except sounddevice.InputOverflowError:
                    logger.warning("Audio input overflow, skipping segment")
                    continue
                except OSError as e:
                    if self._running:
                        logger.error("Audio read error: %s", e)
                    break

                # data shape: (segment_samples, 1) → берём моно-канал
                segment = data[:, 0] if data.ndim > 1 else data

                self._process_segment(segment)

                if self._stop_event.is_set():
                    break

        except Exception as e:
            logger.error("Fatal error in audio capture: %s", e, exc_info=True)
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                    logger.info("Audio stream closed")
                except Exception:
                    pass
            self._running = False
            logger.info("Audio capture loop ended")

    # ------------------------------------------------------------------
    # Internal: BirdNET inference
    # ------------------------------------------------------------------

    def _process_segment(self, audio: np.ndarray):
        """
        Прогоняет один 3-секундный аудиосегмент через BirdNET
        и записывает детекции в БД.

        Args:
            audio: numpy массив shape (segment_samples,) float32.
        """
        try:
            self._ensure_model()
        except Exception as e:
            logger.error("Failed to load BirdNET model: %s", e)
            return

        self._segments_processed += 1

        # Предсказание через BirdNET
        # top_k=None → все 6522 вида; threshold=0.0 → без отсечения на уровне модели
        try:
            result = self._model.predict_arrays(
                (audio, self.sample_rate),
                top_k=None,
                default_confidence_threshold=0.0,
            )
        except Exception as e:
            logger.error("BirdNET prediction failed: %s", e)
            return

        # Результат: species_probs shape (n_inputs, n_segments, n_species)
        # Для одного сегмента: probs = result.species_probs[0, 0]
        try:
            probs = result.species_probs[0, 0]  # shape (6522,)
            species_list = result.species_list    # OrderedSet[str], len=6522

            now = datetime.now()
            detections_this_segment = 0

            for i, conf in enumerate(probs):
                if conf > self.confidence_threshold:
                    raw = str(species_list[i])
                    # species_list формат: "Genus species_Common Name"
                    # Разделяем на sci_name и com_name
                    if "_" in raw:
                        # Берём первую часть до первого подчёркивания,
                        # но binomial имеет пробел, а не подчёркивание:
                        # "Psittacara strenuus_Pacific Parakeet"
                        underscore_idx = raw.index("_")
                        sci_name = raw[:underscore_idx]
                        com_name = raw[underscore_idx + 1:]
                    else:
                        sci_name = raw
                        com_name = raw

                    self.db.insert_detection(
                        sci_name=sci_name,
                        com_name=com_name,
                        confidence=float(conf),
                        dt=now,
                    )
                    self._detections_written += 1
                    detections_this_segment += 1
                    logger.info(
                        "  [%.3f] %s",
                        float(conf),
                        sci_name,
                    )

            if detections_this_segment == 0:
                logger.debug(
                    "Segment #%d: no detections above %.2f",
                    self._segments_processed,
                    self.confidence_threshold,
                )
            else:
                logger.info(
                    "Segment #%d: %d detection(s)",
                    self._segments_processed,
                    detections_this_segment,
                )
        except Exception as e:
            logger.error("Error processing BirdNET results: %s", e)


# ── Standalone test ────────────────────────────────────────────────── #

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=== AudioListener standalone test ===")
    print("Will capture from default microphone for 10 seconds.")
    print("Threshold lowered to 0.01 to see any output.\n")

    db = Database()
    db.init()

    listener = AudioListener(db, confidence_threshold=0.01)
    listener.start()

    print("Listening... (10 seconds)")
    for i in range(10):
        time.sleep(1)
        print(f"  {i+1}s  stats: {listener.stats}")

    listener.stop()
    print(f"\nFinal stats: {listener.stats}")
    print(f"Database stats: {db.stats()}")
    db.close()
    print("Done.")