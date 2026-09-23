"""Tests for the headless device probe's reporting (no device required)."""

from __future__ import annotations

import pytest

from mirror_screen.probe import ProbeResult


def test_probe_result_ok_requires_frames_and_no_error():
    assert not ProbeResult().ok
    assert not ProbeResult(frames=10).ok
    assert ProbeResult(frames=10, width=1080, height=2340).ok
    assert not ProbeResult(frames=10, error="device closed the video stream").ok


def test_probe_report_contains_the_key_numbers():
    result = ProbeResult(
        device_name="SM-A175F",
        serial="R5GL74L3WHX",
        codec="h264",
        width=720,
        height=1600,
        frames=300,
        duration=5.0,
        fps=60.0,
        average_decode_ms=2.5,
        lag_ms=8.0,
        bytes_received=5_000_000,
    )
    text = result.render()

    assert "SM-A175F" in text
    assert "R5GL74L3WHX" in text
    assert "720x1600" in text
    assert "60.0 fps" in text
    assert "2.50 ms" in text

    # 720 * 1600 * 60 / 1e6 = 69.12 MPix/s
    assert result.megapixels_per_second == pytest.approx(69.12, abs=0.01)
    assert "69 MPix/s" in text


def test_probe_report_mentions_drops_and_errors_only_when_present():
    quiet = ProbeResult(width=100, height=100, frames=1, duration=1.0).render()
    assert "dropped" not in quiet
    assert "ERROR" not in quiet

    noisy = ProbeResult(
        width=100, height=100, frames=1, duration=1.0, dropped=7, error="boom"
    ).render()
    assert "dropped" in noisy
    assert "ERROR" in noisy


def test_probe_report_flags_a_static_screen_as_zero_frames():
    """A static screen yields no frames; the report must be explicit about it."""
    result = ProbeResult(width=720, height=1600, frames=0, duration=5.0)
    assert not result.ok
    assert "0 in 5.0s" in result.render()
