"""
main.py — единая точка входа для AvianVisitors Desktop.

Запуск:
    python main.py
    python main.py --port 9090
    python main.py --no-audio
    python main.py --lat 59.93 --lon 30.32

Что делает:
    1. Читает data/desktop_config.json (если есть) → env-переменные
    2. Парсит CLI-аргументы (переопределяют конфиг)
    3. Настраивает логирование (консоль + файл)
    4. Запускает uvicorn с api.app
    5. Через 1 сек открывает браузер
    6. Обрабатывает Ctrl+C → graceful shutdown

Переменные окружения (приоритет: CLI > env > config.json > defaults):
    AVIAN_PORT          — порт uvicorn (по умолчанию 8080)
    AVIAN_NO_AUDIO      — "1" отключает микрофон
    AVIAN_LAT           — широта (по умолчанию 55.75, Москва)
    AVIAN_LON           — долгота (по умолчанию 37.62, Москва)
    AVIAN_CONFIDENCE    — порог уверенности (по умолчанию 0.25)
    AVIAN_SENSITIVITY   — чувствительность BirdNET (по умолчанию 1.0)
    AVIAN_LOG_LEVEL     — уровень логов (DEBUG/INFO/WARNING, по умолчанию INFO)
    AVIAN_NO_BROWSER    — "1" не открывать браузер автоматически
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

# ── Paths ────────────────────────────────────────────────────────────── #

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
CONFIG_FILE = DATA_DIR / "desktop_config.json"
LOG_FILE = DATA_DIR / "avian-visitors.log"

# ── Defaults ─────────────────────────────────────────────────────────── #

DEFAULTS = {
    "port": 8080,
    "lat": 55.75,
    "lon": 37.62,
    "confidence": 0.25,
    "sensitivity": 1.0,
    "no_audio": False,
    "no_browser": False,
    "log_level": "INFO",
}


# ── Config loading ───────────────────────────────────────────────────── #

def _load_config() -> dict:
    """Read data/desktop_config.json and return a dict of values."""
    if not CONFIG_FILE.is_file():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"Warning: cannot read config {CONFIG_FILE}: {e}", file=sys.stderr)
        return {}


def _merge_settings(args: argparse.Namespace) -> dict:
    """
    Merge settings from 3 sources (lowest to highest priority):
      1. config.json defaults
      2. environment variables
      3. CLI arguments
    """
    cfg = _load_config()

    # Map config.json keys to our settings
    settings = {
        "port": int(os.environ.get("AVIAN_PORT", cfg.get("PORT", DEFAULTS["port"]))),
        "lat": float(os.environ.get("AVIAN_LAT", cfg.get("LATITUDE", DEFAULTS["lat"]))),
        "lon": float(os.environ.get("AVIAN_LON", cfg.get("LONGITUDE", DEFAULTS["lon"]))),
        "confidence": float(os.environ.get("AVIAN_CONFIDENCE", cfg.get("CONFIDENCE", DEFAULTS["confidence"]))),
        "sensitivity": float(os.environ.get("AVIAN_SENSITIVITY", cfg.get("SENSITIVITY", DEFAULTS["sensitivity"]))),
        "no_audio": os.environ.get("AVIAN_NO_AUDIO", "").lower() in ("1", "true", "yes"),
        "no_browser": os.environ.get("AVIAN_NO_BROWSER", "").lower() in ("1", "true", "yes"),
        "log_level": os.environ.get("AVIAN_LOG_LEVEL", DEFAULTS["log_level"]).upper(),
    }

    # CLI overrides
    if args.port is not None:
        settings["port"] = args.port
    if args.lat is not None:
        settings["lat"] = args.lat
    if args.lon is not None:
        settings["lon"] = args.lon
    if args.confidence is not None:
        settings["confidence"] = args.confidence
    if args.sensitivity is not None:
        settings["sensitivity"] = args.sensitivity
    if args.no_audio:
        settings["no_audio"] = True
    if args.no_browser:
        settings["no_browser"] = True
    if args.log_level:
        settings["log_level"] = args.log_level.upper()

    return settings


def _apply_env(settings: dict) -> None:
    """Set environment variables so api.py and audio_capture.py pick them up."""
    os.environ["AVIAN_PORT"] = str(settings["port"])
    os.environ["AVIAN_LAT"] = str(settings["lat"])
    os.environ["AVIAN_LON"] = str(settings["lon"])
    os.environ["AVIAN_CONFIDENCE"] = str(settings["confidence"])
    os.environ["AVIAN_SENSITIVITY"] = str(settings["sensitivity"])

    if settings["no_audio"]:
        os.environ["AVIAN_NO_AUDIO"] = "1"


# ── Logging setup ────────────────────────────────────────────────────── #

def _setup_logging(level_name: str) -> None:
    """Configure root logger: console (coloured) + file (plain)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, level_name, logging.INFO)

    fmt_console = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    fmt_file = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt_console)
    console.setLevel(level)

    # File handler (append, rotated externally if needed)
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt_file)
    file_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


# ── Browser opener ───────────────────────────────────────────────────── #

def _open_browser(port: int):
    """Open browser after a short delay (runs in daemon thread)."""
    import time
    time.sleep(1.5)
    url = f"http://localhost:{port}"
    logging.getLogger("avian.main").info("Opening browser: %s", url)
    webbrowser.open(url)


# ── CLI ──────────────────────────────────────────────────────────────── #

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="AvianVisitors Desktop — bird sound identification",
    )
    p.add_argument("--port", type=int, default=None, help="HTTP port (default: 8080)")
    p.add_argument("--lat", type=float, default=None, help="Latitude (default: 55.75)")
    p.add_argument("--lon", type=float, default=None, help="Longitude (default: 37.62)")
    p.add_argument("--confidence", type=float, default=None, help="Min confidence 0-1 (default: 0.25)")
    p.add_argument("--sensitivity", type=float, default=None, help="BirdNET sensitivity (default: 1.0)")
    p.add_argument("--no-audio", action="store_true", help="Disable microphone capture")
    p.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    p.add_argument("--log-level", type=str, default=None, choices=["DEBUG", "INFO", "WARNING"],
                    help="Log level (default: INFO)")
    return p.parse_args()


# ── Main ─────────────────────────────────────────────────────────────── #

def run() -> None:
    """Main entry point."""
    args = _parse_args()
    settings = _merge_settings(args)

    # Ensure data dir exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Setup logging before anything else
    _setup_logging(settings["log_level"])
    log = logging.getLogger("avian.main")

    # Apply settings as env vars for api.py / audio_capture.py
    _apply_env(settings)

    port = settings["port"]
    log.info("AvianVisitors Desktop starting...")
    log.info("  Port:       %d", port)
    log.info("  Location:   %.4f, %.4f", settings["lat"], settings["lon"])
    log.info("  Confidence: %.2f", settings["confidence"])
    log.info("  Sensitivity: %.1f", settings["sensitivity"])
    log.info("  Audio:      %s", "disabled" if settings["no_audio"] else "enabled")
    log.info("  Log file:   %s", LOG_FILE)

    # Open browser in background
    if not settings["no_browser"]:
        t = threading.Thread(target=_open_browser, args=(port,), daemon=True)
        t.start()

    # Run uvicorn
    log.info("Starting uvicorn on port %d...", port)
    try:
        uvicorn.run(
            "api:app",
            host="0.0.0.0",
            port=port,
            log_level=settings["log_level"].lower(),
            reload=False,
        )
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        log.info("AvianVisitors Desktop stopped")


if __name__ == "__main__":
    run()
