"""End-to-end self-check that needs no Android device.

It builds a synthetic scrcpy video stream from a video this process encodes
itself, then pushes it through the real demuxer, decoder and GPU renderer. Any
breakage in the framing, the plane layout, the colour maths or the shader shows
up here.
"""

from __future__ import annotations

import logging
import shutil
import struct
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .errors import ConnectionClosed, MirrorScreenError
from .protocol.const import PACKET_FLAG_CONFIG, PACKET_FLAG_KEY_FRAME, PACKET_FLAG_SESSION
from .protocol.framing import SessionPacket, VideoDemuxer
from .protocol.io import MemoryByteSource
from .ui.color import get_conversion
from .video.decoder import VideoDecoder
from .video.frame import VideoFrame

log = logging.getLogger(__name__)

#: The synthetic signal is built and encoded as BT.709 limited range, so the
#: renderer must be told to decode it the same way.
MATRIX = "bt709"
COLOR_RANGE = "limited"

#: Chroma quadrants used for the test pattern: (top-left, top-right,
#: bottom-left, bottom-right) as RGB triples.
_PATTERNS: tuple[tuple[tuple[float, float, float], ...], ...] = (
    ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (1.0, 1.0, 1.0)),
    ((0.0, 1.0, 1.0), (1.0, 0.0, 1.0), (1.0, 1.0, 0.0), (0.0, 0.0, 0.0)),
)
#: Lossless H.264 keeps 8-bit YUV samples bit-exact, so the tolerance covers
#: only rounding in our own float maths.
_YUV_TOLERANCE = 2
_RGB_TOLERANCE = 4


@dataclass(slots=True)
class Check:
    """One assertion in the self-check report."""

    name: str
    ok: bool
    detail: str = ""

    def __str__(self) -> str:
        status = "ok  " if self.ok else "FAIL"
        return f"{status} {self.name}: {self.detail}"


@dataclass(slots=True)
class SelfCheckReport:
    checks: list[Check] = field(default_factory=list)
    image_path: Path | None = None

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    def render(self) -> str:
        lines = [str(check) for check in self.checks]
        lines.append("")
        lines.append("RESULT: " + ("all checks passed" if self.ok else "FAILURES PRESENT"))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Test signal generation
# --------------------------------------------------------------------------
def build_raw_yuv(
    path: Path, width: int, height: int, frames: int
) -> tuple[np.ndarray, np.ndarray]:
    """Write a raw ``yuv420p`` clip; return the first frame's Y and U planes.

    The values are produced by our own encoder maths, so the expected decode
    result is known exactly.
    """
    conversion = get_conversion(MATRIX, COLOR_RANGE)
    inverse = np.array(conversion.inverse_rows(), dtype=float)
    offset = np.array(conversion.offset, dtype=float)

    frames_bytes = bytearray()
    first_y: np.ndarray | None = None
    first_u: np.ndarray | None = None

    half_width = width // 2
    half_height = height // 2

    for index in range(frames):
        quadrants = _PATTERNS[index % len(_PATTERNS)]
        rgb = np.empty((height, width, 3), dtype=float)
        rgb[:half_height, :half_width] = quadrants[0]
        rgb[:half_height, half_width:] = quadrants[1]
        rgb[half_height:, :half_width] = quadrants[2]
        rgb[half_height:, half_width:] = quadrants[3]

        yuv = rgb @ inverse.T + offset
        yuv = np.clip(np.rint(yuv * 255.0), 0, 255).astype(np.uint8)
        y = yuv[:, :, 0]
        u = yuv[::2, ::2, 1]
        v = yuv[::2, ::2, 2]

        if index == 0:
            first_y, first_u = y.copy(), u.copy()

        frames_bytes += y.tobytes() + u.tobytes() + v.tobytes()

    path.write_bytes(bytes(frames_bytes))
    assert first_y is not None and first_u is not None
    return first_y, first_u


