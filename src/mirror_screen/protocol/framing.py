"""Demux the video/audio sockets into packets.

Wire format (scrcpy 4.x, confirmed against a real device):

* The video socket begins with the stream metadata, which is a **4-byte codec
  id followed by a 12-byte session packet**:

      68 32 36 34   "h264"
      80 00 00 00   session-packet flag (+ bit 0 = client resized)
      00 00 04 38   width  (u32, 1080 here)
      00 00 09 24   height (u32, 2340 here)

  (Both come from ``send_stream_meta=true``.)
* A session packet is re-sent whenever the capture session restarts, which
  happens on rotation or folding.
* Every subsequent packet is prefixed by a 12-byte header::

      byte 0..8            byte 8..12
      pts_and_flags (u64)  packet_size (u32)

  with the flags packed into the top bits of the u64. When the top bit is set
  the packet is a session packet instead, and carries no payload.

:meth:`VideoDemuxer.start` accepts the metadata with or without the codec id
and rejects the scrcpy 2.x shape (codec id, then a bare width/height pair with
no session flag) with an explicit version-mismatch error.
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
    AUDIO_CODEC_NAMES,
    VIDEO_CODEC_NAMES,
)
from .io import ByteSource, read_u32, read_u64


@dataclass(frozen=True, slots=True)
class StreamMeta:
    """Codec and initial frame size of a stream."""

    width: int
    height: int
    codec_id: int | None = None
    client_resized: bool = False

    @property
    def codec_name(self) -> str | None:
        """The codec as a name (``h264``, ``h265``, ...), if recognised."""
        if self.codec_id is None:
            return None
        return VIDEO_CODEC_NAMES.get(self.codec_id)


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
        #: Codec id reported by the device, if it sent one.
        self.codec_id: int | None = None

    def start(self) -> StreamMeta:
        """Consume the stream metadata and return it.

        Raises:
            ProtocolError: if the stream does not start with a session packet,
                which most likely indicates a server version mismatch.
        """
        # The stream metadata starts with a 4-byte codec id, then a session
        # packet holding the capture size. Accept a bare session packet too, in
        # case a server emits the metadata without the codec id.
        first = self._source.read_exact(4)
        codec_id: int | None = None
        if read_u32(first) in VIDEO_CODEC_NAMES:
            codec_id = read_u32(first)
            header = self._source.read_exact(PACKET_HEADER_SIZE)
        else:
            header = first + self._source.read_exact(PACKET_HEADER_SIZE - 4)

        if not is_session_header(header):
            # scrcpy 2.x sent "codec id (u32), width (u32), height (u32)" and
            # had no session packet at all.
            width = read_u32(header, 0)
            height = read_u32(header, 4)
            raise ProtocolError(
                "unexpected video stream metadata: this looks like the "
                f"scrcpy 2.x protocol (codec=0x{codec_id or 0:08x}, "
                f"{width}x{height}), but Mirror Screen speaks the scrcpy 4.x "
                "protocol. The client and the on-device server version must "
                "match exactly."
            )

        session = parse_session_header(header)
        if session.width == 0 or session.height == 0:
            raise ProtocolError(
                f"invalid session video size: {session.width}x{session.height}"
            )

        self._session = session
        self.codec_id = codec_id
        return StreamMeta(
            width=session.width,
            height=session.height,
            codec_id=codec_id,
            client_resized=session.client_resized,
        )

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
    codec_id = read_u32(source.read_exact(4))
    if codec_id not in AUDIO_CODEC_NAMES:
        raise ProtocolError(f"unsupported audio codec id: 0x{codec_id:08x}")
    return codec_id


class AudioDemuxer:
    """Turn the audio socket into bounded encoded media packets."""

    def __init__(self, source: ByteSource) -> None:
        self._source = source
        self.codec_id = read_audio_codec_id(source)

    def read_packet(self) -> MediaPacket:
        """Read one encoded audio access unit with strict bounds checks."""
        header = self._source.read_exact(PACKET_HEADER_SIZE)
        pts_us, payload_size, is_config, is_key_frame = parse_media_header(header)
        return MediaPacket(
            payload=self._source.read_exact(payload_size),
            pts_us=pts_us,
            config=is_config,
            key_frame=is_key_frame,
        )

    def __iter__(self) -> Iterator[MediaPacket]:
        while True:
            try:
                yield self.read_packet()
            except ConnectionClosed:
                return
