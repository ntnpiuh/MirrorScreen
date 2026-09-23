"""Framing tests: the video stream must be parsed exactly as the server writes it."""

from __future__ import annotations

import pytest

from mirror_screen.errors import ConnectionClosed, ProtocolError
from mirror_screen.protocol.const import DEVICE_NAME_FIELD_LENGTH
from mirror_screen.protocol.framing import (
    MediaPacket,
    SessionPacket,
    VideoDemuxer,
    parse_media_header,
    parse_session_header,
    read_device_name,
)
from mirror_screen.protocol.io import MemoryByteSource, SocketByteSource

from .helpers import media_header, session_header


def test_session_header_layout():
    """The session packet carries the size in bytes 4..12 of the header."""
    header = session_header(1080, 2340, client_resized=True)
    assert len(header) == 12
    assert header[0] == 0x80  # top bit marks a session packet
    assert header[3] & 1 == 1  # client-resized flag

    session = parse_session_header(header)
    assert session.width == 1080
    assert session.height == 2340
    assert session.client_resized is True


def test_media_header_flags():
    """Config and key-frame flags live in the top bits of the pts field."""
    header = media_header(1234, pts_us=999_000, key_frame=True)
    pts_us, size, is_config, is_key = parse_media_header(header)
    assert (pts_us, size, is_config, is_key) == (999_000, 1234, False, True)

    config_header = media_header(64, config=True, key_frame=True)
    pts_us, size, is_config, is_key = parse_media_header(config_header)
    assert is_config is True
    # A config packet is not media data, so it must not be given a timestamp.
    assert pts_us == 0
    assert is_key is False
    assert size == 64


def test_media_header_rejects_zero_length():
    with pytest.raises(ProtocolError, match="length"):
        parse_media_header(media_header(0))


def test_media_header_rejects_absurd_length():
    with pytest.raises(ProtocolError, match="too large"):
        parse_media_header(media_header(64 * 1024 * 1024))


def test_device_name_is_nul_padded():
    raw = b"Pixel 8 Pro" + bytes(DEVICE_NAME_FIELD_LENGTH - len(b"Pixel 8 Pro"))
    assert len(raw) == DEVICE_NAME_FIELD_LENGTH
    assert read_device_name(MemoryByteSource(raw)) == "Pixel 8 Pro"


def test_demuxer_reads_a_stream_in_order():
    stream = b"".join(
        [
            session_header(1920, 1080),
            media_header(10, config=True) + b"c" * 10,
            media_header(4, pts_us=1000, key_frame=True) + b"aaaa",
            media_header(3, pts_us=2000) + b"bbb",
            session_header(1080, 1920, client_resized=True),
            media_header(2, pts_us=3000) + b"cc",
        ]
    )

    demuxer = VideoDemuxer(MemoryByteSource(stream))
    first = demuxer.start()
    assert isinstance(first, SessionPacket)
    assert (first.width, first.height) == (1920, 1080)

    packets = list(demuxer)
    assert isinstance(packets[0], MediaPacket)
    assert packets[0].config is True
    assert packets[0].payload == b"c" * 10

    assert packets[1].key_frame is True
    assert packets[1].pts_us == 1000
    assert packets[1].payload == b"aaaa"

    assert packets[2].key_frame is False
    assert packets[2].pts_us == 2000

    # A session packet may appear at any point; it means "size changed".
    assert isinstance(packets[3], SessionPacket)
    assert (packets[3].width, packets[3].height) == (1080, 1920)
    assert packets[3].client_resized is True

    assert packets[4].payload == b"cc"


def test_demuxer_reports_a_version_mismatch_clearly():
    """scrcpy 2.x sent "codec id, width, height" instead of a session packet."""
    legacy = (0x68323634).to_bytes(4, "big") + (1080).to_bytes(4, "big") + (
        1920
    ).to_bytes(4, "big")

    with pytest.raises(ProtocolError, match="scrcpy 2.x"):
        VideoDemuxer(MemoryByteSource(legacy)).start()


def test_demuxer_rejects_a_zero_sized_session():
    with pytest.raises(ProtocolError, match="invalid session video size"):
        VideoDemuxer(MemoryByteSource(session_header(0, 0))).start()


def test_demuxer_reassembles_split_socket_reads():
    """TCP does not preserve write boundaries, so headers can arrive in pieces."""
    import socket
    import threading

    stream = session_header(640, 480) + media_header(5, pts_us=42) + b"hello"
    sender, receiver = socket.socketpair()
    try:

        def dribble() -> None:
            for byte in stream:
                sender.sendall(bytes([byte]))
            sender.shutdown(socket.SHUT_WR)

        threading.Thread(target=dribble, daemon=True).start()

        demuxer = VideoDemuxer(SocketByteSource(receiver))
        first = demuxer.start()
        assert (first.width, first.height) == (640, 480)

        packet = demuxer.read_packet()
        assert isinstance(packet, MediaPacket)
        assert packet.payload == b"hello"
        assert packet.pts_us == 42

        # After the writer closes, the next read must report the end of stream.
        with pytest.raises(ConnectionClosed):
            demuxer.read_packet()
    finally:
        sender.close()
        receiver.close()


def test_iteration_stops_at_end_of_stream():
    stream = session_header(640, 480) + media_header(2, pts_us=1) + b"ok"
    demuxer = VideoDemuxer(MemoryByteSource(stream))
    demuxer.start()
    packets = list(demuxer)
    assert len(packets) == 1
    assert packets[0].payload == b"ok"
