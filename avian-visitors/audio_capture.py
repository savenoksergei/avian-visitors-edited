"""
audio_capture.py — Audio capture + BirdNET analysis.
Blocking mode + ring buffer for macOS compatibility.
"""
import logging
import threading
import time
from datetime import datetime
from typing import Optional
import numpy as np
import sounddevice as sd
from database import Database

logger = logging.getLogger("avian.audio")
SAMPLE_RATE = 48_000
SEGMENT_DURATION = 3.0
SEGMENT_SAMPLES = int(SAMPLE_RATE * SEGMENT_DURATION)
CHANNELS = 1
CONFIDENCE_THRESHOLD = 0.25
LATITUDE = 55.75
LONGITUDE = 37.62

def _current_week() -> int:
    return datetime.now().isocalendar()[1]

class AudioListener:
    def __init__(self, db, sample_rate=SAMPLE_RATE, segment_duration=SEGMENT_DURATION,
                 confidence_threshold=CONFIDENCE_THRESHOLD, latitude=LATITUDE,
                 longitude=LONGITUDE, week=-1, sensitivity=1.0,
                 sf_thresh=0.03, device=None):
        self.db = db
        self.sample_rate = sample_rate
        sample_rate = sample_rate
        self.segment_duration = segment_duration
        self.segment_samples = int(sample_rate * segment_duration)
        self.confidence_threshold = confidence_threshold
        self.latitude = latitude
        self.longitude = longitude
        self.week = week
        self.sensitivity = sensitivity
        self.sf_thresh = sf_thresh
        self.device = device
        self._running = False
        self._thread = None
        self._stop_event = threading.Event()
        self._model_loaded = False
        self._model_lock = threading.Lock()
        self._labels = []
        self._species_list = []
        self._segments_processed = 0
        self._detections_written = 0
        self._start_time = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._start_time = time.time()
        self._segments_processed = 0
        self._detections_written = 0
        self._thread = threading.Thread(target=self._capture_loop,
                                        name="audio-capture", daemon=True)
        self._thread.start()
        logger.info("Audio capture started: %d Hz, %.1fs segments, "
                     "confidence > %.2f, lat=%.2f lon=%.2f week=%s",
                     self.sample_rate, self.segment_duration,
                     self.confidence_threshold, self.latitude,
                     self.longitude,
                     self.week if self.week > 0 else "auto")

    def stop(self):
        if not self._running:
            return
        logger.info("Stopping audio capture...")
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._running = False
        elapsed = time.time() - self._start_time if self._start_time else 0
        logger.info("Audio capture stopped. Segments=%d, Detections=%d, Time=%.1fs",
                     self._segments_processed, self._detections_written, elapsed)

    @property
    def is_running(self):
        return self._running

    @property
    def stats(self):
        return {"segments_processed": self._segments_processed,
                "detections_written": self._detections_written,
                "uptime_seconds": (time.time() - self._start_time)
                if self._start_time else 0}

    def _ensure_model(self):
        if self._model_loaded:
            return
        with self._model_lock:
            if self._model_loaded:
                return
            import birdnet_analyzer.config as cfg
            from birdnet_analyzer import utils as ba_utils
            from birdnet_analyzer.species.utils import get_species_list
            from birdnet_analyzer.analyze.utils import load_codes

            cfg.MODEL_PATH = cfg.BIRDNET_MODEL_PATH
            cfg.LABELS_FILE = cfg.BIRDNET_LABELS_FILE
            cfg.SAMPLE_RATE = cfg.BIRDNET_SAMPLE_RATE
            cfg.SIG_LENGTH = cfg.BIRDNET_SIG_LENGTH
            cfg.TFLITE_THREADS = 4
            cfg.APPLY_SIGMOID = True
            cfg.SIGMOID_SENSITIVITY = self.sensitivity
            cfg.CODES = load_codes()
            cfg.LABELS = ba_utils.read_lines(cfg.LABELS_FILE)

            week = self.week if self.week > 0 else _current_week()
            cfg.LATITUDE = self.latitude
            cfg.LONGITUDE = self.longitude
            cfg.WEEK = week
            cfg.LOCATION_FILTER_THRESHOLD = self.sf_thresh

            if self.latitude > 0 and self.longitude > 0:
                cfg.SPECIES_LIST = get_species_list(
                    self.latitude, self.longitude, week, self.sf_thresh)
                logger.info("Geo-filter: %d species", len(cfg.SPECIES_LIST))
            else:
                cfg.SPECIES_LIST = []
            self._labels = cfg.LABELS
            self._species_list = cfg.SPECIES_LIST
            self._model_loaded = True
            logger.info("BirdNET v2.4 ready. Labels=%d", len(self._labels))

    def _capture_loop(self):
        blocksize = 4096
        buf = np.zeros(self.segment_samples, dtype="float32")
        buf_pos = 0
        stream = None

        try:
            if self.device is not None:
                try:
                    di = sd.query_devices(self.device)
                    logger.info("Device %d: %s (%d Hz, %d inputs)",
                                self.device, di["name"],
                                int(di["default_samplerate"]),
                                di["max_input_channels"])
                except Exception as e:
                    logger.warning("query_devices(%d): %s", self.device, e)

            stream = sd.InputStream(samplerate=self.sample_rate,
                                    channels=CHANNELS, dtype="float32",
                                    device=self.device, blocksize=blocksize)

            dev = stream.device
            if isinstance(dev, int):
                dn = f"device-{dev}"
            elif isinstance(dev, (tuple, list)) and len(dev) > 0:
                dn = dev[0].get("name", "?") if isinstance(dev[0], dict) else str(dev[0])
            elif isinstance(dev, dict):
                dn = dev.get("name", "?")
            else:
                dn = "default"
            logger.info("Audio opened: %s @ %d Hz, blocksize=%d (blocking)",
                        dn, stream.samplerate, blocksize)

            stream.start()
            logger.info("Stream started, reading...")
            _hb = time.time()

            while self._running and not self._stop_event.is_set():
                try:
                    data, overflow = stream.read(blocksize)
                except sd.PortAudioError as e:
                    es = str(e)
                    logger.warning("PortAudio: %s", es)
                    if "stopped" in es.lower() or "9983" in es:
                        logger.info("Stream died, restarting in 2s...")
                        try:
                            stream.stop(); stream.close()
                        except Exception:
                            pass
                        time.sleep(2)
                        if not self._running:
                            break
                        try:
                            stream = sd.InputStream(
                                samplerate=self.sample_rate,
                                channels=CHANNELS, dtype="float32",
                                device=self.device, blocksize=blocksize)
                            stream.start()
                            logger.info("Restarted OK")
                            _hb = time.time()
                            continue
                        except Exception as re:
                            logger.error("Restart fail: %s", re)
                            time.sleep(3)
                            continue
                    time.sleep(0.5)
                    continue
                except Exception as e:
                    logger.error("Read error: %s", e)
                    time.sleep(1)
                    continue

                if overflow:
                    logger.debug("overflow")

                chunk = data[:, 0] if data.ndim > 1 else data.flatten()
                cl = len(chunk)
                rem = self.segment_samples - buf_pos

                if cl >= rem:
                    buf[buf_pos:] = chunk[:rem]
                    self._process_segment(buf.copy())
                    left = chunk[rem:]
                    buf[:len(left)] = left
                    buf_pos = len(left)
                    _hb = time.time()
                else:
                    buf[buf_pos:buf_pos + cl] = chunk
                    buf_pos += cl

                if self._segments_processed == 0 and time.time() - _hb > 15:
                    logger.info("Heartbeat: stream OK, buf=%d/%d (%.1fs)",
                                buf_pos, self.segment_samples,
                                buf_pos / self.sample_rate)
                    _hb = time.time()

        except Exception as e:
            logger.error("Fatal: %s", e, exc_info=True)
        finally:
            if stream:
                try:
                    stream.stop(); stream.close()
                except Exception:
                    pass
            self._running = False

    def _process_segment(self, audio):
        try:
            self._ensure_model()
        except Exception as e:
            logger.error("Model load fail: %s", e)
            return
        self._segments_processed += 1

        import birdnet_analyzer.config as cfg
        from birdnet_analyzer import model as ba_model

        tl = int(cfg.SAMPLE_RATE * cfg.SIG_LENGTH)
        if len(audio) < tl:
            audio = np.pad(audio, (0, tl - len(audio)))
        elif len(audio) > tl:
            audio = audio[:tl]

        sample = audio.astype("float32").reshape(1, -1)
        try:
            pred = ba_model.predict(sample)
        except Exception as e:
            logger.error("Predict fail: %s", e)
            return

        probs = ba_model.flat_sigmoid(np.array(pred),
                                       sensitivity=-1,
                                       bias=cfg.SIGMOID_SENSITIVITY)[0]
        now = datetime.now()
        det = 0
        ss = set(self._species_list) if self._species_list else None

        for i, conf in enumerate(probs):
            if conf < self.confidence_threshold:
                continue
            label = self._labels[i]
            if ss is not None and label not in ss:
                continue
            sci, com = self._parse_label(label)
            self.db.insert_detection(sci_name=sci, com_name=com,
                                     confidence=float(conf), dt=now)
            self._detections_written += 1
            det += 1
            logger.debug("  [%.3f] %s (%s)", conf, sci, com)

        if det == 0:
            logger.debug("Seg #%d: no hits > %.2f",
                         self._segments_processed, self.confidence_threshold)
        else:
            logger.info("Seg #%d: %d detection(s)",
                        self._segments_processed, det)

    @staticmethod
    def _parse_label(label):
        if "_" in label:
            i = label.index("_")
            return label[:i], label[i + 1:]
        return label, label


