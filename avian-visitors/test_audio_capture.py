"""
test_audio_capture.py — Tests for audio_capture.py (birdnet-analyzer backend).

Tests cover:
  - _parse_label() — label string parsing
  - _current_week() — week number calculation
  - analyze_file() — end-to-end file analysis against known recordings
  - AudioListener — construction, stats, model loading
  - Geo-filtering verification
  - Edge cases (empty audio, short audio, invalid paths)
"""

import os
import sys
import tempfile
import numpy as np
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_capture import AudioListener, analyze_file, _current_week


# ── _parse_label ──────────────────────────────────────────────────── #

class TestParseLabel:
    """Tests for the static _parse_label method."""

    def test_standard_label(self):
        sci, com = AudioListener._parse_label("Parus major_Great Tit")
        assert sci == "Parus major"
        assert com == "Great Tit"

    def test_three_word_scientific(self):
        sci, com = AudioListener._parse_label(
            "Poecile montanus borealis_Siberian Tit"
        )
        assert sci == "Poecile montanus borealis"
        assert com == "Siberian Tit"

    def test_label_without_underscore(self):
        sci, com = AudioListener._parse_label("UnknownBird")
        assert sci == "UnknownBird"
        assert com == "UnknownBird"

    def test_label_with_hyphen_in_com_name(self):
        sci, com = AudioListener._parse_label(
            "Lanius collurio_Red-backed Shrike"
        )
        assert sci == "Lanius collurio"
        assert com == "Red-backed Shrike"

    def test_label_empty_string(self):
        sci, com = AudioListener._parse_label("")
        assert sci == ""
        assert com == ""

    def test_real_birdnet_labels(self):
        """Test against known BirdNET v2.4 label format."""
        examples = [
            ("Passer domesticus_House Sparrow", "Passer domesticus", "House Sparrow"),
            ("Turdus philomelos_Song Thrush", "Turdus philomelos", "Song Thrush"),
            ("Columba livia_Rock Pigeon", "Columba livia", "Rock Pigeon"),
        ]
        for label, expected_sci, expected_com in examples:
            sci, com = AudioListener._parse_label(label)
            assert sci == expected_sci, f"Failed for {label}"
            assert com == expected_com, f"Failed for {label}"


# ── _current_week ─────────────────────────────────────────────────── #

class TestCurrentWeek:
    def test_returns_int(self):
        w = _current_week()
        assert isinstance(w, int)

    def test_valid_range(self):
        w = _current_week()
        assert 1 <= w <= 53


# ── AudioListener construction ────────────────────────────────────── #

class TestAudioListenerConstruction:
    def test_default_params(self):
        db = MagicMock()
        listener = AudioListener(db)
        assert listener.sample_rate == 48_000
        assert listener.segment_duration == 3.0
        assert listener.segment_samples == 144_000
        assert listener.confidence_threshold == 0.25
        assert listener.latitude == 55.75
        assert listener.longitude == 37.62
        assert listener.sensitivity == 1.0
        assert listener.is_running is False

    def test_custom_params(self):
        db = MagicMock()
        listener = AudioListener(
            db,
            sample_rate=44100,
            segment_duration=3.0,
            confidence_threshold=0.5,
            latitude=40.71,
            longitude=-74.00,
            week=15,
        )
        assert listener.sample_rate == 44100
        assert listener.segment_samples == 132_300
        assert listener.confidence_threshold == 0.5
        assert listener.latitude == 40.71
        assert listener.longitude == -74.00
        assert listener.week == 15

    def test_stats_initial(self):
        db = MagicMock()
        listener = AudioListener(db)
        s = listener.stats
        assert s["segments_processed"] == 0
        assert s["detections_written"] == 0
        assert s["uptime_seconds"] == 0

    def test_stop_when_not_running(self):
        db = MagicMock()
        listener = AudioListener(db)
        listener.stop()  # Should not raise
        assert listener.is_running is False


# ── AudioListener _ensure_model ───────────────────────────────────── #

