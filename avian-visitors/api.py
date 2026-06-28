"""
api.py — FastAPI endpoints for AvianVisitors Desktop.

Exposes JSON endpoints that map 1:1 to database.py methods,
plus an audio file upload endpoint that delegates to audio_capture.analyze_file(),
static file serving, bird image resolver (/api/cutout), and Wikipedia proxy (/api/wiki).

Endpoints:
  GET  /api/stats              → Database.stats()
  GET  /api/recent             → Database.recent(hours)
  GET  /api/lifelist           → Database.lifelist()
  GET  /api/species            → Database.species_detail(sci)
  GET  /api/timeseries         → Database.timeseries(days)
  GET  /api/firstseen          → Database.firstseen(limit)
  POST /api/upload             → analyze_file() from audio_capture.py
  GET  /api/listener/status    → AudioListener diagnostics
  GET  /api/cutout             → Bird image resolver (illustrations → cutouts)
  GET  /api/wiki               → Wikipedia summary proxy
  GET  /favicon.png            → Favicon
  GET  /{path}                 → Static files (frontend/)

The Database and (optional) AudioListener instances are created at module level
and initialised via the lifespan() context manager, so that every endpoint
shares the same objects.
"""

import logging
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from database import Database
from audio_capture import AudioListener, analyze_file, LATITUDE, LONGITUDE, CONFIDENCE_THRESHOLD

logger = logging.getLogger("avian.api")

# ── Asset directories (resolved relative to this file) ──────────────── #

_HERE = Path(__file__).resolve().parent
_ASSETS_DIR = _HERE / "assets"
_ILLUSTRATIONS_DIR = _ASSETS_DIR / "illustrations"
_CUTOUTS_DIR = _ASSETS_DIR / "cutouts"
_FAVICON = _ASSETS_DIR / "favicon.png"

# Regex for validating scientific names (binomial or trinomial)
_SCI_RE = re.compile(r'^[A-Za-z]{2,40}(?: [a-z]{2,40}){1,3}$')

# Wikipedia proxy settings
_WIKIPEDIA_UA = os.environ.get(
    "AV_USER_AGENT",
    "AvianVisitors/1.0 (+https://github.com/Twarner491/AvianVisitors)",
)
_WIKIMEDIA_HOST_RE = re.compile(
    r'^(?:[^.]+\.)?(?:wikimedia\.org|wikipedia\.org)$', re.IGNORECASE
)

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
        conf = float(os.environ.get("AVIAN_CONFIDENCE", str(CONFIDENCE_THRESHOLD)))
        sens = float(os.environ.get("AVIAN_SENSITIVITY", "1.0"))
        # Parse optional audio device index
        device_val = os.environ.get("AVIAN_AUDIO_DEVICE", "")
        audio_device = int(device_val) if device_val else None

        listener = AudioListener(
            db, latitude=lat, longitude=lon,
            confidence_threshold=conf, sensitivity=sens,
            device=audio_device,
        )
        listener.start()
        logger.info(
            "Audio listener started (lat=%.2f, lon=%.2f, conf=%.2f, sens=%.1f, device=%s)",
            lat, lon, conf, sens, audio_device,
        )

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


# ── Bird image resolver (/api/cutout) ──────────────────────────────── #

def _sci_to_slug(sci: str) -> str:
    """Convert scientific name to filesystem slug.

    Example: "Parus major" → "parus-major"
    """
    slug = re.sub(r'[^a-z0-9]+', '-', sci.lower())
    return slug.strip('-')


