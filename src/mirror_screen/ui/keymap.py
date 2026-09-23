"""Translate Qt key events into Android key events."""

from __future__ import annotations

from PySide6.QtCore import Qt

from ..protocol.const import (
    META_ALT_ON,
    META_CTRL_ON,
    META_META_ON,
    META_SHIFT_ON,
)

# Android key codes (android.view.KeyEvent).
KEYCODE_HOME = 3
KEYCODE_BACK = 4
KEYCODE_DPAD_UP = 19
KEYCODE_DPAD_DOWN = 20
KEYCODE_DPAD_LEFT = 21
KEYCODE_DPAD_RIGHT = 22
KEYCODE_DPAD_CENTER = 23
KEYCODE_VOLUME_UP = 24
KEYCODE_VOLUME_DOWN = 25
KEYCODE_POWER = 26
KEYCODE_A = 29
KEYCODE_Z = 54
KEYCODE_COMMA = 55
KEYCODE_PERIOD = 56
KEYCODE_TAB = 61
KEYCODE_SPACE = 62
KEYCODE_ENTER = 66
KEYCODE_DEL = 67  # backspace
KEYCODE_GRAVE = 68
KEYCODE_MINUS = 69
KEYCODE_EQUALS = 70
KEYCODE_LEFT_BRACKET = 71
KEYCODE_RIGHT_BRACKET = 72
KEYCODE_BACKSLASH = 73
KEYCODE_SEMICOLON = 74
KEYCODE_APOSTROPHE = 75
KEYCODE_SLASH = 76
KEYCODE_AT = 77
KEYCODE_PLUS = 81
KEYCODE_MENU = 82
KEYCODE_PAGE_UP = 92
KEYCODE_PAGE_DOWN = 93
KEYCODE_ESCAPE = 111
KEYCODE_FORWARD_DEL = 112
KEYCODE_MOVE_HOME = 122
KEYCODE_MOVE_END = 123
KEYCODE_INSERT = 124
KEYCODE_F1 = 131
KEYCODE_NUM_LOCK = 143
KEYCODE_APP_SWITCH = 187

#: Mapping for a US layout; the Android side applies the device's own layout on
#: top of these key codes.
_MAP: dict[int, int] = {
    Qt.Key.Key_Space: KEYCODE_SPACE,
    Qt.Key.Key_Return: KEYCODE_ENTER,
    Qt.Key.Key_Enter: KEYCODE_ENTER,
    Qt.Key.Key_Tab: KEYCODE_TAB,
    Qt.Key.Key_Backspace: KEYCODE_DEL,
    Qt.Key.Key_Delete: KEYCODE_FORWARD_DEL,
    Qt.Key.Key_Escape: KEYCODE_ESCAPE,
    Qt.Key.Key_Insert: KEYCODE_INSERT,
    Qt.Key.Key_Home: KEYCODE_MOVE_HOME,
    Qt.Key.Key_End: KEYCODE_MOVE_END,
    Qt.Key.Key_PageUp: KEYCODE_PAGE_UP,
    Qt.Key.Key_PageDown: KEYCODE_PAGE_DOWN,
    Qt.Key.Key_Up: KEYCODE_DPAD_UP,
    Qt.Key.Key_Down: KEYCODE_DPAD_DOWN,
    Qt.Key.Key_Left: KEYCODE_DPAD_LEFT,
    Qt.Key.Key_Right: KEYCODE_DPAD_RIGHT,
    Qt.Key.Key_Comma: KEYCODE_COMMA,
    Qt.Key.Key_Period: KEYCODE_PERIOD,
    Qt.Key.Key_Minus: KEYCODE_MINUS,
    Qt.Key.Key_Equal: KEYCODE_EQUALS,
    Qt.Key.Key_BracketLeft: KEYCODE_LEFT_BRACKET,
    Qt.Key.Key_BracketRight: KEYCODE_RIGHT_BRACKET,
    Qt.Key.Key_Backslash: KEYCODE_BACKSLASH,
    Qt.Key.Key_Semicolon: KEYCODE_SEMICOLON,
    Qt.Key.Key_Apostrophe: KEYCODE_APOSTROPHE,
    Qt.Key.Key_Slash: KEYCODE_SLASH,
    Qt.Key.Key_QuoteLeft: KEYCODE_GRAVE,
    Qt.Key.Key_At: KEYCODE_AT,
    Qt.Key.Key_Plus: KEYCODE_PLUS,
}

# Letters and digits are contiguous in both key code spaces.
for _index in range(26):
    _MAP[Qt.Key.Key_A + _index] = KEYCODE_A + _index
for _index in range(10):
    _MAP[Qt.Key.Key_0 + _index] = 7 + _index
for _index in range(12):
    _MAP[Qt.Key.Key_F1 + _index] = KEYCODE_F1 + _index


def keycode_for_qt_key(key: int) -> int | None:
    """Return the Android key code for a Qt key, or ``None`` if unmapped."""
    return _MAP.get(key)


def metastate_for_modifiers(modifiers: Qt.KeyboardModifier) -> int:
    """Translate Qt modifier flags into an Android meta state."""
    state = 0
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        state |= META_SHIFT_ON
    if modifiers & Qt.KeyboardModifier.AltModifier:
        state |= META_ALT_ON
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        state |= META_CTRL_ON
    if modifiers & Qt.KeyboardModifier.MetaModifier:
        state |= META_META_ON
    return state


def is_printable(text: str) -> bool:
    """True if the text can be injected as literal text."""
    return bool(text) and all(character.isprintable() for character in text)