def encode_h264(ffmpeg: str, raw: Path, out: Path, width: int, height: int) -> None:
    """Encode a raw clip losslessly, tagging it BT.709 limited range."""
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        # Declare the input's range and matrix: without this, ffmpeg assumes
        # full-range input and silently converts the samples, which would make
        # every colour comparison off by a few percent.
        "-color_range",
        "tv",
        "-colorspace",
        "bt709",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-s",
        f"{width}x{height}",
        "-r",
        "30",
        "-i",
        str(raw),
        "-c:v",
        "libx264",
        "-qp",
        "0",  # lossless: the decoded YUV must match the source exactly
        "-preset",
        "ultrafast",
        "-g",
        "10",
        "-bf",
        "0",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-color_range",
        "tv",
        "-f",
        "h264",
        str(out),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MirrorScreenError(
            f"ffmpeg failed to encode the test clip:\n{result.stderr.strip()}"
        )


def iter_annex_b(payload: bytes) -> Iterator[bytes]:
    """Yield the NAL units of an Annex-B buffer."""
    starts: list[tuple[int, int]] = []
    index = 0
    length = len(payload)
    while index < length - 3:
        if payload[index] == 0 and payload[index + 1] == 0:
            if payload[index + 2] == 1:
                starts.append((index, 3))
                index += 3
                continue
            if (
                index < length - 4
                and payload[index + 2] == 0
                and payload[index + 3] == 1
            ):
                starts.append((index, 4))
                index += 4
                continue
        index += 1

    for position, (offset, header) in enumerate(starts):
        start = offset + header
        end = starts[position + 1][0] if position + 1 < len(starts) else length
        if start < end:
            yield payload[start:end]


def h264_config_packet(payload: bytes) -> bytes:
    """Extract SPS/PPS as MediaCodec would deliver them in a config packet."""
    out = bytearray()
    for nal in iter_annex_b(payload):
        if nal and (nal[0] & 0x1F) in (7, 8):
            out += b"\x00\x00\x00\x01" + nal
    return bytes(out)


def read_packets(path: Path) -> list[tuple[bytes, int, bool]]:
    """Demux an H.264 file into ``(payload, pts_us, key_frame)`` triples."""
    import av

    container = av.open(str(path), format="h264")
    stream = container.streams.video[0]
    packets: list[tuple[bytes, int, bool]] = []
    for packet in container.demux(stream):
        if packet.size == 0:
            continue
        pts_us = 0
        if packet.pts is not None:
            pts_us = int(packet.pts * packet.time_base * 1_000_000)
        packets.append((bytes(packet), pts_us, bool(packet.is_keyframe)))
    container.close()
    return packets


# --------------------------------------------------------------------------
# Synthetic scrcpy stream
# --------------------------------------------------------------------------
def session_header(width: int, height: int, *, client_resized: bool = False) -> bytes:
    """A 12-byte session packet, exactly as the server writes it."""
    flags = (PACKET_FLAG_SESSION >> 32) | (1 if client_resized else 0)
    return struct.pack(">III", flags, width, height)


def media_header(size: int, pts_us: int, *, config: bool = False, key_frame: bool = False) -> bytes:
    """A 12-byte media packet header."""
    flags = PACKET_FLAG_CONFIG if config else pts_us
    if key_frame and not config:
        flags |= PACKET_FLAG_KEY_FRAME
    return struct.pack(">QI", flags, size)


def build_synthetic_stream(
    segments: list[tuple[int, int, list[tuple[bytes, int, bool]]]],
) -> bytes:
    """Assemble a scrcpy video stream from ``(width, height, packets)`` parts."""
    out = bytearray()
    for width, height, packets in segments:
        out += session_header(width, height)
        config = h264_config_packet(packets[0][0]) if packets else b""
        if config:
            out += media_header(len(config), 0, config=True) + config
        for payload, pts_us, key_frame in packets:
            out += media_header(len(payload), pts_us, key_frame=key_frame) + payload
    return bytes(out)


