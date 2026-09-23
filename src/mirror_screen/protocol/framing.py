"""Demux the video/audio sockets into packets.

Wire format (scrcpy 4.1, verified against ``app/src/demuxer.c`` and
``server/.../device/Streamer.java``):

* The video socket starts with a 12-byte *session packet* describing the
  current capture size (bytes 4..8 = width, 8..12 = height, byte 3 bit 0 =
  "client resized"). It is re-sent whenever the capture session restarts,
  which happens on rotation or folding.
* Every subsequent packet is prefixed by a 12-byte header::

      byte 0..8            byte 8..12
      pts_and_flags (u64)  packet_size (u32)

  with the flags packed into the top bits of the u64. When the top bit is set
  the packet is a session packet instead, and carries no payload.

Older protocol revisions (scrcpy 2.x) sent a codec id instead of a session
packet; :meth:`VideoDemuxer.start` detects that shape and reports a clear
version mismatch rather than misparsing it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ..errors import ConnectionClosed, ProtocolError
from .const import (
    DEVICE_NAME_FIELD_LENGTH,
    MAX_PACKET_SIZE,
    PACKET_FLAG_CONFIG,
    PACKET_FLAG_KEY_FRAME,
    PACKET_HEADER_SIZE,
    PACKET_PTS_MASK,
)
from .io import ByteSource, read_u32, read_u64


@dataclass(frozen=True, slots=True)
class StreamMeta:
    """Codec and initial frame size of a stream."""

    codec_id: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class SessionPacket:
    """A new capture session started (rotation, folding, resize)."""

    width: int
    height: int
    client_resized: bool


@dataclass(frozen=True, slots=True)
class MediaPacket:
    """One encoded access unit straight out of ``MediaCodec``."""

    payload: bytes
    pts_us: int
    config: bool
    key_frame: bool


Packet = SessionPacket | MediaPacket


def read_device_name(source: ByteSource) -> str:
    """Read the fixed 64-byte, NUL-padded device name field."""
    raw = source.read_exact(DEVICE_NAME_FIELD_LENGTH)
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def parse_session_header(header: bytes) -> SessionPacket:
    """Parse a 12-byte session packet."""
    if len(header) != PACKET_HEADER_SIZE:
        raise ProtocolError(f"bad session header size: {len(header)}")
    return SessionPacket(
        width=read_u32(header, 4),
        height=read_u32(header, 8),
        client_resized=bool(header[3] & 1),
    )


def parse_media_header(header: bytes) -> tuple[int, int, bool, bool]:
    """Parse a 12-byte media header.

    Returns:
        ``(pts_us, payload_size, is_config, is_key_frame)``.
    """
    if len(header) != PACKET_HEADER_SIZE:
        raise ProtocolError(f"bad packet header size: {len(header)}")

    pts_flags = read_u64(header, 0)
    payload_size = read_u32(header, 8)

    if payload_size == 0:
        raise ProtocolError("invalid packet length: 0")
    if payload_size > MAX_PACKET_SIZE:
        raise ProtocolError(f"packet too large: {payload_size} bytes")

    is_config = bool(pts_flags & PACKET_FLAG_CONFIG)
    pts_us = 0 if is_config else (pts_flags & PACKET_PTS_MASK)
    return pts_us, payload_size, is_config, bool(pts_flags & PACKET_FLAG_KEY_FRAME)


def is_session_header(header: bytes) -> bool:
    """True if the top bit of the header marks a session packet."""
    return bool(header[0] & 0x80)


class VideoDemuxer:
    """Turn a connected video socket into a stream of :class:`Packet`."""

    def __init__(self, source: ByteSource) -> None:
        self._source = source
        self._session: SessionPacket | None = None

    def start(self) -> SessionPacket:
        """Consume the stream metadata and return the initial session.

        Raises:
            ProtocolError: if the stream does not start with a session packet,
                which most likely indicates a server version mismatch.
        """
        header = self._source.read_exact(PACKET_HEADER_SIZE)

        if not is_session_header(header):
            # scrcpy 2.x sent "codec id (u32), width (u32), height (u32)".
            codec_id = read_u32(header, 0)
            width = read_u32(header, 4)
            height = read_u32(header, 8)
            raise ProtocolError(
                "unexpected video stream metadata: this looks like the "
                f"scrcpy 2.x protocol (codec=0x{codec_id:08x}, {width}x{height}), "
                "but Mirror Screen speaks the scrcpy 4.x protocol. The client "
                "and the on-device server version must match exactly."
            )

        session = parse_session_header(header)
        if session.width == 0 or session.height == 0:
            raise ProtocolError(
                f"invalid session video size: {session.width}x{session.height}"
            )

        self._session = session
        return session

    def read_packet(self) -> Packet:
        """Read the next packet.

        Raises:
            ConnectionClosed: at end of stream.
            ProtocolError: on malformed framing.
        """
        header = self._source.read_exact(PACKET_HEADER_SIZE)

        if is_session_header(header):
            session = parse_session_header(header)
            self._session = session
            return session

        pts_us, payload_size, is_config, is_key_frame = parse_media_header(header)
        payload = self._source.read_exact(payload_size)
        return MediaPacket(
            payload=payload,
            pts_us=pts_us,
            config=is_config,
            key_frame=is_key_frame,
        )

    def __iter__(self) -> Iterator[Packet]:
        """Yield packets until the stream ends.

        Use :meth:`read_packet` directly when a truncated stream must be
        reported as an error rather than treated as a clean end.
        """
        while True:
            try:
                yield self.read_packet()
            except ConnectionClosed:
                return


def read_audio_codec_id(source: ByteSource) -> int:
    """Read the audio stream codec id (the audio socket has no session packet)."""
    return read_u32(source.read_exact(4))
