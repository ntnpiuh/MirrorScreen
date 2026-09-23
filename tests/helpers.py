"""Shared helpers for the tests."""

from __future__ import annotations

import struct

from mirror_screen.protocol.const import (
    PACKET_FLAG_CONFIG,
    PACKET_FLAG_KEY_FRAME,
    PACKET_FLAG_SESSION,
)


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
