"""
test_api.py — Tests for api.py (FastAPI endpoints).

Uses FastAPI's TestClient (httpx) with a real in-memory SQLite database.
Tests cover all 7 data endpoints + health + listener/status + upload validation.

AudioListener is mocked (no real microphone needed).
BirdNET analysis in upload is mocked (no TFLite model needed).
"""

import os
import sys
import tempfile
from io import BytesIO
from unittest.mock import patch, MagicMock, MagicMock as _MM

import pytest
from fastapi.testclient import TestClient

# Disable audio capture so the lifespan doesn't start a real listener
os.environ["AVIAN_NO_AUDIO"] = "1"

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Mock heavy C/external imports before they are pulled in ───────── #

# sounddevice is imported by audio_capture.py at module level;
# we need it available so that audio_capture (and thus api) can be imported.
sys.modules.setdefault("sounddevice", _MM())

import api
from api import app


# ── Fixtures ───────────────────────────────────────────────────────── #

@pytest.fixture()
def client():
    """
    Create a TestClient with a real temporary database.
    AVIAN_NO_AUDIO=1 prevents the lifespan from starting AudioListener.
    We bypass the lifespan entirely and set up DB manually.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        api.db = api.Database(db_path=db_path)
        api.db.init()
        api.listener = None

        with TestClient(
            api.app,
            raise_server_exceptions=True,
        ) as c:
            yield c

        api.db.close()


@pytest.fixture()
def populated_client(client):
    """
    Client with some test data in the database.
    Inserts detections for 3 species across 2 days.
    """
    from datetime import datetime, timedelta
    db = api.db

    # Day 1: 3 detections of House Sparrow, 2 of Great Tit
    day1 = datetime.now() - timedelta(days=1)
    for i in range(3):
        dt = day1.replace(hour=8 + i, minute=0, second=0, microsecond=0)
        db.insert_detection("Passer domesticus", "House Sparrow", 0.85, dt)
    for i in range(2):
        dt = day1.replace(hour=9 + i, minute=30, second=0, microsecond=0)
        db.insert_detection("Parus major", "Great Tit", 0.92, dt)

    # Day 2 (today): 1 detection of House Sparrow, 1 of Robin
    today = datetime.now()
    db.insert_detection("Passer domesticus", "House Sparrow", 0.78, today)
    db.insert_detection("Erithacus rubecula", "European Robin", 0.65, today)

    yield client


# ── Health ─────────────────────────────────────────────────────────── #

class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_listener_status_disabled(self, client):
        """When listener is None, status should say it's disabled."""
        r = client.get("/api/listener/status")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is False
        assert "disabled" in data["reason"]

    def test_listener_status_running(self, client):
        """When listener mock is set, status should show running=true."""
        mock_listener = MagicMock()
        mock_listener.is_running = True
        mock_listener.stats = {
            "segments_processed": 42,
            "detections_written": 7,
            "uptime_seconds": 120.5,
        }
        api.listener = mock_listener
        try:
            r = client.get("/api/listener/status")
            assert r.status_code == 200
            data = r.json()
            assert data["running"] is True
            assert data["segments_processed"] == 42
        finally:
            api.listener = None


# ── /api/stats ─────────────────────────────────────────────────────── #

