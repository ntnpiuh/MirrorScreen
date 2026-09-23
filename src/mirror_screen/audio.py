"""Low-latency audio demuxing, decoding, buffering, and sink boundaries."""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol

import av

from .errors import ConnectionClosed, DecodeError, MirrorScreenError
from .protocol.const import AUDIO_CODEC_NAMES, FFMPEG_DECODERS
from .protocol.framing import AudioDemuxer, MediaPacket
from .protocol.io import SocketByteSource

log = logging.getLogger(__name__)


class AudioError(MirrorScreenError):
    """Audio failed independently of the video stream."""


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """Interleaved signed 16-bit PCM ready for an output sink."""

    data: bytes
    sample_rate: int
    channels: int
    pts_us: int = 0


@dataclass(frozen=True, slots=True)
class AudioStats:
    """Low-cost counters for diagnosing audio without a debug panel."""

    packets: int = 0
    chunks: int = 0
    bytes_written: int = 0
    dropped: int = 0
    underruns: int = 0
    startup_ms: float = 0.0


class AudioSink(Protocol):
    """Output adapter owned by the application/UI layer."""

    def start(self, sample_rate: int, channels: int) -> None: ...

    def write(self, chunk: AudioChunk) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class BoundedAudioBuffer:
    """A small drop-oldest buffer that bounds audio latency and memory."""

    def __init__(self, max_chunks: int = 4) -> None:
        if max_chunks <= 0:
            raise ValueError("max_chunks must be > 0")
        self._chunks: deque[AudioChunk] = deque(maxlen=max_chunks)
        self._condition = threading.Condition()
        self.dropped = 0
        self.underruns = 0
        self._closed = False

    def put(self, chunk: AudioChunk) -> None:
        with self._condition:
            if self._closed:
                return
            if len(self._chunks) == self._chunks.maxlen:
                self._chunks.popleft()
                self.dropped += 1
            self._chunks.append(chunk)
            self._condition.notify()

    def get(self, timeout: float | None = None) -> AudioChunk | None:
        with self._condition:
            if not self._chunks and not self._closed:
                if not self._condition.wait(timeout):
                    self.underruns += 1
                    return None
            if not self._chunks:
                return None
            return self._chunks.popleft()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._chunks.clear()
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def stats(self) -> tuple[int, int]:
        """Return drop and underrun counters as one consistent snapshot."""
        with self._condition:
            return self.dropped, self.underruns


class AudioDecoder:
    """Decode compressed scrcpy audio and resample it to signed 16-bit PCM."""

    def __init__(self, codec_name: str, *, sample_rate: int = 48_000, channels: int = 2):
        if codec_name not in FFMPEG_DECODERS:
            raise DecodeError(f"no FFmpeg decoder mapped for audio codec {codec_name!r}")
        self.codec_name = codec_name
        self.sample_rate = sample_rate
        self.channels = channels
        self._context: av.CodecContext | None = None
        self._resampler: av.AudioResampler | None = None

    def _ensure_context(self) -> av.CodecContext:
        if self._context is None:
            try:
                self._context = av.CodecContext.create(FFMPEG_DECODERS[self.codec_name], "r")
            except Exception as exc:  # pragma: no cover - FFmpeg build dependent
                raise DecodeError(f"could not create audio decoder: {exc}") from exc
        return self._context

    def _ensure_resampler(self) -> av.AudioResampler:
        if self._resampler is None:
            try:
                layout = "mono" if self.channels == 1 else "stereo"
                self._resampler = av.AudioResampler(
                    format="s16", layout=layout, rate=self.sample_rate
                )
            except Exception as exc:  # pragma: no cover - FFmpeg build dependent
                raise DecodeError(f"could not create audio resampler: {exc}") from exc
        return self._resampler

    def decode(self, packet: MediaPacket) -> list[AudioChunk]:
        if packet.config:
            # MediaCodec reports out-of-band decoder configuration (the AAC
            # AudioSpecificConfig, FLAC STREAMINFO, ...) as a config-flagged
            # packet with no audio in it. Apply it as extradata before any
            # frame is decoded, mirroring how the video decoder primes
            # SPS/PPS from its own config packets.
            try:
                self._ensure_context().extradata = packet.payload
            except av.AVError as exc:
                raise DecodeError(f"could not apply audio codec config: {exc}") from exc
            return []
        try:
            frames = self._ensure_context().decode(av.Packet(packet.payload))
            chunks: list[AudioChunk] = []
            for frame in frames:
                for converted in self._ensure_resampler().resample(frame):
                    chunks.append(
                        AudioChunk(
                            data=converted.to_ndarray().tobytes(),
                            sample_rate=self.sample_rate,
                            channels=self.channels,
                            pts_us=packet.pts_us,
                        )
                    )
            return chunks
        except av.AVError as exc:
            raise DecodeError(f"audio decode failed: {exc}") from exc

    def close(self) -> None:
        self._context = None
        self._resampler = None