def demux_and_decode(stream: bytes, codec: str = "h264") -> tuple[list[VideoFrame], list[SessionPacket]]:
    """Push a synthetic stream through the real demuxer and decoder."""
    demuxer = VideoDemuxer(MemoryByteSource(stream))
    first = demuxer.start()

    decoder = VideoDecoder(codec, first.width, first.height)
    frames: list[VideoFrame] = []
    sessions: list[SessionPacket] = [first]

    while True:
        try:
            packet = demuxer.read_packet()
        except ConnectionClosed:
            break
        if isinstance(packet, SessionPacket):
            sessions.append(packet)
            decoder.reset(packet.width, packet.height)
            continue
        frames.extend(decoder.decode(packet))

    return frames, sessions


# --------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------
def run_self_check(
    work_dir: Path,
    *,
    output_image: Path | None = None,
    progress: Callable[[str], None] | None = None,
    width: int = 320,
    height: int = 240,
    rotated_width: int = 240,
    rotated_height: int = 320,
    frames_per_segment: int = 6,
) -> SelfCheckReport:
    """Run every check and return the report."""
    report = SelfCheckReport()
    say = progress or (lambda _message: None)

    ffmpeg = shutil.which("ffmpeg")
    report.add("ffmpeg available", ffmpeg is not None, ffmpeg or "not found on PATH")
    if ffmpeg is None:
        report.add("checks skipped", False, "ffmpeg is required to build the test clip")
        return report

    work_dir.mkdir(parents=True, exist_ok=True)

    say("generating a lossless test clip")
    raw = work_dir / "pattern.yuv"
    first_y, first_u = build_raw_yuv(raw, width, height, frames_per_segment)
    clip = work_dir / "pattern.h264"
    encode_h264(ffmpeg, raw, clip, width, height)

    rotated_raw = work_dir / "pattern-rotated.yuv"
    build_raw_yuv(rotated_raw, rotated_width, rotated_height, frames_per_segment)
    rotated_clip = work_dir / "pattern-rotated.h264"
    encode_h264(ffmpeg, rotated_raw, rotated_clip, rotated_width, rotated_height)

    primary = read_packets(clip)
    rotated = read_packets(rotated_clip)
    report.add(
        "test clip encoded",
        len(primary) >= frames_per_segment,
        f"{len(primary)} access units at {width}x{height}",
    )

    # -- framing ------------------------------------------------------------
    stream = build_synthetic_stream(
        [
            (width, height, primary),
            (rotated_width, rotated_height, rotated),
        ]
    )
    frames, sessions = demux_and_decode(stream)
    seen_sizes = [(session.width, session.height) for session in sessions]
    report.add(
        "session packets parsed",
        seen_sizes == [(width, height), (rotated_width, rotated_height)],
        f"{len(sessions)} sessions, sizes {seen_sizes}",
    )
    report.add(
        "access units decoded",
        len(frames) >= 2 * frames_per_segment - 2,
        f"{len(frames)} frames from {len(primary) + len(rotated)} packets",
    )
    if not frames:
        report.add("decoding produced frames", False, "no frames decoded")
        return report

    sizes = {(frame.width, frame.height) for frame in frames}
    report.add(
        "resolution change handled",
        sizes == {(width, height), (rotated_width, rotated_height)},
        f"frame sizes seen: {sorted(sizes)}",
    )

    # -- plane layout and colour -------------------------------------------
    first = next(f for f in frames if f.size == (width, height))

    # If the chroma planes were sliced out of PyAV's packed buffer incorrectly
    # (see split_yuv420p_planes), these quadrants would not line up.
    conversion = get_conversion(MATRIX, COLOR_RANGE)
    quadrants = _PATTERNS[0]
    worst_yuv = 0
    worst_rgb = 0
    expected_colors: dict[tuple[int, int], tuple[float, float, float]] = {}

    for index, (qx, qy) in enumerate(
        [
            (width // 4, height // 4),
            (3 * width // 4, height // 4),
            (width // 4, 3 * height // 4),
            (3 * width // 4, 3 * height // 4),
        ]
    ):
        expected_rgb = quadrants[index]
        expected_yuv = conversion.encode(*expected_rgb)
        expected_colors[(qx, qy)] = expected_rgb

        actual_y = float(first.y[qy, qx]) / 255.0
        actual_u = float(first.u[qy // 2, qx // 2]) / 255.0
        actual_v = float(first.v[qy // 2, qx // 2]) / 255.0
        worst_yuv = max(
            worst_yuv,
            abs(actual_y - expected_yuv[0]) * 255.0,
            abs(actual_u - expected_yuv[1]) * 255.0,
            abs(actual_v - expected_yuv[2]) * 255.0,
        )

        recovered = conversion.apply(actual_y, actual_u, actual_v)
        worst_rgb = max(
            worst_rgb,
            *(abs(recovered[i] - expected_rgb[i]) * 255.0 for i in range(3)),
        )

    report.add(
        "decoded YUV matches the source",
        worst_yuv <= _YUV_TOLERANCE,
        f"largest deviation {worst_yuv:.1f} (tolerance {_YUV_TOLERANCE})",
    )
    report.add(
        "colour matrix reconstructs the source",
        worst_rgb <= _RGB_TOLERANCE,
        f"largest deviation {worst_rgb:.1f}/255 (tolerance {_RGB_TOLERANCE})",
    )

    # -- GPU rendering -----------------------------------------------------
    image = _render_offscreen(first, conversion, width, height)
    if image is None:
        report.add("offscreen render", False, "no Qt application could be created")
        return report

    worst_render = 0
    for (qx, qy), expected_rgb in expected_colors.items():
        colour = image.pixelColor(qx, qy)
        actual = (colour.red() / 255.0, colour.green() / 255.0, colour.blue() / 255.0)
        worst_render = max(
            worst_render, *(abs(actual[i] - expected_rgb[i]) * 255.0 for i in range(3))
        )
    report.add(
        "shader output matches the reference",
        worst_render <= _RGB_TOLERANCE,
        f"largest deviation {worst_render:.1f}/255 (tolerance {_RGB_TOLERANCE})",
    )

    # Vertical orientation: the top half of the image must show the top half of
    # the frame, which the flipped texture coordinates are responsible for.
    top = image.pixelColor(width // 4, height // 8)
    bottom = image.pixelColor(width // 4, 7 * height // 8)
    report.add(
        "image is not vertically flipped",
        top.red() > 200 and bottom.green() > 200,
        f"top-left {top.getRgb()} (expect red), bottom-left {bottom.getRgb()} (expect green)",
    )

    if output_image is not None:
        output_image.parent.mkdir(parents=True, exist_ok=True)
        saved = image.save(str(output_image))
        report.image_path = output_image if saved else None
        report.add("screenshot written", saved, str(output_image))

    # The quadrants must also be checkable across the whole plane, so report
    # the extremes of the chroma planes as an extra signal of a sane layout.
    report.add(
        "chroma planes are not uniform",
        int(first.u.min()) != int(first.u.max()) and int(first.v.min()) != int(first.v.max()),
        f"U range {int(first.u.min())}..{int(first.u.max())}, "
        f"V range {int(first.v.min())}..{int(first.v.max())}",
    )

    return report


def _render_offscreen(
    frame: VideoFrame, conversion, width: int, height: int
):
    """Render a frame offscreen, creating a Qt application if needed."""
    from PySide6.QtGui import QGuiApplication

    from .ui.offscreen import OffscreenRenderer

    if QGuiApplication.instance() is None:
        try:
            QGuiApplication([])
        except Exception as exc:  # pragma: no cover - headless environment
            log.debug("could not create a Qt application: %s", exc)
            return None

    with OffscreenRenderer(filter_mode="nearest") as renderer:
        return renderer.render(frame, width, height, conversion=conversion)


__all__ = ["Check", "SelfCheckReport", "run_self_check"]