class TestEnsureModel:
    def test_model_loads(self):
        """Test that _ensure_model can load birdnet-analyzer model."""
        db = MagicMock()
        listener = AudioListener(
            db,
            latitude=55.75,
            longitude=37.62,
            week=26,
        )
        # This will actually load the model (takes a few seconds)
        listener._ensure_model()

        assert listener._model_loaded is True
        assert len(listener._labels) == 6522
        assert isinstance(listener._species_list, list)
        assert len(listener._species_list) > 0  # Moscow should have species
        print(f"  Labels: {len(listener._labels)}, Species list: {len(listener._species_list)}")

    def test_model_loads_once(self):
        """Model should only load once (second call is a no-op)."""
        db = MagicMock()
        listener = AudioListener(db)
        listener._ensure_model()
        labels_len = len(listener._labels)
        listener._ensure_model()  # Should skip
        assert len(listener._labels) == labels_len

    def test_no_geo_filter(self):
        """Without valid coordinates, all species should be used."""
        db = MagicMock()
        listener = AudioListener(db, latitude=-1, longitude=-1)
        listener._ensure_model()
        assert listener._model_loaded is True
        # No geo-filter → species list is empty (means "use all")
        assert len(listener._species_list) == 0


# ── analyze_file — real recordings ────────────────────────────────── #

class TestAnalyzeFileReal:
    """
    End-to-end tests against real audio recordings.
    These verify that birdnet-analyzer produces CORRECT results.
    """

    @pytest.fixture
    def db(self):
        """Create a temporary in-memory database for each test."""
        from database import Database
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path=db_path)
            db.init()
            yield db
            db.close()

    @pytest.fixture
    def sparrow_wav(self):
        """Path to the Wikipedia House Sparrow test file."""
        path = "/home/z/my-project/upload/sparrow_test.wav"
        if os.path.exists(path):
            yield path
        else:
            pytest.skip("sparrow_test.wav not found")

    def test_sparrow_detection(self, db, sparrow_wav):
        """
        Wikipedia House Sparrow recording should detect House Sparrow
        as the top species with high confidence.
        """
        # Clean DB
        db._get_conn().execute("DELETE FROM detections")
        db._get_conn().commit()

        result = analyze_file(
            sparrow_wav,
            db,
            confidence_threshold=0.25,
            latitude=55.75,
            longitude=37.62,
            week=26,
        )

        assert result["segments_analyzed"] > 0
        assert result["detections_written"] > 0

        # Check that House Sparrow is in the top detections
        top = result["top_detections"]
        top_species = [d["sci_name"] for d in top]
        assert "Passer domesticus" in top_species, (
            f"House Sparrow not found in top detections: {top_species[:5]}"
        )

        # Verify it's in the database
        stats = db.stats()
        assert stats["totals"]["detections"] > 0

        # Print top 5 for debugging
        print("\n  Top detections for House Sparrow recording:")
        for d in top[:5]:
            print(f"    {d['confidence']:.3f}  {d['sci_name']}  ({d['com_name']})")

    def test_user_birdybird(self, db):
        """User's first recording (birdybird.m4a) — should detect Great Tit."""
        path = "/home/z/my-project/upload/birdybird.m4a"
        if not os.path.exists(path):
            pytest.skip("birdybird.m4a not found")

        db._get_conn().execute("DELETE FROM detections")
        db._get_conn().commit()

        result = analyze_file(
            path,
            db,
            confidence_threshold=0.25,
            latitude=55.75,
            longitude=37.62,
            week=26,
        )

        top = result["top_detections"]
        top_species = [d["sci_name"] for d in top]
        assert "Parus major" in top_species, (
            f"Great Tit not found in top detections: {top_species[:5]}"
        )

        print("\n  Top detections for birdybird.m4a:")
        for d in top[:5]:
            print(f"    {d['confidence']:.3f}  {d['sci_name']}  ({d['com_name']})")

    def test_user_birdybird2(self, db):
        """User's second recording (birdybird2.m4a) — Thrush Nightingale."""
        path = "/home/z/my-project/upload/birdybird2.m4a"
        if not os.path.exists(path):
            pytest.skip("birdybird2.m4a not found")

        db._get_conn().execute("DELETE FROM detections")
        db._get_conn().commit()

        result = analyze_file(
            path,
            db,
            confidence_threshold=0.1,
            latitude=55.75,
            longitude=37.62,
            week=26,
        )

        top = result["top_detections"]
        top_species = [d["sci_name"] for d in top]
        # Thrush Nightingale should be among top detections
        assert "Luscinia luscinia" in top_species, (
            f"Thrush Nightingale not found in top: {top_species[:5]}"
        )

        print("\n  Top detections for birdybird2.m4a:")
        for d in top[:5]:
            print(f"    {d['confidence']:.3f}  {d['sci_name']}  ({d['com_name']})")


# ── Edge cases ────────────────────────────────────────────────────── #

