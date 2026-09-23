"""Serialize control messages (client -> device).

Field layout verified against ``app/src/control_msg.c`` (client) and
``server/.../control/ControlMessageReader.java`` (server). Everything is
big-endian; the server reads through a ``DataInputStream``, so widths follow
Java's ``readUnsignedByte``/``readShort``/``readInt``/``readLong``.

All builders return the raw bytes to write on the control socket.
"""

from __future__ import annotations

from enum import IntEnum

from .const import ACTION_DOWN, ACTION_UP, BUTTON_PRIMARY
from .io import (
    float_to_i16fp,
    float_to_u16fp,
    pack_i32,
    pack_u16,
    pack_u32,
    pack_u64,
    pack_u8,
)

#: Longest text accepted by the server for a single INJECT_TEXT message.
INJECT_TEXT_MAX_LENGTH = 300
#: Longest clipboard text accepted by the server.
CLIPBOARD_TEXT_MAX_LENGTH = (1 << 18) - 14


class ControlMessageType(IntEnum):
    INJECT_KEYCODE = 0
    INJECT_TEXT = 1
    INJECT_TOUCH_EVENT = 2
    INJECT_SCROLL_EVENT = 3
    BACK_OR_SCREEN_ON = 4
    EXPAND_NOTIFICATION_PANEL = 5
    EXPAND_SETTINGS_PANEL = 6
    COLLAPSE_PANELS = 7
    GET_CLIPBOARD = 8
    SET_CLIPBOARD = 9
    SET_DISPLAY_POWER = 10
    ROTATE_DEVICE = 11
    UHID_CREATE = 12
    UHID_INPUT = 13
    UHID_DESTROY = 14
    OPEN_HARD_KEYBOARD_SETTINGS = 15
    START_APP = 16
    RESET_VIDEO = 17
    CAMERA_SET_TORCH = 18
    CAMERA_ZOOM_IN = 19
    CAMERA_ZOOM_OUT = 20
    RESIZE_DISPLAY = 21
    SCAN_FILE = 22


class CopyKey(IntEnum):
    """The ``GET_CLIPBOARD`` selector."""

    NONE = 0
    COPY = 1
    CUT = 2


def _write_position(x: int, y: int, width: int, height: int) -> bytes:
    """Position is ``i32 x, i32 y, u16 width, u16 height`` (12 bytes)."""
    return pack_i32(x) + pack_i32(y) + pack_u16(width) + pack_u16(height)


def inject_keycode(
    action: int,
    keycode: int,
    repeat: int = 0,
    metastate: int = 0,
) -> bytes:
    """Send a key press/release."""
    return (
        pack_u8(ControlMessageType.INJECT_KEYCODE)
        + pack_u8(action)
        + pack_u32(keycode)
        + pack_u32(repeat)
        + pack_u32(metastate)
    )


def inject_text(text: str) -> bytes:
    """Inject literal text (used for characters with no Android keycode)."""
    payload = text.encode("utf-8")[:INJECT_TEXT_MAX_LENGTH]
    return pack_u8(ControlMessageType.INJECT_TEXT) + pack_u32(len(payload)) + payload


def inject_touch(
    action: int,
    pointer_id: int,
    x: int,
    y: int,
    screen_width: int,
    screen_height: int,
    pressure: float = 1.0,
    action_button: int = 0,
    buttons: int = 0,
) -> bytes:
    """Inject a touch/mouse event.

    ``x``/``y`` are device video coordinates; ``screen_width``/``screen_height``
    are the current video frame dimensions, which the server uses to scale the
    event onto the real display.
    """
    return (
        pack_u8(ControlMessageType.INJECT_TOUCH_EVENT)
        + pack_u8(action)
        + pack_u64(pointer_id)
        + _write_position(x, y, screen_width, screen_height)
        + pack_u16(float_to_u16fp(pressure))
        + pack_u32(action_button)
        + pack_u32(buttons)
    )


def inject_scroll(
    x: int,
    y: int,
    screen_width: int,
    screen_height: int,
    hscroll: float = 0.0,
    vscroll: float = 0.0,
    buttons: int = 0,
) -> bytes:
    """Inject a scroll event.

    The server normalizes the range ``[-16, 16]`` to ``[-1, 1]`` before
    converting to fixed point, so values are expected on that scale.
    """
    hscaled = _scroll_normalized(hscroll)
    vscaled = _scroll_normalized(vscroll)
    return (
        pack_u8(ControlMessageType.INJECT_SCROLL_EVENT)
        + _write_position(x, y, screen_width, screen_height)
        + pack_u16(float_to_i16fp(hscaled) & 0xFFFF)
        + pack_u16(float_to_i16fp(vscaled) & 0xFFFF)
        + pack_u32(buttons)
    )