def analyze_file(file_path, db, confidence_threshold=CONFIDENCE_THRESHOLD,
                 latitude=LATITUDE, longitude=LONGITUDE, week=-1,
                 sensitivity=1.0, sf_thresh=0.03):
    import birdnet_analyzer.config as cfg
    from birdnet_analyzer import audio as ba_audio, model as ba_model
    from birdnet_analyzer import utils as ba_utils
    from birdnet_analyzer.analyze.utils import load_codes
    from birdnet_analyzer.species.utils import get_species_list

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

    wv = week if week > 0 else _current_week()
    cfg.LATITUDE = latitude
    cfg.LONGITUDE = longitude
    cfg.WEEK = wv
    cfg.LOCATION_FILTER_THRESHOLD = sf_thresh

    if latitude > 0 and longitude > 0:
        sl = get_species_list(latitude, longitude, wv, sf_thresh)
    else:
        sl = []
    ss = set(sl) if sl else None

    ba_model.load_model(class_output=True)
    sig, rate = ba_audio.open_audio_file(
        file_path, cfg.SAMPLE_RATE, 0, None,
        cfg.BANDPASS_FMIN, cfg.BANDPASS_FMAX, cfg.AUDIO_SPEED)
    chunks = ba_audio.split_signal(sig, rate, cfg.SIG_LENGTH,
                                    cfg.SIG_OVERLAP, cfg.SIG_MINLEN)

    sa = 0; dw = 0; all_d = []
    for ci, chunk in enumerate(chunks):
        tl = int(cfg.SAMPLE_RATE * cfg.SIG_LENGTH)
        if len(chunk) < tl:
            chunk = np.pad(chunk, (0, tl - len(chunk)))
        elif len(chunk) > tl:
            chunk = chunk[:tl]
        pred = ba_model.predict(chunk.astype("float32").reshape(1, -1))
        probs = ba_model.flat_sigmoid(np.array(pred), sensitivity=-1,
                                       bias=cfg.SIGMOID_SENSITIVITY)[0]
        sa += 1
        ts = round(ci * cfg.SIG_LENGTH, 2)
        te = round(ts + cfg.SIG_LENGTH, 2)
        now = datetime.now()
        for i, conf in enumerate(probs):
            if conf < confidence_threshold:
                continue
            label = cfg.LABELS[i]
            if ss is not None and label not in ss:
                continue
            sci, com = AudioListener._parse_label(label)
            db.insert_detection(sci_name=sci, com_name=com,
                                confidence=float(conf), dt=now)
            dw += 1
            all_d.append({"start": ts, "end": te, "sci_name": sci,
                          "com_name": com, "confidence": float(conf)})

    all_d.sort(key=lambda d: d["confidence"], reverse=True)
    logger.info("File done: %d segs, %d det", sa, dw)
    return {"segments_analyzed": sa, "detections_written": dw,
            "top_detections": all_d[:10]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")
    print("=== Standalone test (10s) ===")
    db = Database(); db.init()
    l = AudioListener(db, confidence_threshold=0.01)
    l.start()
    for i in range(10):
        time.sleep(1)
        print(f"  {i+1}s {l.stats}")
    l.stop()
    print(f"Final: {l.stats}")
    db.close()