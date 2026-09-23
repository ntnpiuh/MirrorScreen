"""Audio framing and bounded-buffer tests."""

from __future__ import annotations

import socket
import threading
import time

import av
import numpy as np
import pytest

from mirror_screen.audio import (
    AudioChunk,
    AudioDecoder,
    AudioStats,
    AudioWorker,
    BoundedAudioBuffer,
)
from mirror_screen.audio_sink import audio_output_devices, default_audio_output_id
from mirror_screen.errors import ConnectionClosed, ProtocolError
from mirror_screen.protocol.framing import AudioDemuxer, MediaPacket
from mirror_screen.protocol.io import MemoryByteSource

from .helpers import audio_codec_header, media_header


def test_audio_demuxer_reads_codec_and_packets():
    stream = audio_codec_header("opus") + media_header(3, pts_us=42) + b"abc"
    demuxer = AudioDemuxer(MemoryByteSource(stream))

    packet = demuxer.read_packet()
    assert demuxer.codec_id is not None
    assert packet.payload == b"abc"
    assert packet.pts_us == 42


def test_audio_demuxer_rejects_unknown_codec():
    with pytest.raises(ProtocolError, match="unsupported audio codec"):
        AudioDemuxer(MemoryByteSource((0x12345678).to_bytes(4, "big")))


def test_audio_demuxer_reports_truncated_packet():
    demuxer = AudioDemuxer(
        MemoryByteSource(audio_codec_header() + media_header(4) + b"ab")
    )
    with pytest.raises(ConnectionClosed):
        demuxer.read_packet()


def test_audio_buffer_drops_oldest_chunk_when_full():
    buffer = BoundedAudioBuffer(max_chunks=2)
    chunks = [AudioChunk(bytes([index]), 48_000, 2) for index in range(3)]
    for chunk in chunks:
        buffer.put(chunk)

    assert buffer.dropped == 1
    first = buffer.get(0)
    second = buffer.get(0)
    assert first is not None and first.data == b"\x01"
    assert second is not None and second.data == b"\x02"
    assert buffer.get(0) is None
    assert buffer.underruns == 1


def test_audio_buffer_stats_are_snapshot_consistent():
    buffer = BoundedAudioBuffer(max_chunks=1)
    buffer.put(AudioChunk(b"a", 48_000, 2))
    buffer.put(AudioChunk(b"b", 48_000, 2))
    assert buffer.stats() == (1, 0)
    chunk = buffer.get(0)
    assert chunk is not None and chunk.data == b"b"
    assert buffer.get(0) is None
    assert buffer.stats() == (1, 1)


def test_audio_stats_defaults_are_zero():
    assert AudioStats() == AudioStats(
        packets=0,
        chunks=0,
        bytes_written=0,
        dropped=0,
        underruns=0,
        startup_ms=0.0,
    )


def test_audio_decoder_applies_config_packet_as_extradata():
    """The AAC/FLAC AudioSpecificConfig must reach the decoder, not be dropped."""
    decoder = AudioDecoder("aac")
    config_packet = MediaPacket(payload=b"\x12\x10", pts_us=0, config=True, key_frame=False)

    assert decoder.decode(config_packet) == []
    assert decoder._ensure_context().extradata == b"\x12\x10"


def _encode_silence_as_opus(num_frames: int = 20) -> bytes:
    """Real Opus packets an :class:`AudioDecoder` can actually decode."""
    ctx = av.CodecContext.create("opus", "w")
    ctx.sample_rate = 48_000
    ctx.format = av.AudioFormat("s16")
    ctx.layout = "stereo"
    ctx.open()

    encoded: list[bytes] = []
    for _ in range(num_frames):
        samples = np.zeros((2, 960), dtype=np.int16)
        frame = av.AudioFrame.from_ndarray(samples, format="s16p", layout="stereo")
        frame.sample_rate = 48_000
        encoded += [bytes(packet) for packet in ctx.encode(frame)]
    encoded += [bytes(packet) for packet in ctx.encode(None)]

    stream = audio_codec_header("opus")
    for payload in encoded:
        stream += media_header(len(payload)) + payload
    return stream


class _RecordingSink:
    """Records every call and the thread it was made from."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.threads: set[int] = set()

    def start(self, sample_rate: int, channels: int) -> None:
        self.calls.append(("start", sample_rate, channels))
        self.threads.add(threading.get_ident())

    def write(self, chunk: AudioChunk) -> None:
        self.calls.append(("write", len(chunk.data)))
        self.threads.add(threading.get_ident())

    def flush(self) -> None:
        self.calls.append(("flush",))
        self.threads.add(threading.get_ident())

    def close(self) -> None:
        self.calls.append(("close",))
        self.threads.add(threading.get_ident())


def test_audio_worker_decodes_and_drives_the_sink_from_one_thread():
    """The sink must see a linear start/write.../flush/close from one thread.

    A sink created and driven from more than one thread (or written to before
    it starts, or left unflushed) is exactly the bug this test guards
    against: Qt audio backends are not safe to use across threads.
    """
    server_sock, client_sock = socket.socketpair()
    server_sock.sendall(_encode_silence_as_opus())
    server_sock.shutdown(socket.SHUT_WR)
    try:
        sink = _RecordingSink()
        worker = AudioWorker(client_sock, sink)
        worker.start()
        worker.join(timeout=5)

        assert sink.calls[0][0] == "start"
        assert sink.calls[-2][0] == "flush"
        assert sink.calls[-1][0] == "close"
        assert any(call[0] == "write" for call in sink.calls)
        assert len(sink.threads) == 1, "the sink was touched from more than one thread"

        stats = worker.stats()
        assert stats.packets > 0
        assert stats.chunks > 0
    finally:
        server_sock.close()


def test_audio_worker_buffer_absorbs_a_slow_sink_instead_of_blocking_decode():
    """The bounded buffer must decouple decode from a slow/blocking sink.

    If decode and playback shared one synchronous put-then-get step (the bug
    this guards against), a slow sink would stall decoding instead of losing
    the oldest buffered chunk, and `dropped` would never move.
    """

    class SlowSink(_RecordingSink):
        def write(self, chunk: AudioChunk) -> None:
            time.sleep(0.03)
            super().write(chunk)

    server_sock, client_sock = socket.socketpair()
    server_sock.sendall(_encode_silence_as_opus(num_frames=40))
    server_sock.shutdown(socket.SHUT_WR)
    try:
        buffer = BoundedAudioBuffer(max_chunks=1)
        worker = AudioWorker(client_sock, SlowSink(), buffer=buffer)
        worker.start()
        worker.join(timeout=10)

        stats = worker.stats()
        assert stats.packets > stats.chunks, "decode should race ahead of the slow sink"
        assert stats.dropped > 0
    finally:
        server_sock.close()


def test_audio_output_discovery_has_a_headless_fallback():
    devices = audio_output_devices()
    default_id = default_audio_output_id()
    if devices:
        assert default_id in {device_id for device_id, _ in devices}
    else:
        assert default_id is None