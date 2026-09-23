"""Exception hierarchy for Mirror Screen."""

from __future__ import annotations


class MirrorScreenError(Exception):
    """Base class for all errors raised by Mirror Screen."""


class AdbError(MirrorScreenError):
    """An adb command failed or adb could not be located."""


class DeviceError(MirrorScreenError):
    """No usable device, or the device rejected an operation."""


class ServerError(MirrorScreenError):
    """The on-device scrcpy server could not be started or stopped cleanly."""


class ProtocolError(MirrorScreenError):
    """The device sent data that does not match the expected wire format."""


class ConnectionClosed(MirrorScreenError):
    """The peer closed the connection (or the socket was shut down)."""


class DecodeError(MirrorScreenError):
    """The video stream could not be decoded."""
