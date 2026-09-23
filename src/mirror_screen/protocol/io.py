"""Byte sources and big-endian pack/unpack helpers.

The demuxer reads from an abstract :class:`ByteSource` rather than a socket
directly, which keeps the parsing logic pure and unit-testable.
"""

from __future__ import annotations

import socket
import struct
from typing import Protocol

from ..errors import ConnectionClosed, ProtocolError


class ByteSource(Protocol):
    """A stream of bytes we can pull an exact number of bytes from."""

    def read_exact(self, n: int) -> bytes:
        """Return exactly ``n`` bytes, blocking as needed.

        Raises:
            ConnectionClosed: if the peer closed the stream before ``n`` bytes
                could be read.
        """


class SocketByteSource:
    """Read exact byte counts from a connected socket."""

    __slots__ = ("_sock", "_chunk")

    def __init__(self, sock: socket.socket, chunk_size: int = 1 << 16) -> None:
        self._sock = sock
        # Avoid tiny reads when a large payload is split across many segments.
        self._chunk = chunk_size

    def read_exact(self, n: int) -> bytes:
        if n == 0:
            return b""

        sock = self._sock
        if n <= self._chunk:
            # Fast path: a single recv() almost always satisfies the request,
            # because the server writes header+payload together.
            view = bytearray(n)
            got = sock.recv_into(view, n)
            if got == 0:
                raise ConnectionClosed("device closed the connection")
            if got == n:
                return bytes(view)
            return bytes(view[:got]) + self.read_exact(n - got)

        chunks: list[bytes] = []
        remaining = n
        while remaining:
            data = sock.recv(min(remaining, self._chunk))
            if not data:
                raise ConnectionClosed("device closed the connection")
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)


class MemoryByteSource:
    """Serve bytes from an in-memory buffer (used by tests and self-checks)."""

    def __init__(self, data: bytes) -> None:
        self._data = memoryview(data)
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    def read_exact(self, n: int) -> bytes:
        if n > self.remaining:
            raise ConnectionClosed(
                f"stream ended: wanted {n} bytes, {self.remaining} available"
            )
        out = bytes(self._data[self._pos : self._pos + n])
        self._pos += n
        return out


# --------------------------------------------------------------------------
# Big-endian helpers (the protocol is big-endian throughout).
# --------------------------------------------------------------------------
def read_u32(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def read_u64(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">Q", data, offset)[0]


def pack_u8(value: int) -> bytes:
    return struct.pack(">B", value & 0xFF)


def pack_u16(value: int) -> bytes:
    return struct.pack(">H", value & 0xFFFF)


def pack_u32(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def pack_i32(value: int) -> bytes:
    return struct.pack(">i", _clamp(value, -(2**31), 2**31 - 1))


def pack_u64(value: int) -> bytes:
    return struct.pack(">Q", value & 0xFFFFFFFFFFFFFFFF)


def float_to_u16fp(value: float) -> int:
    """Convert a float in ``[0, 1]`` to a 16-bit unsigned fixed-point value."""
    return _clamp(int(round(value * 0xFFFF)), 0, 0xFFFF)


def float_to_i16fp(value: float) -> int:
    """Convert a float in ``[-1, 1]`` to a 16-bit signed fixed-point value.

    scrcpy itself multiplies by 32768, which wraps to ``-32768`` for exactly
    ``1.0`` and silently inverts the value's sign. Scaling by 32767 and
    clamping symmetrically keeps the magnitude and the direction correct at
    both extremes.
    """
    return _clamp(int(round(value * 0x7FFF)), -0x7FFF, 0x7FFF)


def _clamp(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value


def read_length_prefixed_string(
    data: bytes, offset: int, length_size: int
) -> tuple[str, int]:
    """Read a length-prefixed UTF-8 string, returning ``(text, new_offset)``."""
    if length_size == 4:
        if offset + 4 > len(data):
            raise ProtocolError("truncated string length")
        length = read_u32(data, offset)
        offset += 4
    elif length_size == 1:
        if offset + 1 > len(data):
            raise ProtocolError("truncated string length")
        length = data[offset]
        offset += 1
    else:  # pragma: no cover - programmer error
        raise ValueError(f"unsupported length size: {length_size}")

    if offset + length > len(data):
        raise ProtocolError("truncated string payload")
    text = data[offset : offset + length].decode("utf-8", errors="replace")
    return text, offset + length