def _find_image(sci: str, pose: int = 1) -> Optional[Path]:
    """Resolve bird image through the lookup chain.

    1. illustrations/<slug>-<pose>.png (pose-specific kachō-e)
    2. illustrations/<slug>.png       (default pose kachō-e)
    3. cutouts/<slug>.png            (background-removed photo)

    Returns the first existing file > 1 KB, or None.
    """
    slug = _sci_to_slug(sci)

    # Pose-specific illustration (e.g. -2 for flight)
    if pose != 1:
        pose_path = _ILLUSTRATIONS_DIR / f"{slug}-{pose}.png"
        if pose_path.is_file() and pose_path.stat().st_size > 1024:
            return pose_path

    # Default illustration (pose 1)
    default_path = _ILLUSTRATIONS_DIR / f"{slug}.png"
    if default_path.is_file() and default_path.stat().st_size > 1024:
        return default_path

    # Cutout fallback (background-removed photo)
    cutout_path = _CUTOUTS_DIR / f"{slug}.png"
    if cutout_path.is_file() and cutout_path.stat().st_size > 1024:
        return cutout_path

    return None


@app.get("/api/cutout")
async def cutout(
    sci: str = Query(..., description="Scientific name, e.g. 'Parus major'"),
    pose: int = Query(default=1, ge=1, le=99, description="Pose variant (1=perched, 2+=flight)"),
):
    """Resolve a bird image for the given species.

    Lookup chain (mirrors the original PHP cutout.php):
      1. Bundled kachō-e illustration with pose suffix
      2. Bundled kachō-e illustration default pose (fallback for missing pose variant)
      3. Bundled background-removed photo (cutout)
      4. 404 if nothing found

    Response is a PNG with Cache-Control: public, max-age=86400 (24h).
    """
    if not _SCI_RE.match(sci):
        raise HTTPException(status_code=400, detail="invalid sci")

    image_path = _find_image(sci, pose)
    if image_path is None:
        raise HTTPException(status_code=404, detail=f"no illustration for {sci}")

    return FileResponse(
        image_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Wikipedia summary proxy (/api/wiki) ────────────────────────────── #

@app.get("/api/wiki")
async def wiki(
    sci: str = Query(..., description="Scientific name, e.g. 'Parus major'"),
):
    """Proxy for Wikipedia REST API summary.

    Returns {extract, thumbnail, title} for the species article.
    The thumbnail URL is validated to only allow wikimedia.org / wikipedia.org hosts
    (SSRF protection, same as the original PHP wiki.php).
    Cached by the browser for 24 hours.
    """
    if not _SCI_RE.match(sci):
        raise HTTPException(status_code=400, detail="invalid sci")

    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{sci}"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers={"User-Agent": _WIKIPEDIA_UA})
            if resp.status_code != 200:
                return JSONResponse(
                    content={"extract": None, "thumbnail": None, "title": None},
                    headers={"Cache-Control": "public, max-age=86400"},
                )
            data = resp.json()
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.warning("Wikipedia fetch failed for %s: %s", sci, e)
        return JSONResponse(
            content={"extract": None, "thumbnail": None, "title": None},
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # Extract and validate thumbnail URL (SSRF protection)
    thumbnail = None
    raw_thumb = data.get("thumbnail", {}).get("source")
    if raw_thumb:
        host = urlparse(raw_thumb).hostname or ""
        if _WIKIMEDIA_HOST_RE.match(host):
            thumbnail = {"source": raw_thumb}

    return JSONResponse(
        content={
            "extract": data.get("extract"),
            "thumbnail": thumbnail,
            "title": data.get("title"),
        },
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Recording endpoint (audio files) ────────────────────────────────── #
# Desktop version: audio files are not saved to disk (File_Name is ''),
# so this endpoint always returns 404. The frontend handles this gracefully
# (shows "no audio" on cards, "No recordings yet." in modal).

@app.get("/api/recording")
async def recording(
    sci: Optional[str] = Query(default=None, description="Scientific name"),
    file: Optional[str] = Query(default=None, description="Audio file path"),
):
    """Serve an audio recording file.

    In the Desktop edition, recordings are not persisted to disk,
    so this always returns 404. Kept for frontend compatibility —
    the JS already handles 404 gracefully (shows 'no audio').
    """
    raise HTTPException(status_code=404, detail="recordings not available in desktop mode")


# ── Menu endpoint (auth + nav items) ────────────────────────────────── #
# Desktop version: no auth required, no external admin pages.

@app.get("/api/menu")
@app.post("/api/menu")
async def menu():
    """Return navigation menu items.

    In the original BirdNET-Pi this handles Caddy basic-auth and returns
    a list of {label, href, native} items for the drawer.  The Desktop
    edition has no external admin pages, so we return only the in-app
    Settings link (which uses the config endpoint below).
    """
    return {
        "items": [
            {"label": "Settings", "href": "#admin=settings", "native": True},
        ],
    }


# ── Config endpoint (settings read/write) ──────────────────────────── #
# Desktop version: settings are read from environment variables and
# written to a small JSON sidecar file.

_CONFIG_FILE = _HERE / "data" / "desktop_config.json"


def _read_config() -> dict:
    """Merge environment-variable defaults with persisted overrides."""
    values = {
        "CONFIDENCE": float(os.environ.get("AVIAN_CONFIDENCE", str(CONFIDENCE_THRESHOLD))),
        "SENSITIVITY": 1.0,
        "OVERLAP": 0.0,
        "FULL_DISK": "keep",
        "PRESERVE": False,
    }
    if _CONFIG_FILE.is_file():
        try:
            import json
            saved = json.loads(_CONFIG_FILE.read_text())
            values.update(saved)
        except Exception:
            pass
    return values


@app.get("/api/config")
async def config_read():
    """Return current configuration values."""
    return {"values": _read_config(), "preserve": False}


@app.post("/api/config")
async def config_write(body: dict):
    """Persist configuration changes to the JSON sidecar."""
    import json
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Merge: only update keys that were sent
    existing = {}
    if _CONFIG_FILE.is_file():
        try:
            existing = json.loads(_CONFIG_FILE.read_text())
        except Exception:
            pass
    existing.update(body)
    _CONFIG_FILE.write_text(json.dumps(existing, indent=2))
    return {"ok": True}


# ── Status endpoint (system diagnostics) ────────────────────────────── #
# Desktop version: no systemd, no Icecast. Returns basic info.

@app.get("/api/status")
async def status_get(
    action: str = Query(..., description="Action: diag, logs"),
    unit: Optional[str] = Query(default=None),
    lines: int = Query(default=120, ge=1, le=500),
):
    """System diagnostics stub for Desktop.

    action=diag: returns basic system info (no systemd services).
    action=logs: returns a message that logs are not available.
    """
    import platform
    import psutil

    if action == "diag":
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(str(_HERE))
        uptime_s = int(psutil.boot_time() and (time.time() - psutil.boot_time()))
        return {
            "system": {
                "hostname": platform.node(),
                "kernel": platform.release(),
                "uptime": {"pretty": _fmt_seconds(uptime_s), "seconds": uptime_s},
                "mem": {
                    "used_bytes": mem.used,
                    "total_bytes": mem.total,
                    "used_pct": round(mem.percent, 1),
                },
                "temp_c": None,  # not available cross-platform
            },
            "services": {},    # no systemd in Desktop
            "recent_logs": {},
        }
    elif action == "logs":
        return {
            "text": "Log viewing is not available in Desktop mode.\n"
                    "Check the terminal where AvianVisitors is running.",
        }
    else:
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")


@app.post("/api/status")
async def status_post(
    action: str = Query(..., description="Action: restart"),
    unit: str = Query(..., description="Service unit name"),
):
    """Service restart stub — not applicable for Desktop."""
    raise HTTPException(
        status_code=501,
        detail=f"Service restart not available in Desktop mode (unit: {unit})",
    )


def _fmt_seconds(s: int) -> str:
    """Format seconds into a human-readable uptime string."""
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    d = s // 86400
    h = (s % 86400) // 3600
    return f"{d}d {h}h"


# ── Static files & favicon (must be AFTER all /api/* routes) ────────── #

@app.get("/favicon.png", include_in_schema=False)
async def favicon():
    if _FAVICON.is_file():
        return FileResponse(_FAVICON, media_type="image/png")
    raise HTTPException(status_code=404)

# Frontend static files — served from frontend/ directory
_frontend_dir = _HERE / "frontend"
if _frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")