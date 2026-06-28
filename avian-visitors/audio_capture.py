"""
audio_capture.py — Audio capture + BirdNET analysis.

Continuous capture from microphone → 3-second segments →
BirdNET v2.4 analysis (birdnet-analyzer, TFLite) → SQLite via database.py.

Uses:
  - sounddevice — microphone audio capture
  - numpy — buffering
  - birdnet_analyzer — the ORIGINAL BirdNET package by Kai Hilbert / Cornell
    (TFLite model V2.4, 48 kHz, 6522 species, sigmoid confidence)
  - database.Database — storing detection results

Default coordinates: Moscow 55.75°N, 37.62°E.
Default confidence threshold: 0.25.
"""

import logging
import queue
import threading
import time
from datetime import datetime
from typing import Optional

import numpy as np
import sounddevice

from database import Database

logger = logging.getLogger("avian.audio")

# ─── Constants ────────────────────────────────────────────────────────

# BirdNET v2.4 expects 48 kHz
SAMPLE_RATE = 48_000
# Segment length for analysis (seconds)
SEGMENT_DURATION = 3.0
# Number of samples in a segment
SEGMENT_SAMPLES = int(SAMPLE_RATE * SEGMENT_DURATION)
# BirdNET expects mono
CHANNELS = 1
# Confidence threshold for recording a detection
CONFIDENCE_THRESHOLD = 0.25
# Default coordinates: Moscow
LATITUDE = 55.75
LONGITUDE = 37.62


def _current_week() -> int:
    """Return the ISO week of the year (1-53), used for geo-filtering."""
    return datetime.now().isocalendar()[1]