def _scroll_normalized(value: float) -> float:
    normalized = value / 16.0
    return max(-1.0, min(1.0, normalized))


def back_or_screen_on(action: int = ACTION_DOWN) -> bytes:
    """Back button, or screen-on when the display is off."""
    return pack_u8(ControlMessageType.BACK_OR_SCREEN_ON) + pack_u8(action)


def expand_notification_panel() -> bytes:
    return pack_u8(ControlMessageType.EXPAND_NOTIFICATION_PANEL)


def expand_settings_panel() -> bytes:
    return pack_u8(ControlMessageType.EXPAND_SETTINGS_PANEL)


def collapse_panels() -> bytes:
    return pack_u8(ControlMessageType.COLLAPSE_PANELS)


def get_clipboard(copy_key: int = CopyKey.NONE) -> bytes:
    return pack_u8(ControlMessageType.GET_CLIPBOARD) + pack_u8(copy_key)


def set_clipboard(sequence: int, text: str, paste: bool = False) -> bytes:
    payload = text.encode("utf-8")[:CLIPBOARD_TEXT_MAX_LENGTH]
    return (
        pack_u8(ControlMessageType.SET_CLIPBOARD)
        + pack_u64(sequence)
        + pack_u8(1 if paste else 0)
        + pack_u32(len(payload))
        + payload
    )


def set_display_power(on: bool) -> bytes:
    return pack_u8(ControlMessageType.SET_DISPLAY_POWER) + pack_u8(1 if on else 0)


def rotate_device() -> bytes:
    return pack_u8(ControlMessageType.ROTATE_DEVICE)


def open_hard_keyboard_settings() -> bytes:
    return pack_u8(ControlMessageType.OPEN_HARD_KEYBOARD_SETTINGS)


def reset_video() -> bytes:
    return pack_u8(ControlMessageType.RESET_VIDEO)


def start_app(name: str) -> bytes:
    """Start an app by package name (or ``+``-prefixed activity)."""
    payload = name.encode("utf-8")[:255]
    return pack_u8(ControlMessageType.START_APP) + pack_u8(len(payload)) + payload


def resize_display(width: int, height: int) -> bytes:
    """Ask the device for a new virtual display size (0x0 restores it)."""
    return pack_u8(ControlMessageType.RESIZE_DISPLAY) + pack_u16(width) + pack_u16(height)


def uhid_create(
    device_id: int,
    vendor_id: int,
    product_id: int,
    name: str,
    report_desc: bytes,
) -> bytes:
    name_bytes = name.encode("utf-8")[:127]
    return (
        pack_u8(ControlMessageType.UHID_CREATE)
        + pack_u16(device_id)
        + pack_u16(vendor_id)
        + pack_u16(product_id)
        + pack_u8(len(name_bytes))
        + name_bytes
        + pack_u16(len(report_desc))
        + report_desc
    )


def uhid_input(device_id: int, data: bytes) -> bytes:
    return (
        pack_u8(ControlMessageType.UHID_INPUT)
        + pack_u16(device_id)
        + pack_u16(len(data))
        + data
    )


def uhid_destroy(device_id: int) -> bytes:
    return pack_u8(ControlMessageType.UHID_DESTROY) + pack_u16(device_id)


def tap(
    x: int,
    y: int,
    screen_width: int,
    screen_height: int,
    pointer_id: int = 0xFFFFFFFFFFFFFFFF,
) -> list[bytes]:
    """Convenience helper: a down/up pair at a device coordinate."""
    return [
        inject_touch(
            ACTION_DOWN,
            pointer_id,
            x,
            y,
            screen_width,
            screen_height,
            pressure=1.0,
            action_button=BUTTON_PRIMARY,
            buttons=BUTTON_PRIMARY,
        ),
        inject_touch(
            ACTION_UP,
            pointer_id,
            x,
            y,
            screen_width,
            screen_height,
            pressure=0.0,
            action_button=0,
            buttons=0,
        ),
    ]