class AudioWorker:
    """Own the audio socket and sink on non-GUI threads.

    Decoding and playback run on two separate threads, connected only by
    ``buffer``: a decode thread demuxes and decodes packets straight off the
    socket, and a writer thread pulls chunks out of the buffer and feeds the
    sink. This keeps a slow/blocking sink write from stalling the socket
    read, and it means the sink - a Qt audio backend, not safe to touch from
    more than one thread - is created, written to, flushed, and closed
    entirely on the writer thread.
    """

    def __init__(
        self,
        sock: socket.socket,
        sink: AudioSink,
        *,
        buffer: BoundedAudioBuffer | None = None,
        on_error=None,
    ) -> None:
        self._sock = sock
        self._sink = sink
        self._buffer = buffer or BoundedAudioBuffer()
        self._on_error = on_error or (lambda _exc: None)
        self._stop = threading.Event()
        self._decode_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self.codec_name: str | None = None
        self._stats_lock = threading.Lock()
        self._packets = 0
        self._chunks = 0
        self._bytes_written = 0
        self._started_at: float | None = None
        self._first_chunk_at: float | None = None
        #: Sample rate and channel count, handed from the decode thread to
        #: the writer thread once the codec is known; ``None`` if decoding
        #: never got that far.
        self._format: tuple[int, int] | None = None
        self._format_ready = threading.Event()

    def stats(self) -> AudioStats:
        """Return a thread-safe snapshot suitable for UI status text."""
        with self._stats_lock:
            started_at = self._started_at
            first_chunk_at = self._first_chunk_at
            packets = self._packets
            chunks = self._chunks
            bytes_written = self._bytes_written
        dropped, underruns = self._buffer.stats()
        startup_ms = (
            max(0.0, first_chunk_at - started_at) * 1000.0
            if started_at is not None and first_chunk_at is not None
            else 0.0
        )
        return AudioStats(
            packets=packets,
            chunks=chunks,
            bytes_written=bytes_written,
            dropped=dropped,
            underruns=underruns,
            startup_ms=startup_ms,
        )

    def start(self) -> None:
        if self._decode_thread is not None:
            raise RuntimeError("audio worker already started")
        self._decode_thread = threading.Thread(
            target=self._run_decode, name="audio-decoder", daemon=True
        )
        self._writer_thread = threading.Thread(
            target=self._run_writer, name="audio-writer", daemon=True
        )
        self._decode_thread.start()
        self._writer_thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._buffer.close()
        # Unblock a writer that never received a format (decoding failed
        # before the codec was known).
        self._format_ready.set()
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RD)
        decode_thread, self._decode_thread = self._decode_thread, None
        if decode_thread is not None:
            decode_thread.join(timeout=timeout)
        writer_thread, self._writer_thread = self._writer_thread, None
        if writer_thread is not None:
            writer_thread.join(timeout=timeout)

    def join(self, timeout: float | None = None) -> None:
        if self._decode_thread is not None:
            self._decode_thread.join(timeout=timeout)
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=timeout)

    def _run_decode(self) -> None:
        try:
            demuxer = AudioDemuxer(SocketByteSource(self._sock))
            self.codec_name = AUDIO_CODEC_NAMES[demuxer.codec_id]
            decoder = AudioDecoder(self.codec_name)
            with self._stats_lock:
                self._started_at = time.perf_counter()
            self._format = (decoder.sample_rate, decoder.channels)
            self._format_ready.set()
            for packet in demuxer:
                if self._stop.is_set():
                    return
                with self._stats_lock:
                    self._packets += 1
                for chunk in decoder.decode(packet):
                    self._buffer.put(chunk)
            decoder.close()
        except (ConnectionClosed, OSError) as exc:
            if not self._stop.is_set():
                self._on_error(exc)
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            if not self._stop.is_set():
                log.exception("audio worker failed")
                self._on_error(exc)
        finally:
            # Wake a writer still waiting for the first chunk/format, and
            # let it drain out instead of blocking forever, however this
            # thread ended.
            self._buffer.close()
            self._format_ready.set()

    def _run_writer(self) -> None:
        self._format_ready.wait()
        audio_format = self._format
        if audio_format is None:
            return
        try:
            self._sink.start(*audio_format)
        except Exception as exc:
            if not self._stop.is_set():
                log.exception("audio sink failed to start")
                self._on_error(exc)
            # The decoder has nowhere to send chunks any more; stop it too.
            self._stop.set()
            self._buffer.close()
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RD)
            return
        try:
            while True:
                chunk = self._buffer.get(timeout=0.2)
                if chunk is None:
                    if self._buffer.closed:
                        return
                    continue
                now = time.perf_counter()
                with self._stats_lock:
                    self._chunks += 1
                    self._bytes_written += len(chunk.data)
                    if self._first_chunk_at is None:
                        self._first_chunk_at = now
                self._sink.write(chunk)
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            if not self._stop.is_set():
                log.exception("audio sink write failed")
                self._on_error(exc)
        finally:
            self._sink.flush()
            self._sink.close()