class AudioListener:
    """
    Captures audio from microphone and analyzes it with BirdNET.

    Works in a background thread. Start via start(), stop via stop().

    The BirdNET model (birdnet-analyzer) is loaded lazily on first use.
    Geo-filtering (latitude, longitude, week) restricts predictions
    to species expected at the given location and time of year.

    Usage::

        db = Database()
        db.init()
        listener = AudioListener(db)
        listener.start()
        # ... runs in background ...
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
        week: int = -1,
        sensitivity: float = 1.0,
        sf_thresh: float = 0.03,
        device=None,
    ):
        self.db = db
        self.sample_rate = sample_rate
        self.segment_duration = segment_duration
        self.segment_samples = int(sample_rate * segment_duration)
        self.confidence_threshold = confidence_threshold
        self.latitude = latitude
        self.longitude = longitude
        self.week = week  # -1 = auto (current week)
        self.sensitivity = sensitivity
        self.sf_thresh = sf_thresh
        self.device = device  # None = default mic

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Lazy-loaded state (thread-safe, once)
        self._model_loaded = False
        self._model_lock = threading.Lock()
        # References populated by _ensure_model():
        self._labels: list[str] = []
        self._species_list: list[str] = []

        # Counters for diagnostics
        self._segments_processed = 0
        self._detections_written = 0
        self._start_time: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Starts capture and analysis in a background thread."""
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
            "confidence > %.2f, lat=%.2f lon=%.2f week=%s",
            self.sample_rate,
            self.segment_duration,
            self.confidence_threshold,
            self.latitude,
            self.longitude,
            self.week if self.week > 0 else "auto",
        )

    def stop(self):
        """Stops capture (graceful, waits for current segment to finish)."""
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
        Loads the BirdNET v2.4 TFLite model (via birdnet-analyzer) once.

        Sets:
          - birdnet_analyzer.config globals (MODEL_PATH, LABELS, etc.)
          - self._labels: full label list (6522 entries)
          - self._species_list: geo-filtered species list
        """
        if self._model_loaded:
            return

        with self._model_lock:
            if self._model_loaded:
                return

            import birdnet_analyzer.config as cfg
            from birdnet_analyzer import utils as ba_utils
            from birdnet_analyzer.species.utils import get_species_list

            # Configure birdnet-analyzer for BirdNET v2.4 (TFLite)
            cfg.MODEL_PATH = cfg.BIRDNET_MODEL_PATH
            cfg.LABELS_FILE = cfg.BIRDNET_LABELS_FILE
            cfg.SAMPLE_RATE = cfg.BIRDNET_SAMPLE_RATE
            cfg.SIG_LENGTH = cfg.BIRDNET_SIG_LENGTH
            cfg.TFLITE_THREADS = 4
            cfg.APPLY_SIGMOID = True
            cfg.SIGMOID_SENSITIVITY = self.sensitivity

            # Load eBird codes
            from birdnet_analyzer.analyze.utils import load_codes
            cfg.CODES = load_codes()

            # Load labels
            cfg.LABELS = ba_utils.read_lines(cfg.LABELS_FILE)

            # Build species list for geo-filtering
            week = self.week if self.week > 0 else _current_week()
            cfg.LATITUDE = self.latitude
            cfg.LONGITUDE = self.longitude
            cfg.WEEK = week
            cfg.LOCATION_FILTER_THRESHOLD = self.sf_thresh

            if self.latitude > 0 and self.longitude > 0:
                cfg.SPECIES_LIST = get_species_list(
                    self.latitude, self.longitude, week, self.sf_thresh
                )
                logger.info(
                    "Geo-filter: lat=%.2f lon=%.2f week=%d → %d species on list",
                    self.latitude, self.longitude, week,
                    len(cfg.SPECIES_LIST),
                )
            else:
                cfg.SPECIES_LIST = []
                logger.info("No geo-filter — using all %d species", len(cfg.LABELS))

            self._labels = cfg.LABELS
            self._species_list = cfg.SPECIES_LIST
            self._model_loaded = True

            logger.info(
                "BirdNET v2.4 model ready. Labels=%d, Species list=%d",
                len(self._labels),
                len(self._species_list),
            )

    # ------------------------------------------------------------------
    # Internal: capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self):
        """
        Main capture loop.

        Uses PortAudio callback mode for cross-platform reliability.
        On macOS, blocking read() from a non-main thread causes
        'Stream is stopped' errors. Callback mode lets PortAudio manage
        its own internal thread.

        Audio is accumulated into a numpy buffer; once 3 seconds are
        collected, the segment is put on a queue for BirdNET processing.
        """
        segment_queue = queue.Queue()
        buf = np.zeros(self.segment_samples, dtype="float32")
        buf_pos = [0]  # mutable container for closure

        def _audio_callback(indata, frames, time_info, status):
            """PortAudio callback — fills ring buffer, enqueues full segments."""
            if status:
                logger.debug("Audio status: %s", status)
            chunk = indata[:, 0] if indata.ndim > 1 else indata
            chunk_len = len(chunk)
            remaining = self.segment_samples - buf_pos[0]
            if chunk_len >= remaining:
                buf[buf_pos[0]:] = chunk[:remaining]
                try:
                    segment_queue.put_nowait(buf.copy())
                except queue.Full:
                    pass  # drop if processing can't keep up
                leftover = chunk[remaining:]
                buf[:len(leftover)] = leftover
                buf_pos[0] = len(leftover)
            else:
                buf[buf_pos[0]:buf_pos[0] + chunk_len] = chunk
                buf_pos[0] += chunk_len

        stream = None
        try:
            blocksize = 4096
            stream = sounddevice.InputStream(
                samplerate=self.sample_rate,
                channels=CHANNELS,
                dtype="float32",
                device=self.device,
                blocksize=blocksize,
                callback=_audio_callback,
            )
            dev = stream.device
            if isinstance(dev, (tuple, list)) and len(dev) > 0:
                dev_name = dev[0].get("name", "unknown") if isinstance(dev[0], dict) else str(dev[0])
            elif isinstance(dev, dict):
                dev_name = dev.get("name", "unknown")
            elif isinstance(dev, int):
                dev_name = f"device-{dev}"
            else:
                dev_name = "default"
            logger.info(
                "Audio device opened: %s @ %d Hz, blocksize=%d (callback mode)",
                dev_name, stream.samplerate, blocksize,
            )

            # Process segments from the queue
            while self._running:
                try:
                    segment = segment_queue.get(timeout=1.0)
                    self._process_segment(segment)
                except queue.Empty:
                    continue
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
        Runs one 3-second audio segment through BirdNET and writes
        detections to the database.

        The pipeline:
          1. Pad to exactly 144000 samples (3s × 48kHz) if shorter
          2. Call birdnet_analyzer.model.predict() → logits (1, 6522)
          3. Apply flat_sigmoid to get confidence scores
          4. Filter by geo-species list and confidence threshold
          5. Write matching detections to database

        Args:
            audio: numpy array, shape (segment_samples,), float32.
        """
        try:
            self._ensure_model()
        except Exception as e:
            logger.error("Failed to load BirdNET model: %s", e)
            return

        self._segments_processed += 1

        import birdnet_analyzer.config as cfg
        from birdnet_analyzer import model as ba_model

        # Pad to exactly segment length if needed (short last segment)
        target_len = int(cfg.SAMPLE_RATE * cfg.SIG_LENGTH)
        if len(audio) < target_len:
            audio = np.pad(audio, (0, target_len - len(audio)), mode="constant")
        elif len(audio) > target_len:
            audio = audio[:target_len]

        # Reshape for batch prediction: (1, 144000)
        sample = audio.astype("float32").reshape(1, -1)

        # Run inference
        try:
            prediction = ba_model.predict(sample)
        except Exception as e:
            logger.error("BirdNET prediction failed: %s", e)
            return

        # Apply sigmoid: logits → probabilities
        # flat_sigmoid with sensitivity=-1, bias=sensitivity
        probs = ba_model.flat_sigmoid(
            np.array(prediction), sensitivity=-1, bias=cfg.SIGMOID_SENSITIVITY
        )[0]

        # Build detections: filter by species list and confidence
        now = datetime.now()
        detections_this_segment = 0
        species_set = set(self._species_list) if self._species_list else None

        # Get indices sorted by confidence (descending) for early exit
        # if we only need top results
        for i, conf in enumerate(probs):
            if conf < self.confidence_threshold:
                continue

            label = self._labels[i]

            # Geo-filter: skip species not on the list
            if species_set is not None and label not in species_set:
                continue

            # Parse label: "Genus species_Common Name"
            sci_name, com_name = self._parse_label(label)

            self.db.insert_detection(
                sci_name=sci_name,
                com_name=com_name,
                confidence=float(conf),
                dt=now,
            )
            self._detections_written += 1
            detections_this_segment += 1
            logger.debug(
                "  [%.3f] %s (%s)",
                float(conf),
                sci_name,
                com_name,
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

    # ------------------------------------------------------------------
    # Internal: label parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_label(label: str) -> tuple[str, str]:
        """
        Parse a BirdNET label string into (scientific_name, common_name).

        BirdNET label format: "Genus species_Common Name"
        Example: "Parus major_Great Tit"

        Returns:
            Tuple of (scientific_name, common_name).
        """
        if "_" in label:
            idx = label.index("_")
            return label[:idx], label[idx + 1:]
        return label, label


# ── File analysis (for uploaded files, not real-time) ───────────────── #

def analyze_file(
    file_path: str,
    db: Database,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    latitude: float = LATITUDE,
    longitude: float = LONGITUDE,
    week: int = -1,
    sensitivity: float = 1.0,
    sf_thresh: float = 0.03,
) -> dict:
    """
    Analyze an audio file (WAV/MP3/OGG/M4A/FLAC) with BirdNET and
    write detections to the database.

    This is a convenience function for processing uploaded files,
    as opposed to real-time microphone capture.

    Args:
        file_path: Path to the audio file.
        db: Database instance for storing results.
        confidence_threshold: Minimum confidence (0-1) to record.
        latitude: Latitude for geo-filtering.
        longitude: Longitude for geo-filtering.
        week: Week of year (1-53). -1 = auto (current).
        sensitivity: Sigmoid sensitivity (1.0 = default).
        sf_thresh: Species filter threshold for geo-filtering.

    Returns:
        Dict with 'segments_analyzed', 'detections_written', 'top_detections'.
    """
    import birdnet_analyzer.config as cfg
    from birdnet_analyzer import audio as ba_audio, model as ba_model, utils as ba_utils
    from birdnet_analyzer.analyze.utils import load_codes
    from birdnet_analyzer.species.utils import get_species_list

    # Configure birdnet-analyzer
    cfg.MODEL_PATH = cfg.BIRDNET_MODEL_PATH
    cfg.LABELS_FILE = cfg.BIRDNET_LABELS_FILE
    cfg.SAMPLE_RATE = cfg.BIRDNET_SAMPLE_RATE
    cfg.SIG_LENGTH = cfg.BIRDNET_SIG_LENGTH
    cfg.SIG_OVERLAP = 0.0
    cfg.SIG_MINLEN = cfg.SIG_MINLEN
    cfg.BANDPASS_FMIN = 0
    cfg.BANDPASS_FMAX = 15000
    cfg.AUDIO_SPEED = 1.0
    cfg.FILE_SPLITTING_DURATION = 600
    cfg.TFLITE_THREADS = 4
    cfg.BATCH_SIZE = 1
    cfg.APPLY_SIGMOID = True
    cfg.SIGMOID_SENSITIVITY = sensitivity
    cfg.MIN_CONFIDENCE = confidence_threshold
    cfg.CUSTOM_CLASSIFIER = None
    cfg.USE_PERCH = False
    cfg.USE_NOISE = False
    cfg.CODES = load_codes()
    cfg.LABELS = ba_utils.read_lines(cfg.LABELS_FILE)

    # Geo-filtering
    week_val = week if week > 0 else _current_week()
    cfg.LATITUDE = latitude
    cfg.LONGITUDE = longitude
    cfg.WEEK = week_val
    cfg.LOCATION_FILTER_THRESHOLD = sf_thresh

    if latitude > 0 and longitude > 0:
        species_list = get_species_list(latitude, longitude, week_val, sf_thresh)
    else:
        species_list = []
    species_set = set(species_list) if species_list else None

    # Load model
    ba_model.load_model(class_output=True)
    logger.info(
        "Analyzing file: %s (species list: %d, labels: %d)",
        file_path, len(species_list), len(cfg.LABELS),
    )

    # Read and split audio
    sig, rate = ba_audio.open_audio_file(
        file_path, cfg.SAMPLE_RATE, 0, None,
        cfg.BANDPASS_FMIN, cfg.BANDPASS_FMAX, cfg.AUDIO_SPEED,
    )
    chunks = ba_audio.split_signal(sig, rate, cfg.SIG_LENGTH, cfg.SIG_OVERLAP, cfg.SIG_MINLEN)

    segments_analyzed = 0
    detections_written = 0
    all_detections = []

    for chunk_index, chunk in enumerate(chunks):
        # Pad if needed
        target_len = int(cfg.SAMPLE_RATE * cfg.SIG_LENGTH)
        if len(chunk) < target_len:
            chunk = np.pad(chunk, (0, target_len - len(chunk)), mode="constant")
        elif len(chunk) > target_len:
            chunk = chunk[:target_len]

        sample = chunk.astype("float32").reshape(1, -1)

        # Predict
        prediction = ba_model.predict(sample)
        probs = ba_model.flat_sigmoid(
            np.array(prediction), sensitivity=-1, bias=cfg.SIGMOID_SENSITIVITY
        )[0]

        segments_analyzed += 1
        t_start = round(chunk_index * cfg.SIG_LENGTH, 2)
        t_end = round(t_start + cfg.SIG_LENGTH, 2)

        now = datetime.now()

        for i, conf in enumerate(probs):
            if conf < confidence_threshold:
                continue

            label = cfg.LABELS[i]
            if species_set is not None and label not in species_set:
                continue

            sci_name, com_name = AudioListener._parse_label(label)

            db.insert_detection(
                sci_name=sci_name,
                com_name=com_name,
                confidence=float(conf),
                dt=now,
            )
            detections_written += 1
            all_detections.append({
                "start": t_start,
                "end": t_end,
                "sci_name": sci_name,
                "com_name": com_name,
                "confidence": float(conf),
            })

    # Sort all detections by confidence descending and take top 10
    all_detections.sort(key=lambda d: d["confidence"], reverse=True)
    top = all_detections[:10]

    logger.info(
        "File analysis complete: %d segments, %d detections",
        segments_analyzed, detections_written,
    )

    return {
        "segments_analyzed": segments_analyzed,
        "detections_written": detections_written,
        "top_detections": top,
    }


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