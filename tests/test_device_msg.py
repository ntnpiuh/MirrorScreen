"""Device message tests (the control socket's inbound direction)."""

from __future__ import annotations

import struct

import pytest

from mirror_screen.errors import ProtocolError
from mirror_screen.protocol.device_msg import (
    AckClipboardMessage,
    ClipboardMessage,
    DeviceMessageParser,
    IncompleteMessage,
    UhidOutputMessage,
    deserialize,
)


def test_clipboard_message():
    payload = bytes([0]) + struct.pack(">I", 5) + b"hello"
    message, consumed = deserialize(payload)
    assert isinstance(message, ClipboardMessage)
    assert message.text == "hello"
    assert consumed == len(payload)


def test_ack_clipboard_message():
    payload = bytes([1]) + struct.pack(">Q", 123456789)
    message, consumed = deserialize(payload)
    assert isinstance(message, AckClipboardMessage)
    assert message.sequence == 123456789
    assert consumed == 9


def test_uhid_output_message():
    payload = bytes([2]) + struct.pack(">HH", 3, 4) + b"\x01\x02\x03\x04"
    message, consumed = deserialize(payload)
    assert isinstance(message, UhidOutputMessage)
    assert message.device_id == 3
    assert message.data == b"\x01\x02\x03\x04"
    assert consumed == 9


def test_partial_message_needs_more_data():
    with pytest.raises(IncompleteMessage):
        deserialize(bytes([0]) + struct.pack(">I", 5) + b"hel")

    with pytest.raises(IncompleteMessage):
        deserialize(b"")


def test_unknown_type_is_an_error():
    with pytest.raises(ProtocolError, match="unknown device message type"):
        deserialize(bytes([99]))


def test_parser_handles_split_and_batched_messages():
    parser = DeviceMessageParser()
    first = bytes([0]) + struct.pack(">I", 5) + b"hello"
    second = bytes([1]) + struct.pack(">Q", 9)

    # Split the first message across three reads.
    assert parser.feed(first[:2]) == []
    assert parser.feed(first[2:6]) == []
    messages = parser.feed(first[6:] + second)
    assert len(messages) == 2
    assert isinstance(messages[0], ClipboardMessage)
    assert messages[0].text == "hello"
    assert isinstance(messages[1], AckClipboardMessage)
    assert messages[1].sequence == 9


def test_parser_keeps_a_trailing_partial_message():
    parser = DeviceMessageParser()
    parser.feed(bytes([2]) + struct.pack(">HH", 1, 10) + b"abc")
    messages = parser.feed(b"defghij")
    assert len(messages) == 1
    assert messages[0].data == b"abcdefghij"


def test_parser_rejects_oversized_buffers():
    parser = DeviceMessageParser(max_buffer=16)
    with pytest.raises(ProtocolError, match="overflow"):
        parser.feed(b"\x00" * 32)