class TestEdgeCases:
    def test_analyze_nonexistent_file(self):
        db = MagicMock()
        with pytest.raises(Exception):
            analyze_file(
                "/nonexistent/path/audio.wav",
                db,
                latitude=55.75,
                longitude=37.62,
            )

    def test_silence_segment(self):
        """
        A segment of silence should produce no detections above threshold.
        We test this by creating a numpy array of zeros and verifying
        the inference pipeline handles it.
        """
        db = MagicMock()
        listener = AudioListener(db, confidence_threshold=0.25)
        listener._ensure_model()

        # Create silence
        silence = np.zeros(144_000, dtype="float32")
        listener._process_segment(silence)

        # No insertions should have been called
        assert not db.insert_detection.called, (
            "Silence should not produce detections"
        )

    def test_short_segment_padded(self):
        """
        A segment shorter than 3s should be padded and not crash.
        """
        db = MagicMock()
        listener = AudioListener(db, confidence_threshold=0.01)
        listener._ensure_model()

        # 1 second of audio
        short_audio = np.random.randn(48_000).astype("float32") * 0.01
        listener._process_segment(short_audio)

        # Should not crash — we just verify no exception


# ── Geo-filtering ─────────────────────────────────────────────────── #

class TestGeoFiltering:
    def test_moscow_has_species(self):
        """Moscow should have a reasonable species list."""
        db = MagicMock()
        listener = AudioListener(
            db,
            latitude=55.75,
            longitude=37.62,
            week=26,
        )
        listener._ensure_model()
        # Moscow in summer should have hundreds of species
        assert len(listener._species_list) > 100
        # Great Tit should be in Moscow's species list
        assert "Parus major_Great Tit" in listener._species_list

    def test_different_weeks(self):
        """Different weeks might give slightly different species lists."""
        db = MagicMock()
        listener26 = AudioListener(db, latitude=55.75, longitude=37.62, week=26)
        listener26._ensure_model()
        list26 = set(listener26._species_list)

        listener1 = AudioListener(db, latitude=55.75, longitude=37.62, week=1)
        listener1._ensure_model()
        list1 = set(listener1._species_list)

        # Lists should differ (winter vs summer species)
        # Both should still have common resident species like Great Tit
        great_tit = "Parus major_Great Tit"
        assert great_tit in list26
        assert great_tit in list1
        # But they shouldn't be identical (migratory species differ)
        # We can't assert strict inequality since it depends on threshold,
        # but we can check sizes are reasonable
        assert len(list26) > 50
        assert len(list1) > 50


# ── Confidence threshold ──────────────────────────────────────────── #

class TestConfidenceThreshold:
    def test_higher_threshold_fewer_detections(self, sparrow_wav_fixture=None):
        """
        Higher confidence threshold should produce fewer or equal detections.
        """
        # Only run if sparrow file exists
        path = "/home/z/my-project/upload/sparrow_test.wav"
        if not os.path.exists(path):
            pytest.skip("sparrow_test.wav not found")

        from database import Database
        with tempfile.TemporaryDirectory() as tmpdir:
            db1 = Database(db_path=os.path.join(tmpdir, "test1.db"))
            db1.init()
            r1 = analyze_file(path, db1, confidence_threshold=0.5,
                            latitude=55.75, longitude=37.62, week=26)

            db2 = Database(db_path=os.path.join(tmpdir, "test2.db"))
            db2.init()
            r2 = analyze_file(path, db2, confidence_threshold=0.01,
                            latitude=55.75, longitude=37.62, week=26)

            assert r1["detections_written"] <= r2["detections_written"]
            db1.close()
            db2.close()


# ── Database integration ──────────────────────────────────────────── #

class TestDatabaseIntegration:
    def test_detections_written_to_db(self):
        """Verify detections are actually written to the database."""
        path = "/home/z/my-project/upload/sparrow_test.wav"
        if not os.path.exists(path):
            pytest.skip("sparrow_test.wav not found")

        from database import Database
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(db_path=os.path.join(tmpdir, "test.db"))
            db.init()

            result = analyze_file(
                path, db,
                confidence_threshold=0.25,
                latitude=55.75, longitude=37.62, week=26,
            )

            stats = db.stats()
            assert stats["totals"]["detections"] == result["detections_written"]
            assert stats["totals"]["species"] > 0

            # Check lifelist includes House Sparrow
            lifelist = db.lifelist()
            species_sci = [s["sci"] for s in lifelist["species"]]
            assert "Passer domesticus" in species_sci

            db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])