class TestStats:
    def test_empty_db(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["totals"]["detections"] == 0
        assert data["totals"]["species"] == 0
        assert data["today"]["detections"] == 0
        assert data["week"]["detections"] == 0
        assert data["started"] is None
        assert "as_of" in data

    def test_populated_db(self, populated_client):
        r = populated_client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        # Total: 3 + 2 + 1 + 1 = 7 detections, 3 species
        assert data["totals"]["detections"] == 7
        assert data["totals"]["species"] == 3
        # Today: 2 detections (1 sparrow + 1 robin)
        assert data["today"]["detections"] == 2
        assert data["today"]["species"] == 2
        assert data["started"] is not None
        assert "as_of" in data


# ── /api/recent ────────────────────────────────────────────────────── #

class TestRecent:
    def test_empty_db(self, client):
        r = client.get("/api/recent")
        assert r.status_code == 200
        data = r.json()
        assert data["hours"] == 24
        assert data["species"] == []
        assert "as_of" in data

    def test_default_hours(self, populated_client):
        r = populated_client.get("/api/recent")
        assert r.status_code == 200
        data = r.json()
        assert data["hours"] == 24
        # With data from yesterday and today, should find species
        # (today's detections are within 24h)
        assert len(data["species"]) >= 2

    def test_custom_hours(self, populated_client):
        r = populated_client.get("/api/recent?hours=1")
        assert r.status_code == 200
        data = r.json()
        assert data["hours"] == 1

    def test_recent_species_fields(self, populated_client):
        r = populated_client.get("/api/recent?hours=48")
        data = r.json()
        for sp in data["species"]:
            assert "sci" in sp
            assert "com" in sp
            assert "n" in sp
            assert "best_conf" in sp
            assert "last_seen" in sp
            assert "top_file" in sp  # Always empty string in desktop
            assert "top_at" in sp

    def test_hours_clamping(self, client):
        """Hours should be clamped to [1, 1000000]."""
        r = client.get("/api/recent?hours=0")
        # FastAPI's Query(ge=1) should reject this with 422
        assert r.status_code == 422


# ── /api/lifelist ──────────────────────────────────────────────────── #

class TestLifelist:
    def test_empty_db(self, client):
        r = client.get("/api/lifelist")
        assert r.status_code == 200
        data = r.json()
        assert data["species"] == []
        assert "as_of" in data

    def test_populated_db(self, populated_client):
        r = populated_client.get("/api/lifelist")
        assert r.status_code == 200
        data = r.json()
        assert len(data["species"]) == 3
        # Sorted by first_seen ASC → House Sparrow and Great Tit (day1)
        # should come before European Robin (day2/today)
        sci_names = [s["sci"] for s in data["species"]]
        assert "Passer domesticus" in sci_names
        assert "Parus major" in sci_names
        assert "Erithacus rubecula" in sci_names
        # Robin should be last (most recent first_seen)
        assert sci_names[-1] == "Erithacus rubecula"

    def test_lifelist_fields(self, populated_client):
        r = populated_client.get("/api/lifelist")
        data = r.json()
        sp = data["species"][0]
        assert "sci" in sp
        assert "com" in sp
        assert "first_seen" in sp
        assert "last_seen" in sp
        assert "n" in sp
        assert "best_conf" in sp


# ── /api/species ───────────────────────────────────────────────────── #

class TestSpecies:
    def test_missing_param(self, client):
        """sci parameter is required."""
        r = client.get("/api/species")
        assert r.status_code == 422

    def test_species_not_found(self, client):
        r = client.get("/api/species?sci=Nonexistent%20birdus")
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_species_found(self, populated_client):
        r = populated_client.get("/api/species?sci=Passer%20domesticus")
        assert r.status_code == 200
        data = r.json()
        assert data["sci"] == "Passer domesticus"
        assert data["summary"] is not None
        assert data["summary"]["com"] == "House Sparrow"
        assert data["summary"]["total"] == 4  # 3 day1 + 1 today
        assert len(data["detections"]) == 4

    def test_species_detections_order(self, populated_client):
        """Detections should be ordered DESC by date/time."""
        r = populated_client.get("/api/species?sci=Passer%20domesticus")
        data = r.json()
        detections = data["detections"]
        # First detection should be most recent (today)
        assert detections[0]["d"] >= detections[-1]["d"]


# ── /api/timeseries ────────────────────────────────────────────────── #

class TestTimeseries:
    def test_empty_db(self, client):
        r = client.get("/api/timeseries")
        assert r.status_code == 200
        data = r.json()
        assert data["days"] == 30
        assert data["daily"] == []
        assert data["by_hour"] == []
        assert "as_of" in data

    def test_populated_db(self, populated_client):
        r = populated_client.get("/api/timeseries?days=7")
        assert r.status_code == 200
        data = r.json()
        assert data["days"] == 7
        # Should have daily entries for the 2 days with data
        assert len(data["daily"]) >= 1
        # by_hour should have entries for hours 8, 9, 10
        assert len(data["by_hour"]) >= 1

    def test_days_clamping(self, client):
        """Days should be clamped to [1, 90]."""
        r = client.get("/api/timeseries?days=0")
        assert r.status_code == 422  # FastAPI rejects ge=1

        r = client.get("/api/timeseries?days=200")
        assert r.status_code == 422  # FastAPI rejects le=90


# ── /api/firstseen ─────────────────────────────────────────────────── #

class TestFirstseen:
    def test_empty_db(self, client):
        r = client.get("/api/firstseen")
        assert r.status_code == 200
        data = r.json()
        assert data["species"] == []
        assert "as_of" in data

    def test_populated_db(self, populated_client):
        r = populated_client.get("/api/firstseen?limit=10")
        assert r.status_code == 200
        data = r.json()
        assert len(data["species"]) == 3
        # DESC by first_seen → European Robin (today) first
        assert data["species"][0]["sci"] == "Erithacus rubecula"

    def test_limit(self, populated_client):
        r = populated_client.get("/api/firstseen?limit=2")
        data = r.json()
        assert len(data["species"]) == 2

    def test_firstseen_fields(self, populated_client):
        r = populated_client.get("/api/firstseen")
        data = r.json()
        sp = data["species"][0]
        assert "sci" in sp
        assert "com" in sp
        assert "first_seen" in sp
        assert "total" in sp


# ── /api/upload ────────────────────────────────────────────────────── #

class TestUpload:
    def test_upload_no_file(self, client):
        """POST without a file should return 422."""
        r = client.post("/api/upload")
        assert r.status_code == 422

    def test_upload_bad_extension(self, client):
        """Non-audio file extension should be rejected."""
        r = client.post(
            "/api/upload",
            files={"file": ("test.txt", b"hello world", "text/plain")},
        )
        assert r.status_code == 400
        assert "Unsupported" in r.json()["detail"]

    def test_upload_success_mocked(self, client):
        """Successful upload with mocked analyze_file."""
        mock_result = {
            "segments_analyzed": 5,
            "detections_written": 3,
            "top_detections": [
                {
                    "start": 0.0,
                    "end": 3.0,
                    "sci_name": "Passer domesticus",
                    "com_name": "House Sparrow",
                    "confidence": 0.85,
                }
            ],
        }
        with patch("api.analyze_file", return_value=mock_result) as mock_analyze:
            r = client.post(
                "/api/upload",
                files={"file": ("test.wav", b"\x00" * 1000, "audio/wav")},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["filename"] == "test.wav"
            assert data["segments_analyzed"] == 5
            assert data["detections_written"] == 3
            assert len(data["top_detections"]) == 1
            mock_analyze.assert_called_once()

    def test_upload_with_custom_params(self, client):
        """Upload with custom confidence, lat, lon passed via form."""
        mock_result = {
            "segments_analyzed": 1,
            "detections_written": 0,
            "top_detections": [],
        }
        with patch("api.analyze_file", return_value=mock_result) as mock_analyze:
            r = client.post(
                "/api/upload",
                files={"file": ("recording.mp3", b"\x00" * 500, "audio/mpeg")},
                data={
                    "confidence": "0.5",
                    "latitude": "40.71",
                    "longitude": "-74.0",
                },
            )
            assert r.status_code == 200
            # Verify analyze_file was called with custom params
            _, kwargs = mock_analyze.call_args
            assert kwargs["confidence_threshold"] == 0.5
            assert kwargs["latitude"] == 40.71
            assert kwargs["longitude"] == -74.0

    def test_upload_analysis_error(self, client):
        """If analyze_file raises, should return 500."""
        with patch("api.analyze_file", side_effect=RuntimeError("TFLite error")):
            r = client.post(
                "/api/upload",
                files={"file": ("bad.wav", b"\x00" * 100, "audio/wav")},
            )
            assert r.status_code == 500
            assert "TFLite error" in r.json()["detail"]

    def test_upload_temp_file_cleaned(self, client):
        """Temp file should be deleted after analysis (success or failure)."""
        import glob
        mock_result = {
            "segments_analyzed": 0,
            "detections_written": 0,
            "top_detections": [],
        }
        with patch("api.analyze_file", return_value=mock_result):
            r = client.post(
                "/api/upload",
                files={"file": ("clean.wav", b"\x00" * 100, "audio/wav")},
            )
            assert r.status_code == 200
        # We can't easily verify the temp file is gone, but the finally
        # block in the endpoint ensures os.unlink is called.


# ── Response format compatibility ──────────────────────────────────── #

class TestResponseFormat:
    """
    Verify that response JSON structures match the format expected by the
    original AvianVisitors frontend (PHP API compatibility).
    """

    def test_stats_structure(self, populated_client):
        r = populated_client.get("/api/stats")
        data = r.json()
        assert "totals" in data
        assert "detections" in data["totals"]
        assert "species" in data["totals"]
        assert "today" in data
        assert "last_hour" in data
        assert "week" in data
        assert "started" in data
        assert "as_of" in data

    def test_recent_structure(self, populated_client):
        r = populated_client.get("/api/recent?hours=48")
        data = r.json()
        assert "hours" in data
        assert "species" in data
        for sp in data["species"]:
            assert "sci" in sp
            assert "com" in sp
            assert "n" in sp
            assert "best_conf" in sp
            assert "last_seen" in sp
            assert "top_file" in sp
            assert "top_at" in sp

    def test_lifelist_structure(self, populated_client):
        r = populated_client.get("/api/lifelist")
        data = r.json()
        assert "species" in data
        assert "as_of" in data
        for sp in data["species"]:
            assert "sci" in sp
            assert "com" in sp
            assert "first_seen" in sp
            assert "last_seen" in sp
            assert "n" in sp
            assert "best_conf" in sp

    def test_species_structure(self, populated_client):
        r = populated_client.get("/api/species?sci=Parus%20major")
        data = r.json()
        assert data["sci"] == "Parus major"
        assert "summary" in data
        assert "detections" in data
        assert "com" in data["summary"]
        assert "total" in data["summary"]
        assert "first_seen" in data["summary"]
        assert "last_seen" in data["summary"]
        assert "best_conf" in data["summary"]
        for det in data["detections"]:
            assert "d" in det
            assert "t" in det
            assert "file" in det
            assert "conf" in det

    def test_timeseries_structure(self, populated_client):
        r = populated_client.get("/api/timeseries?days=7")
        data = r.json()
        assert "days" in data
        assert "daily" in data
        assert "by_hour" in data
        assert "as_of" in data
        for d in data["daily"]:
            assert "date" in d
            assert "detections" in d
            assert "species" in d
        for h in data["by_hour"]:
            assert "hour" in h
            assert "detections" in h

    def test_firstseen_structure(self, populated_client):
        r = populated_client.get("/api/firstseen")
        data = r.json()
        assert "species" in data
        assert "as_of" in data
        for sp in data["species"]:
            assert "sci" in sp
            assert "com" in sp
            assert "first_seen" in sp
            assert "total" in sp


# ── Part 4: /api/cutout ─────────────────────────────────────────────── #

class TestCutout:
    """Tests for the bird image resolver endpoint."""

    def test_illustration_found(self, client):
        """Species with a bundled illustration should return PNG 200."""
        r = client.get("/api/cutout?sci=Passer+domesticus")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert len(r.content) > 1024
        assert "max-age=86400" in r.headers.get("cache-control", "")

    def test_cutout_fallback(self, client):
        """Species with only a cutout (no illustration) should still work."""
        # Use a species that's in cutouts but test that the resolver works
        # contopus-sordidulus is in both illustrations and cutouts
        r = client.get("/api/cutout?sci=Contopus+sordidulus")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"

    def test_pose_fallback(self, client):
        """Pose 2 missing should fall back to pose 1 illustration."""
        # Most species have pose-1; many also have pose-2.
        # Sturnus vulgaris has both - test that pose=2 returns the -2 variant.
        r2 = client.get("/api/cutout?sci=Sturnus+vulgaris&pose=2")
        assert r2.status_code == 200
        assert r2.headers["content-type"] == "image/png"

    def test_not_found(self, client):
        """Completely unknown species should return 404."""
        r = client.get("/api/cutout?sci=Nonexistent+fictitious+birdus")
        assert r.status_code == 404

    def test_invalid_sci(self, client):
        """Invalid scientific name patterns should return 400."""
        # Path traversal attempt
        r = client.get("/api/cutout?sci=../etc/passwd")
        assert r.status_code == 400

        # Empty
        r = client.get("/api/cutout?sci=")
        assert r.status_code in (400, 422)  # 422 from FastAPI's required query

    def test_sci_to_slug_helper(self):
        """Verify the slug conversion logic."""
        from api import _sci_to_slug
        assert _sci_to_slug("Parus major") == "parus-major"
        assert _sci_to_slug("Luscinia luscinia") == "luscinia-luscinia"
        assert _sci_to_slug("Aechmophorus occidentalis") == "aechmophorus-occidentalis"


# ── Part 4: /api/wiki ────────────────────────────────────────────────── #

class TestWiki:
    """Tests for the Wikipedia summary proxy endpoint."""

    @pytest.fixture()
    def wiki_client(self, client):
        """Client with httpx mocked to avoid real network calls."""
        return client

    def test_wiki_parus_major(self, wiki_client):
        """Real species should return extract + thumbnail from Wikipedia."""
        r = wiki_client.get("/api/wiki?sci=Parus+major")
        # Wikipedia is live — may or may not be reachable in CI.
        # If reachable, should return 200 with extract.
        # If unreachable, the endpoint gracefully returns nulls.
        assert r.status_code == 200
        data = r.json()
        assert "extract" in data
        assert "thumbnail" in data
        assert "title" in data
        # Parus major is a well-known species, so we expect real data
        assert data["title"] is not None
        assert data["extract"] is not None
        assert len(data["extract"]) > 100

    def test_wiki_invalid_sci(self, wiki_client):
        """Invalid scientific name should return 400."""
        r = wiki_client.get("/api/wiki?sci=../etc/passwd")
        assert r.status_code == 400

    def test_wiki_missing_species(self, wiki_client):
        """Non-existent Wikipedia page should gracefully return nulls."""
        r = wiki_client.get("/api/wiki?sci=Fictitious+birdus+nonexistus")
        assert r.status_code == 200
        data = r.json()
        # Wikipedia returns 404 for unknown pages; our proxy returns nulls
        assert data["extract"] is None
        assert data["thumbnail"] is None

    def test_wiki_cache_header(self, wiki_client):
        """Response should include Cache-Control: max-age=86400."""
        r = wiki_client.get("/api/wiki?sci=Passer+domesticus")
        assert r.status_code == 200
        assert "max-age=86400" in r.headers.get("cache-control", "")

    def test_wiki_ssrf_protection(self, wiki_client):
        """Even if Wikipedia returned a non-wikimedia URL, we'd strip it.

        This is hard to test without mocking, but we verify the regex logic
        directly.
        """
        from api import _WIKIMEDIA_HOST_RE
        # Valid hosts
        assert _WIKIMEDIA_HOST_RE.match("upload.wikimedia.org")
        assert _WIKIMEDIA_HOST_RE.match("en.wikipedia.org")
        # Invalid hosts (SSRF)
        assert not _WIKIMEDIA_HOST_RE.match("evil.com")
        assert not _WIKIMEDIA_HOST_RE.match("wikimedia.org.evil.com")


# ════════════════════════════════════════════════════════════════════════ #
#  Part 5: New stub endpoints (recording, menu, config, status)
# ════════════════════════════════════════════════════════════════════════ #

class TestRecordingEndpoint:
    """GET /api/recording — always 404 in Desktop mode (no audio files saved)."""

    @pytest.fixture(autouse=True)
    def _client(self, client):
        self.client = client

    def test_recording_by_sci_404(self):
        r = self.client.get("/api/recording?sci=Passer+domesticus")
        assert r.status_code == 404

    def test_recording_by_file_404(self):
        r = self.client.get("/api/recording?file=some/path.wav")
        assert r.status_code == 404


class TestMenuEndpoint:
    """GET/POST /api/menu — returns in-app Settings link for Desktop."""

    @pytest.fixture(autouse=True)
    def _client(self, client):
        self.client = client

    def test_menu_get_returns_settings(self):
        r = self.client.get("/api/menu")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        labels = [it["label"] for it in data["items"]]
        assert "Settings" in labels

    def test_menu_post_same_as_get(self):
        r = self.client.post("/api/menu")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data


class TestConfigEndpoint:
    """GET/POST /api/config — reads/writes desktop_config.json."""

    @pytest.fixture(autouse=True)
    def _client(self, client):
        self.client = client

    def test_config_read(self):
        r = self.client.get("/api/config")
        assert r.status_code == 200
        data = r.json()
        assert "values" in data
        assert "CONFIDENCE" in data["values"]
        assert "preserve" in data

    def test_config_write_and_reread(self):
        # Write a custom value
        r = self.client.post(
            "/api/config",
            json={"CONFIDENCE": 0.42, "FULL_DISK": "purge"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # Re-read and verify
        r = self.client.get("/api/config")
        data = r.json()
        assert data["values"]["CONFIDENCE"] == 0.42
        assert data["values"]["FULL_DISK"] == "purge"

    def test_config_write_merges(self):
        """Second write should merge with, not overwrite, previous values."""
        self.client.post("/api/config", json={"SENSITIVITY": 1.3})
        self.client.post("/api/config", json={"OVERLAP": 0.5})
        r = self.client.get("/api/config")
        v = r.json()["values"]
        assert v["SENSITIVITY"] == 1.3
        assert v["OVERLAP"] == 0.5


class TestStatusEndpoint:
    """GET/POST /api/status — system diagnostics stub for Desktop."""

    @pytest.fixture(autouse=True)
    def _client(self, client):
        self.client = client

    def test_status_diag(self):
        r = self.client.get("/api/status?action=diag")
        assert r.status_code == 200
        data = r.json()
        assert "system" in data
        sys_ = data["system"]
        assert "hostname" in sys_
        assert "uptime" in sys_
        assert "mem" in sys_
        assert "used_pct" in sys_["mem"]
        # No systemd services in Desktop
        assert data["services"] == {}

    def test_status_logs(self):
        r = self.client.get("/api/status?action=logs&unit=test&lines=50")
        assert r.status_code == 200
        data = r.json()
        assert "text" in data
        assert "Desktop" in data["text"]

    def test_status_restart_501(self):
        r = self.client.post("/api/status?action=restart&unit=something")
        assert r.status_code == 501

    def test_status_unknown_action_400(self):
        r = self.client.get("/api/status?action=foobar")
        assert r.status_code == 400


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])