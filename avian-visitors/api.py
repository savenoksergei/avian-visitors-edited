"""
api.py — FastAPI endpoints for AvianVisitors Desktop.

Exposes JSON endpoints that map 1:1 to database.py methods,
plus an audio file upload endpoint that delegates to audio_capture.analyze_file().

Endpoints:
  GET  /api/stats              → Database.stats()
  GET  /api/recent             → Database.recent(hours)
  GET  /api/lifelist           → Database.lifelist()
  GET  /api/species            → Database.species_detail(sci)
  GET  /api/timeseries         → Database.timeseries(days)
  GET  /api/firstseen          → Database.firstseen(limit)
  POST /api/upload             → analyze_file() from audio_capture.py
  GET  /api/listener/status    → AudioListener diagnostics

The Database and (optional) AudioListener instances are created at module level
and initialised via the lifespan() context manager, so that every endpoint
shares the same objects.
"""

import logging
import os
import tempfile
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile

from database import Database
from audio_capture import AudioListener, analyze_file, LATITUDE, LONGITUDE, CONFIDENCE_THRESHOLD

logger = logging.getLogger("avian.api")

# ── Module-level singletons (set during lifespan) ──────────────────── #

db: Database = Database()          # type: ignore[assignment]
listener: Optional[AudioListener] = None


# ── Lifespan: init DB & start listener ─────────────────────────────── #

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB and optionally start the AudioListener."""
    global listener
    logger.info("Initializing database...")
    db.init()
    logger.info("Database ready at %s", db.db_path)

    # Start audio listener if not disabled via env var
    if os.environ.get("AVIAN_NO_AUDIO", "").lower() in ("1", "true", "yes"):
        logger.info("Audio capture disabled (AVIAN_NO_AUDIO=1)")
    else:
        lat = float(os.environ.get("AVIAN_LAT", str(LATITUDE)))
        lon = float(os.environ.get("AVIAN_LON", str(LONGITUDE)))
        listener = AudioListener(db, latitude=lat, longitude=lon)
        listener.start()
        logger.info("Audio listener started (lat=%.2f, lon=%.2f)", lat, lon)

    yield

    # Shutdown
    if listener is not None:
        listener.stop()
        logger.info("Audio listener stopped")
    db.close()
    logger.info("Database closed, shutdown complete")


# ── FastAPI app ────────────────────────────────────────────────────── #

app = FastAPI(
    title="AvianVisitors Desktop",
    description="Bird sound identification API (BirdNET v2.4)",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Health / diagnostics ───────────────────────────────────────────── #

@app.get("/api/health")
async def health():
    """Basic health check."""
    return {"status": "ok"}


@app.get("/api/listener/status")
async def listener_status():
    """Return AudioListener diagnostics (segments processed, uptime, etc.)."""
    if listener is None:
        return {
            "running": False,
            "reason": "Audio capture is disabled (AVIAN_NO_AUDIO=1)",
        }
    return {
        "running": listener.is_running,
        **listener.stats,
    }


# ── Data endpoints (thin wrappers around Database) ─────────────────── #

@app.get("/api/stats")
async def stats():
    """Overall statistics: totals, today, last_hour, week, started."""
    return db.stats()


@app.get("/api/recent")
async def recent(
    hours: int = Query(default=24, ge=1, le=1_000_000, description="Time window in hours"),
):
    """Species detected in the last N hours, aggregated by species."""
    return db.recent(hours)


@app.get("/api/lifelist")
async def lifelist():
    """All species ever detected, sorted by first detection."""
    return db.lifelist()


@app.get("/api/species")
async def species(
    sci: str = Query(..., description="Scientific name, e.g. 'Parus major'"),
):
    """Detail for a single species: summary + recent detections."""
    result = db.species_detail(sci)
    if result["summary"] is None:
        raise HTTPException(status_code=404, detail=f"Species not found: {sci}")
    return result


@app.get("/api/timeseries")
async def timeseries(
    days: int = Query(default=30, ge=1, le=90, description="Number of days"),
):
    """Daily and hourly detection counts for charts."""
    return db.timeseries(days)


@app.get("/api/firstseen")
async def firstseen(
    limit: int = Query(default=10, ge=1, le=50, description="Number of species"),
):
    """Most recently added species to the lifelist (DESC by first detection)."""
    return db.firstseen(limit)


# ── Audio file upload ──────────────────────────────────────────────── #

@app.post("/api/upload")
async def upload_audio(
    file: UploadFile = File(..., description="Audio file (WAV, MP3, OGG, M4A, FLAC)"),
    confidence: Optional[float] = Form(
        default=None,
        ge=0.01,
        le=1.0,
        description="Confidence threshold (default: from env or 0.25)",
    ),
    latitude: Optional[float] = Form(
        default=None,
        ge=-90.0,
        le=90.0,
        description="Latitude for geo-filtering (default: from env or 55.75)",
    ),
    longitude: Optional[float] = Form(
        default=None,
        ge=-180.0,
        le=180.0,
        description="Longitude for geo-filtering (default: from env or 37.62)",
    ),
):
    """
    Upload an audio file for BirdNET analysis.

    The file is saved to a temporary location, analyzed with BirdNET,
    and detections are written to the database. Returns analysis results
    including top detections and segment count.
    """
    # Validate file extension
    allowed_ext = {".wav", ".mp3", ".ogg", ".m4a", ".flac"}
    _, ext = os.path.splitext(file.filename or "")
    if ext.lower() not in allowed_ext:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Allowed: {', '.join(sorted(allowed_ext))}",
        )

    # Resolve parameters: form values > env vars > defaults
    conf_threshold = confidence if confidence is not None else float(
        os.environ.get("AVIAN_CONFIDENCE", str(CONFIDENCE_THRESHOLD))
    )
    lat = latitude if latitude is not None else float(
        os.environ.get("AVIAN_LAT", str(LATITUDE))
    )
    lon = longitude if longitude is not None else float(
        os.environ.get("AVIAN_LON", str(LONGITUDE))
    )

    # Save uploaded file to a temp path
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext.lower())
    try:
        # Stream file to disk (avoid loading large files into memory)
        with os.fdopen(tmp_fd, "wb") as tmp_file:
            while True:
                chunk = await file.read(1024 * 1024)  # 1 MB chunks
                if not chunk:
                    break
                tmp_file.write(chunk)

        logger.info(
            "Analyzing uploaded file: %s (%.1f KB, threshold=%.2f, lat=%.2f, lon=%.2f)",
            file.filename,
            os.path.getsize(tmp_path) / 1024,
            conf_threshold,
            lat,
            lon,
        )

        # Run BirdNET analysis
        result = analyze_file(
            file_path=tmp_path,
            db=db,
            confidence_threshold=conf_threshold,
            latitude=lat,
            longitude=lon,
        )

        return {
            "filename": file.filename,
            "segments_analyzed": result["segments_analyzed"],
            "detections_written": result["detections_written"],
            "top_detections": result["top_detections"],
        }

    except Exception as e:
        logger.error("Failed to analyze uploaded file: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Analysis failed: {e}",
        )
    finally:
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass