"""End-to-end pipeline test driven by a synthetic stream (no device needed).

This exercises the real demuxer, decoder, colour maths and GPU shader. It is
skipped when ffmpeg is unavailable (ffmpeg is only needed to encode the test
clip; PyAV brings its own FFmpeg for decoding).
"""

from __future__ import annotations

import shutil

import pytest

pytest.importorskip("av")
pytest.importorskip("PySide6")

from mirror_screen.selfcheck import run_self_check  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is required to build the test clip"
)


def test_pipeline_end_to_end(tmp_path):
    report = run_self_check(
        tmp_path / "work",
        output_image=tmp_path / "pattern.png",
        width=160,
        height=120,
        rotated_width=120,
        rotated_height=160,
        frames_per_segment=4,
    )

    assert report.ok, report.render()
    assert (tmp_path / "pattern.png").is_file()


def test_self_check_reports_a_truncated_stream():
    """A stream that ends mid-header must fail loudly instead of hanging."""
    from mirror_screen.errors import ConnectionClosed
    from mirror_screen.protocol.const import PACKET_HEADER_SIZE
    from mirror_screen.selfcheck import demux_and_decode

    with pytest.raises(ConnectionClosed):
        demux_and_decode(b"\x00" * (PACKET_HEADER_SIZE - 1))
