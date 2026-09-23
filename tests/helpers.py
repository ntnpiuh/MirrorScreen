"""Shared helpers for the tests."""

from __future__ import annotations

import struct

from mirror_screen.protocol.const import (
    PACKET_FLAG_CONFIG,
    PACKET_FLAG_KEY_FRAME,
    PACKET_FLAG_SESSION,
)


def codec_header(name: str = "h264") -> bytes:
    """The 4-byte codec id that precedes the first session packet."""
    from mirror_screen.protocol.const import VIDEO_CODECS

    return VIDEO_CODECS[name].to_bytes(4, "big")


def session_header(width: int, height: int, *, client_resized: bool = False) -> bytes:
    """Build a 12-byte session packet."""
    flags = (PACKET_FLAG_SESSION >> 32) | (1 if client_resized else 0)
    return struct.pack(">III", flags, width, height)


def media_header(
    size: int, pts_us: int = 0, *, config: bool = False, key_frame: bool = False
) -> bytes:
    """Build a 12-byte media packet header."""
    flags = PACKET_FLAG_CONFIG if config else pts_us
    if key_frame and not config:
        flags |= PACKET_FLAG_KEY_FRAME
    return struct.pack(">QI", flags, size)


def audio_codec_header(name: str = "opus") -> bytes:
    """The codec id that prefixes a scrcpy audio stream."""
    from mirror_screen.protocol.const import AUDIO_CODECS

    return AUDIO_CODECS[name].to_bytes(4, "big")
