"""Control message tests: byte-for-byte layouts must match the server's reader."""

from __future__ import annotations

import struct

import pytest

from mirror_screen.protocol import control
from mirror_screen.protocol.const import ACTION_DOWN, ACTION_UP, META_SHIFT_ON


def test_inject_keycode_layout():
    payload = control.inject_keycode(ACTION_DOWN, keycode=3, repeat=0, metastate=0)
    assert len(payload) == 14
    assert payload[0] == control.ControlMessageType.INJECT_KEYCODE
    action, keycode, repeat, meta = struct.unpack_from(">BIII", payload, 1)
    assert (action, keycode, repeat, meta) == (ACTION_DOWN, 3, 0, 0)


def test_inject_keycode_carries_modifiers():
    payload = control.inject_keycode(ACTION_UP, keycode=29, metastate=META_SHIFT_ON)
    assert struct.unpack_from(">BIII", payload, 1) == (ACTION_UP, 29, 0, META_SHIFT_ON)


def test_inject_touch_layout_is_32_bytes():
    payload = control.inject_touch(
        ACTION_DOWN,
        0xFFFFFFFFFFFFFFFF,
        100,
        200,
        1080,
        1920,
        pressure=1.0,
        action_button=1,
        buttons=1,
    )
    assert len(payload) == 32
    assert payload[0] == control.ControlMessageType.INJECT_TOUCH_EVENT
    assert payload[1] == ACTION_DOWN

    pointer_id = struct.unpack_from(">Q", payload, 2)[0]
    x, y, width, height = struct.unpack_from(">iiHH", payload, 10)
    (pressure,) = struct.unpack_from(">H", payload, 22)
    action_button, buttons = struct.unpack_from(">II", payload, 24)

    assert pointer_id == 0xFFFFFFFFFFFFFFFF  # POINTER_ID_MOUSE
    assert (x, y) == (100, 200)
    assert (width, height) == (1080, 1920)
    assert pressure == 0xFFFF  # full pressure
    assert (action_button, buttons) == (1, 1)


def test_inject_touch_clamps_pressure():
    payload = control.inject_touch(ACTION_UP, 1, 0, 0, 10, 10, pressure=4.0)
    (pressure,) = struct.unpack_from(">H", payload, 22)
    assert pressure == 0xFFFF

    payload = control.inject_touch(ACTION_UP, 1, 0, 0, 10, 10, pressure=-1.0)
    (pressure,) = struct.unpack_from(">H", payload, 22)
    assert pressure == 0


def test_inject_scroll_layout_is_21_bytes():
    payload = control.inject_scroll(
        10, 20, 1080, 1920, hscroll=-1.0, vscroll=16.0, buttons=1
    )
    assert len(payload) == 21
    assert payload[0] == control.ControlMessageType.INJECT_SCROLL_EVENT

    x, y, width, height = struct.unpack_from(">iiHH", payload, 1)
    hscroll, vscroll = struct.unpack_from(">hh", payload, 13)
    (buttons,) = struct.unpack_from(">I", payload, 17)

    assert (x, y, width, height) == (10, 20, 1080, 1920)
    # The server divides by 16 and clamps to [-1, 1], so 16 is the maximum.
    assert vscroll == 0x7FFF
    assert hscroll < 0
    assert buttons == 1


def test_scroll_is_clamped_to_the_documented_range():
    payload = control.inject_scroll(0, 0, 10, 10, vscroll=1000.0)
    (vscroll,) = struct.unpack_from(">h", payload, 15)
    assert vscroll == 0x7FFF


def test_set_clipboard_layout():
    payload = control.set_clipboard(7, "hi", paste=True)
    assert payload[0] == control.ControlMessageType.SET_CLIPBOARD
    (sequence,) = struct.unpack_from(">Q", payload, 1)
    paste = payload[9]
    (length,) = struct.unpack_from(">I", payload, 10)
    assert sequence == 7
    assert paste == 1
    assert length == 2
    assert payload[14:] == b"hi"
    assert len(payload) == 14 + 2


def test_simple_messages_are_two_bytes():
    assert control.back_or_screen_on() == bytes([4, 0])
    assert control.get_clipboard() == bytes([8, 0])
    assert control.set_display_power(True) == bytes([10, 1])
    assert control.set_display_power(False) == bytes([10, 0])
    assert control.rotate_device() == bytes([11])
    assert control.collapse_panels() == bytes([7])


def test_start_app_uses_a_one_byte_length():
    payload = control.start_app("com.android.settings")
    assert payload[0] == control.ControlMessageType.START_APP
    assert payload[1] == len("com.android.settings")
    assert payload[2:] == b"com.android.settings"


def test_resize_display_layout():
    payload = control.resize_display(1080, 1920)
    assert payload == bytes([21]) + struct.pack(">HH", 1080, 1920)


def test_uhid_create_layout():
    payload = control.uhid_create(1, 0x2E8A, 0x0005, "Mouse", b"\x05\x01")
    assert payload[0] == control.ControlMessageType.UHID_CREATE
    device_id, vendor, product = struct.unpack_from(">HHH", payload, 1)
    assert (device_id, vendor, product) == (1, 0x2E8A, 0x0005)
    name_length = payload[7]
    assert name_length == len("Mouse")
    assert payload[8 : 8 + name_length] == b"Mouse"
    (description_length,) = struct.unpack_from(
        ">H", payload, 8 + name_length
    )
    assert description_length == 2
    assert payload[10 + name_length :] == b"\x05\x01"


def test_tap_sends_a_down_and_up_pair():
    payloads = control.tap(5, 6, 100, 200)
    assert len(payloads) == 2
    assert payloads[0][1] == ACTION_DOWN
    assert payloads[1][1] == ACTION_UP
    # The release must report no pressure and no buttons.
    (pressure,) = struct.unpack_from(">H", payloads[1], 22)
    action_button, buttons = struct.unpack_from(">II", payloads[1], 24)
    assert (pressure, action_button, buttons) == (0, 0, 0)


def test_inject_text_uses_a_four_byte_length():
    payload = control.inject_text("héllo")
    assert payload[0] == control.ControlMessageType.INJECT_TEXT
    (length,) = struct.unpack_from(">I", payload, 1)
    assert length == len("héllo".encode())
    assert payload[5:].decode() == "héllo"


def test_inject_text_is_truncated_to_the_protocol_limit():
    payload = control.inject_text("x" * 1000)
    (length,) = struct.unpack_from(">I", payload, 1)
    assert length == control.INJECT_TEXT_MAX_LENGTH


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.0, 0), (1.0, 0x7FFF), (-1.0, -0x7FFF), (2.0, 0x7FFF), (-2.0, -0x7FFF)],
)
def test_i16_fixed_point_never_wraps(value, expected):
    from mirror_screen.protocol.io import float_to_i16fp

    assert float_to_i16fp(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [(0.0, 0), (1.0, 0xFFFF), (2.0, 0xFFFF), (-1.0, 0)]
)
def test_u16_fixed_point_clamps(value, expected):
    from mirror_screen.protocol.io import float_to_u16fp

    assert float_to_u16fp(value) == expected
