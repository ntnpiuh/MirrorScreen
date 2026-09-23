"""Headless end-to-end check against a real device.

Starts the on-device server, decodes for a few seconds and reports what it saw.
No window is opened, so this is the quickest way to confirm that the device
handshake, the demuxing and the decoding all work against real hardware, and to
get actual frame rate and latency numbers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .app import start_device_session
from .config import SessionConfig
from .video.pipeline import FrameMailbox, PipelineCallbacks, VideoPipeline

log = logging.getLogger(__name__)

#: How often the polling loop wakes up.
_POLL_INTERVAL = 0.005


@dataclass(slots=True)
class ProbeResult:
    """What the probe observed."""

    device_name: str = ""
    serial: str = ""
    codec: str = ""
    width: int = 0
    height: int = 0
    sessions: int = 0
    frames: int = 0
    duration: float = 0.0
    fps: float = 0.0
    average_decode_ms: float = 0.0
    lag_ms: float = 0.0
    idle_fraction: float = 0.0
    dropped: int = 0
    bytes_received: int = 0
    end_reason: str = ""
    error: str = ""
    screenshot: Path | None = None
    control_ok: bool = False
    control_note: str = ""
    clipboard_text: str = ""

    @property
    def ok(self) -> bool:
        """True when we learned the video format and actually decoded frames."""
        return not self.error and self.frames > 0 and self.width > 0

    @property
    def megapixels_per_second(self) -> float:
        return self.width * self.height * self.fps / 1e6

    def render(self) -> str:
        lines = [
            f"device         {self.device_name} ({self.serial})",
            f"video          {self.width}x{self.height} {self.codec}",
            f"frames         {self.frames} in {self.duration:.1f}s ({self.fps:.1f} fps)",
            f"decode         {self.average_decode_ms:.2f} ms/frame average",
            f"keeping up     {self.idle_fraction * 100:.1f}% idle "
            "(waiting vs decoding)",
            f"throughput     {self.megapixels_per_second:.0f} MPix/s",
            "stream drift   "
            f"{self.lag_ms:.0f} ms (host clock vs device stamps, not a latency "
            "measurement)",
            f"stream         {self.bytes_received / max(self.duration, 0.001) / 125000:.2f} Mbit/s",
        ]
        if self.dropped:
            lines.append(f"dropped        {self.dropped} (frames the UI never picked up)")
        if self.sessions > 1:
            lines.append(f"sessions       {self.sessions} (resolution changed mid-stream)")
        if self.screenshot is not None:
            lines.append(f"screenshot     {self.screenshot}")
        if self.end_reason:
            lines.append(f"stream ended   {self.end_reason}")
        if self.control_note:
            lines.append(f"control        {self.control_note}")
        if self.error:
            lines.append(f"ERROR          {self.error}")
        return "\n".join(lines)


def run_probe(
    config: SessionConfig,
    *,
    duration: float = 5.0,
    screenshot: Path | None = None,
    check_control: bool = True,
    progress=None,
) -> ProbeResult:
    """Decode from a real device for ``duration`` seconds and report."""
    result = ProbeResult(codec=config.video_codec)
    say = progress or (lambda _message: None)

    server, session = start_device_session(config, progress=progress)
    pipeline: VideoPipeline | None = None
    try:
        result.device_name = session.device_name
        result.serial = session.serial

        mailbox = FrameMailbox()
        callbacks = PipelineCallbacks(
            on_session=lambda packet: _record_session(result, packet),
            on_end=lambda reason: setattr(result, "end_reason", reason),
            on_error=lambda exc: setattr(result, "error", str(exc)),
        )
        pipeline = VideoPipeline(session.video, config, mailbox, callbacks)
        pipeline.start()

        say(f"decoding for {duration:.0f}s - interact with the phone for a busier test")
        deadline = time.monotonic() + duration
        last_frame = None
        while time.monotonic() < deadline and not result.error:
            frame = mailbox.take()
            if frame is not None:
                last_frame = frame
            # A static screen produces no frames at all: MediaCodec only emits
            # when something changes, so an idle phone is not a failure.
            time.sleep(_POLL_INTERVAL)

        result.duration = duration
        stats = pipeline.stats
        result.frames = stats.frames
        result.fps = stats.fps
        result.average_decode_ms = stats.average_decode_ms
        result.lag_ms = stats.stream_lag_ms
        result.idle_fraction = stats.idle_fraction
        result.bytes_received = stats.bytes_received
        result.dropped = mailbox.overwritten

        if screenshot is not None and last_frame is not None:
            result.screenshot = _write_screenshot(last_frame, screenshot)

        if check_control and session.control is not None:
            _check_control(result, session.control)

        return result
    finally:
        if pipeline is not None:
            pipeline.stop()
        server.stop()


def _record_session(result: ProbeResult, packet) -> None:
    result.sessions += 1
    result.width = packet.width
    result.height = packet.height


#: Written to the device clipboard only if the read-only check cannot answer.
_CLIPBOARD_MARKER = "mirror-screen control check"


def _check_control(result: ProbeResult, control_socket, timeout: float = 3.0) -> None:
    """Prove the control socket works, preferably without changing anything.

    A GET_CLIPBOARD is read-only, but a device with an empty clipboard may
    legitimately stay silent, so fall back to a write that the server must
    acknowledge.
    """
    from .control_channel import ControlChannel

    seen: list[str] = []
    acks: list[int] = []
    channel = ControlChannel(
        control_socket, on_clipboard=seen.append, on_ack=acks.append
    )
    channel.start()
    try:
        channel.request_clipboard()
        if _wait_for(lambda: bool(seen), timeout):
            result.control_ok = True
            result.clipboard_text = seen[0]
            result.control_note = "ok (read-only clipboard round trip)"
            return

        sequence = channel.set_clipboard(_CLIPBOARD_MARKER)
        if _wait_for(lambda: sequence in acks, timeout):
            result.control_ok = True
            result.control_note = (
                "ok (write acknowledged; the device clipboard now holds "
                f"{_CLIPBOARD_MARKER!r})"
            )
        else:
            result.control_note = "no reply from the device on the control socket"
    finally:
        channel.stop()


def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _write_screenshot(frame, path: Path) -> Path | None:
    """Render one decoded frame to a PNG (needs a Qt application)."""
    try:
        from PySide6.QtGui import QGuiApplication

        from .ui.color import resolve
        from .ui.offscreen import OffscreenRenderer

        if QGuiApplication.instance() is None:
            QGuiApplication([])

        conversion = resolve(
            "auto", "auto", frame_matrix=frame.color_matrix, frame_range=frame.color_range
        )
        with OffscreenRenderer(filter_mode="nearest") as renderer:
            return renderer.save(
                frame, path, frame.width, frame.height, conversion=conversion
            )
    except Exception as exc:  # pragma: no cover - depends on the environment
        log.warning("could not write a screenshot: %s", exc)
        return None


__all__ = ["ProbeResult", "run_probe"]
