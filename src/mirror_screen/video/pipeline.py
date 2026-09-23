"""Read, decode and publish frames on a dedicated thread.

Latency policy: the pipeline never queues. The newest decoded frame replaces
any frame the UI has not picked up yet, so a slow repaint costs dropped frames
instead of growing delay.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from ..config import SessionConfig
from ..errors import ConnectionClosed, MirrorScreenError
from ..protocol.framing import MediaPacket, SessionPacket, StreamMeta, VideoDemuxer
from ..protocol.io import SocketByteSource
from .decoder import VideoDecoder
from .frame import VideoFrame

log = logging.getLogger(__name__)


class FrameMailbox:
    """A single-slot mailbox holding only the most recent frame."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: VideoFrame | None = None
        self.overwritten = 0

    def put(self, frame: VideoFrame) -> None:
        with self._lock:
            if self._frame is not None:
                self.overwritten += 1
            self._frame = frame

    def take(self) -> VideoFrame | None:
        with self._lock:
            frame, self._frame = self._frame, None
            return frame

    def peek(self) -> VideoFrame | None:
        with self._lock:
            return self._frame

    def clear(self) -> None:
        with self._lock:
            self._frame = None


@dataclass(slots=True)
class PipelineStats:
    """Live counters for the status display."""

    packets: int = 0
    frames: int = 0
    bytes_received: int = 0
    decode_seconds: float = 0.0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    #: Time spent blocked waiting for the device to send data. This and
    #: ``decode_seconds`` are the only latency figures that can be measured on
    #: one clock, which makes them the trustworthy ones.
    read_wait_seconds: float = 0.0
    #: Raw difference between host elapsed time and device timestamp progress.
    #: Useful only for spotting a runaway queue: it also grows linearly from
    #: device clock/quantisation drift, so it is not a latency measurement.
    stream_lag_ms: float = 0.0

    @property
    def average_decode_ms(self) -> float:
        if not self.frames:
            return 0.0
        return self.decode_seconds / self.frames * 1000.0

    @property
    def idle_fraction(self) -> float:
        """Share of pipeline time spent waiting rather than decoding.

        Near 1.0 means the device cannot feed frames faster than we consume
        them, so the software is not the bottleneck; a low value means decoding
        limits the frame rate and delay can accumulate.
        """
        total = self.read_wait_seconds + self.decode_seconds
        if total <= 0:
            return 0.0
        return self.read_wait_seconds / total


@dataclass(slots=True)
class PipelineCallbacks:
    """Hooks invoked from the pipeline thread."""

    on_frame: Callable[[VideoFrame], None] = lambda _frame: None
    on_session: Callable[[SessionPacket], None] = lambda _session: None
    on_end: Callable[[str], None] = lambda _reason: None
    on_error: Callable[[Exception], None] = lambda _exc: None


