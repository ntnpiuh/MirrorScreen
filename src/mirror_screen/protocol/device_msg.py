"""Parse device messages (device -> client) from the control socket.

Verified against ``app/src/device_msg.h``:

* ``CLIPBOARD``     : u8 type, u32 length, UTF-8 text
* ``ACK_CLIPBOARD`` : u8 type, u64 sequence
* ``UHID_OUTPUT``   : u8 type, u16 id, u16 size, bytes

The control socket is a byte stream, so messages may be split across reads or
batched together. :class:`DeviceMessageParser` buffers partial data and yields
complete messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from ..errors import ProtocolError
from .io import read_u32, read_u64


class DeviceMessageType(IntEnum):
    CLIPBOARD = 0
    ACK_CLIPBOARD = 1
    UHID_OUTPUT = 2


#: type (1 byte) + length (4 bytes)
TEXT_MAX_LENGTH = (1 << 18) - 5


class IncompleteMessage(Exception):
    """Not an error: more bytes are needed to complete the message."""


@dataclass(frozen=True, slots=True)
class ClipboardMessage:
    text: str


@dataclass(frozen=True, slots=True)
class AckClipboardMessage:
    sequence: int


@dataclass(frozen=True, slots=True)
class UhidOutputMessage:
    device_id: int
    data: bytes


DeviceMessage = ClipboardMessage | AckClipboardMessage | UhidOutputMessage


def deserialize(data: bytes) -> tuple[DeviceMessage, int]:
    """Parse one message from the front of ``data``.

    Returns:
        ``(message, bytes_consumed)``.

    Raises:
        IncompleteMessage: if ``data`` does not yet hold a whole message.
        ProtocolError: if the message is malformed or of an unknown type.
    """
    if not data:
        raise IncompleteMessage

    msg_type = data[0]

    if msg_type == DeviceMessageType.CLIPBOARD:
        if len(data) < 5:
            raise IncompleteMessage
        length = read_u32(data, 1)
        if length > TEXT_MAX_LENGTH:
            raise ProtocolError(f"clipboard text too long: {length}")
        if len(data) < 5 + length:
            raise IncompleteMessage
        text = data[5 : 5 + length].decode("utf-8", errors="replace")
        return ClipboardMessage(text), 5 + length

    if msg_type == DeviceMessageType.ACK_CLIPBOARD:
        if len(data) < 9:
            raise IncompleteMessage
        return AckClipboardMessage(read_u64(data, 1)), 9

    if msg_type == DeviceMessageType.UHID_OUTPUT:
        if len(data) < 5:
            raise IncompleteMessage
        device_id = (data[1] << 8) | data[2]
        size = (data[3] << 8) | data[4]
        if len(data) < 5 + size:
            raise IncompleteMessage
        return UhidOutputMessage(device_id, data[5 : 5 + size]), 5 + size

    raise ProtocolError(f"unknown device message type: {msg_type}")


class DeviceMessageParser:
    """Incremental parser for the control socket's inbound stream."""

    def __init__(self, max_buffer: int = 1 << 18) -> None:
        self._buffer = bytearray()
        self._max_buffer = max_buffer

    def feed(self, data: bytes) -> list[DeviceMessage]:
        """Add received bytes and return every message now complete."""
        self._buffer.extend(data)
        if len(self._buffer) > self._max_buffer:
            raise ProtocolError("device message buffer overflow")

        messages: list[DeviceMessage] = []
        while self._buffer:
            try:
                message, consumed = deserialize(bytes(self._buffer))
            except IncompleteMessage:
                break
            del self._buffer[:consumed]
            messages.append(message)
        return messages
