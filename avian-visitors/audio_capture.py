"""
audio_capture.py — Аудиозахват + BirdNET анализ.

Непрерывный захват с микрофона → 3-секундные сегменты →
BirdNET v2.4 анализ → SQLite через database.py.

Использует:
  - sounddevice — захват аудио с микрофона
  - numpy — буферизация
  - birdnet (pip-пакет v0.2.16) — акустическая модель + опционально geo-фильтрация
  - database.Database — запись результатов

Координаты по умолчанию: Москва 55.75°N, 37.62°E.
Порог confidence: 0.25.
"""

import logging
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
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

# Device selection ("" = default, "cpu" = CPU only, "gpu" = GPU)
DEVICE = ""


class AudioListener:
    """
    Захват звука с микрофона и анализ через BirdNET.

    Работает в фоновом потоке. Запуск через start(), остановка через stop()
    или контекст-менеджер (with-блок).
    """

    def __init__(
        self,
        db: Database,
        sample_rate: int = SAMPLE_RATE,
        segment_duration: float = SEGMENT_DURATION,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        latitude: float = LATITUDE,
        longitude: float = LONGITUDE,
        device: str = DEVICE,
    ):
        self.db = db
        self.sample_rate = sample_rate
        self.segment_duration = segment_duration
        self.segment_samples = int(sample_rate * segment_duration)
        self.confidence_threshold = confidence_threshold
        self.latitude = latitude
        self.longitude = longitude
        self.device = device

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Счётчики для логирования
        self._segments_processed = 0
        self._detections_written = 0
        self._start_time: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Запускает захват и анализ в фоновом потоке."""
        if self._running:
            logger.warning("AudioListener уже запущен")
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
            "Audio capture started: %d Hz, %d-ch segments, "
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
    # Internal: capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self):
        """
        Основной цикл захвата.

        Использует кольцевой буфер для непрерывного аудиопотока.
        Каждые 3 секунды буфер отправляется в BirdNET.
        """
        logger.info("Audio capture loop started (device: %s)", self.device)

        try:
            # Создаём поток audio для ввода
            stream = sounddevice.InputStream(
                samplerate=self.sample_rate,
                channels=CHANNELS,
                dtype="float32",
                device=self.device,
                blocksize=int(self.sample_rate * self.segment_duration),
            )
            logger.info(
                "Audio device opened: %s @ %d Hz",
                stream.device.name,
                stream.samplerate,
            )

            # Кольцевой буфер: 3 секунды * sample_rate
            buffer = np.zeros(self.segment_samples, dtype=np.float32)

            # Читаем аудио в буфер
            while self._running:
                try:
                    chunk, overflow = stream.read(self.segment_samples)
                except sounddevice.InputOverflowError:
                    # Переполнение — записываем только новую часть
                    pass
                except OSError as e:
                    if self._running:
                        logger.error("Audio read error: %s", e)
                    break
                else:
                    if chunk is None:
                        logger.debug("No audio data (stream ended)")
                        time.sleep(0.1)
                        continue

                    # Добавляем новые сэмплы в буфер
                    n_new = len(chunk)
                    if n_new > 0:
                        # Сдвигаем старые данные
                        buffer = np.roll(buffer, -n_new)
                        buffer[-n_new:] = chunk

                    # Когда набрали достаточно данных для сегмента
                    if len(chunk) == 0:
                        time.sleep(0.01)
                        continue

                    # Проверяем, есть ли полный сегмент
                    if np.abs(np.sin(2 * np.pi * np.arange(len(buffer)) / self.segment_samples))[-1] > 0:
                        self._process_segment(buffer.copy())
                        # Обнуляем буфер (данные уже обработаны)
                        buffer[:] = 0.0

                except Exception as e:
                    logger.error("Error in capture loop: %s", e)
                    time.sleep(1)

        except Exception as e:
            logger.error("Fatal error in audio capture: %s", e)
        finally:
            try:
                stream.stop()
                logger.info("Audio stream closed")
            except Exception:
                pass
            self._running = False
            logger.info("Audio capture loop ended")

    # ------------------------------------------------------------------
    # Internal: BirdNET inference
    # ------------------------------------------------------------------

    def _load_model(self):
        """
        Загружает акустическую модель BirdNET v2.4.
        При первом запуске модель скачивается автоматически (~90 MB).
        """
        from birdnet import load as birdnet_load, AcousticPredictionSession

        logger.info("Loading BirdNET acoustic model (v2.4, backend=tf)...")
        model = birdnet_load("acoustic", "v2.4", "tf")
        logger.info(
            "Model loaded. SR=%d Hz, segment=%.1fs, %d species",
            model.model_sr,
            model.segment_duration_s,
            model.n_species,
        )

        # Создаём сессию для предсказаний
        session = AcousticPredictionSession(model)
        logger.info("AcousticPredictionSession ready")
        return model, session

    def _process_segment(self, audio: np.ndarray):
        """
        Прогоняет один 3-секундный аудиосегмент через BirdNET
        и записывает детекции в БД.

        Args:
            audio: numpy массив shape (segment_samples,) с аудио.
        """
        try:
            model, session = self._load_model()
        except Exception as e:
            logger.error("Failed to load BirdNET model: %s", e)
            return

        self._segments_processed += 1

        # Предсказание
        try:
            # run_arrays принимает список кортежей (audio_array, sample_rate)
            result = session.run_arrays((audio, self.sample_rate))
        except Exception as e:
            logger.error("BirdNET prediction failed: %s", e)
            return

        # Разбираем результаты
        try:
            probs = result.species_probs  # shape: (n_species,)
            species_list = result.species_list  # OrderedSet[str]

            # Для каждого вида с confidence > threshold — записываем
            for i, conf in enumerate(probs):
                if conf > self.confidence_threshold:
                    sci_name = species_list[i]
                    com_name = sci_name  # birdnet даёт латинские имена

                    # Пропускаем "Latin Name" в "Common Name" для записи в БД.
                    # birdnet v2.4 species_list содержит "Latin Name" (binomial).
                    # Для совместимости с фронтендом (он ждёт Com_Name)
                    # форматируем как "Genus species".
                    parts = sci_name.split(" ", 1)
                    com_name = sci_name if len(parts) < 2 else f"{parts[0]} {parts[1]}"

                    self.db.insert_detection(
                        sci_name=sci_name,
                        com_name=com_name,
                        confidence=float(conf),
                    )
                    self._detections_written += 1
                    logger.info(
                        "%.2f  %s",
                        float(conf),
                        com_name,
                    )
        except Exception as e:
            logger.error("Error processing results: %s", e)


def _slugify(sci_name: str) -> str:
    """Переводит научное имя в slug для файла: 'Columba livia' → 'columba-livia'."""
    return sci_name.lower().replace(" ", "-")


# ── Тестирование ─────────────────────────────────────────────────────── #

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=== AudioListener quick test ===")
    print("Creating database...")
    db = Database()
    db.init()
    print("Starting audio capture for 5 seconds...")

    listener = AudioListener(db, confidence_threshold=0.01)  # низкий порог для теста
    listener.start()
    time.sleep(5)
    listener.stop()

    print(f"\nStats: {listener.stats}")
    print(f"Database stats: {db.stats()}")
    db.close()
    print("Done.")