class VideoPipeline:
    """Owns the video socket: demux -> decode -> publish."""

    def __init__(
        self,
        sock: socket.socket,
        config: SessionConfig,
        mailbox: FrameMailbox,
        callbacks: PipelineCallbacks | None = None,
        *,
        decoder: VideoDecoder | None = None,
    ) -> None:
        self._sock = sock
        self._config = config
        self._mailbox = mailbox
        self._callbacks = callbacks or PipelineCallbacks()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._decoder = decoder
        self.stats = PipelineStats()

        # Rolling window used to compute the frame rate.
        self._frame_times: deque[float] = deque(maxlen=60)
        self._first_host_time: float | None = None
        self._first_pts_us: int | None = None
        self._current_size: tuple[int, int] = (0, 0)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("pipeline already started")
        self._thread = threading.Thread(
            target=self._run, name="video-pipeline", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Stop the thread, unblocking the socket read if needed."""
        self._stop.set()
        # Shutting down the read side unblocks a pending recv().
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RD)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _handle_end(self, reason: str) -> None:
        log.info("video pipeline ended: %s", reason)
        self._callbacks.on_end(reason)

    # -- worker -------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._consume()
        except ConnectionClosed:
            if not self._stop.is_set():
                self._handle_end("device closed the video stream")
        except OSError as exc:
            # Closing the socket during shutdown makes recv() fail; that is
            # expected and must not be reported as a fault.
            if self._stop.is_set():
                log.debug("video socket closed during shutdown: %s", exc)
            else:
                self._handle_end(f"video socket error: {exc}")
        except MirrorScreenError as exc:
            log.error("video pipeline error: %s", exc)
            self._callbacks.on_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("unexpected video pipeline failure")
            self._callbacks.on_error(exc)
        except BaseException:  # pragma: no cover - shutdown path
            raise

    def _consume(self) -> None:
        demuxer = VideoDemuxer(SocketByteSource(self._sock))

        meta = demuxer.start()
        log.info(
            "stream metadata: %s %dx%d",
            meta.codec_name or self._config.video_codec,
            meta.width,
            meta.height,
        )
        self._apply_session(meta, initial=True)

        while not self._stop.is_set():
            read_start = time.perf_counter()
            packet = demuxer.read_packet()
            self.stats.read_wait_seconds += time.perf_counter() - read_start

            if isinstance(packet, SessionPacket):
                self._apply_session(packet, initial=False)
                continue

            self.stats.packets += 1
            self.stats.bytes_received += len(packet.payload)

            start = time.perf_counter()
            frames = self._decode(packet)
            self.stats.decode_seconds += time.perf_counter() - start

            for frame in frames:
                self._publish(frame)

    def _decode(self, packet: MediaPacket) -> list[VideoFrame]:
        assert self._decoder is not None
        return self._decoder.decode(packet)

    def _apply_session(self, session: SessionPacket | StreamMeta, *, initial: bool) -> None:
        size = (session.width, session.height)
        # Prefer the codec the device actually reports over the one we asked
        # for, so a fallback on the device side cannot desynchronise us.
        codec = getattr(session, "codec_name", None) or self._config.video_codec
        if codec != self._config.video_codec:
            log.warning(
                "device is streaming %s although %s was requested",
                codec,
                self._config.video_codec,
            )

        if self._decoder is None:
            self._decoder = VideoDecoder(codec, session.width, session.height)
        elif size != self._current_size:
            self._decoder.reset(session.width, session.height)

        self._current_size = size
        self.stats.width = session.width
        self.stats.height = session.height

        log.info(
            "%s video session: %dx%d%s",
            "initial" if initial else "new",
            session.width,
            session.height,
            " (client resized)" if session.client_resized else "",
        )
        self._callbacks.on_session(session)

    def _publish(self, frame: VideoFrame) -> None:
        now = time.perf_counter()
        self.stats.frames += 1
        self._frame_times.append(now)

        if self._first_host_time is None:
            self._first_host_time = now
            self._first_pts_us = frame.pts_us
        elif frame.pts_us and self._first_pts_us is not None:
            # The device and the host have unrelated clocks, so compare how far
            # each has advanced: any excess is time we added (queueing, decode,
            # repaint) rather than time the device spent producing frames.
            host_delta = (now - self._first_host_time) * 1_000_000
            device_delta = frame.pts_us - self._first_pts_us
            lag_ms = (host_delta - device_delta) / 1000.0
            # Smoothed, clamped at 0 because device clock drift can make this
            # negative. See PipelineStats.stream_lag_ms for the caveats.
            self.stats.stream_lag_ms = max(
                0.0, 0.8 * self.stats.stream_lag_ms + 0.2 * lag_ms
            )

        if len(self._frame_times) >= 2:
            span = self._frame_times[-1] - self._frame_times[0]
            if span > 0:
                self.stats.fps = (len(self._frame_times) - 1) / span

        self._mailbox.put(frame)
        self._callbacks.on_frame(frame)
