"""The control socket: send input events, receive device messages."""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
from collections.abc import Callable, Iterable

from .protocol import control
from .protocol.device_msg import (
    AckClipboardMessage,
    ClipboardMessage,
    DeviceMessageParser,
    UhidOutputMessage,
)

log = logging.getLogger(__name__)

_RECV_CHUNK = 1 << 16


class ControlChannel:
    """Owns the control socket.

    Sending is safe from any thread: input events come from the Qt main thread
    while the reader thread only ever receives.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        on_clipboard: Callable[[str], None] | None = None,
        on_ack: Callable[[int], None] | None = None,
        on_uhid_output: Callable[[int, bytes], None] | None = None,
    ) -> None:
        self._sock = sock
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._parser = DeviceMessageParser()
        self._sequence = 0

        self._on_clipboard = on_clipboard
        self._on_ack = on_ack
        self._on_uhid_output = on_uhid_output

        #: Sequence number of the last SET_CLIPBOARD we sent.
        self.last_sequence = 0
        #: Sequence number the device acknowledged.
        self.acked_sequence = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._read_loop, name="control-channel", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RD)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- sending ------------------------------------------------------------
    def send(self, payload: bytes) -> None:
        """Write one serialized control message."""
        with self._send_lock:
            try:
                self._sock.sendall(payload)
            except OSError as exc:
                log.warning("could not send a control message: %s", exc)

    def send_many(self, payloads: Iterable[bytes]) -> None:
        """Write several messages, coalescing them into one write."""
        data = b"".join(payloads)
        if data:
            self.send(data)

    def next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFFFFFFFFFFFFFFFF
        return self._sequence

    def set_clipboard(self, text: str, *, paste: bool = False) -> int:
        """Push the host clipboard to the device; return the sequence number."""
        sequence = self.next_sequence()
        self.last_sequence = sequence
        self.send(control.set_clipboard(sequence, text, paste=paste))
        return sequence

    def request_clipboard(self, copy_key: int = control.CopyKey.NONE) -> None:
        self.send(control.get_clipboard(copy_key))

    def set_display_power(self, on: bool) -> None:
        self.send(control.set_display_power(on))

    def rotate_device(self) -> None:
        self.send(control.rotate_device())

    def reset_video(self) -> None:
        self.send(control.reset_video())

    def start_app(self, name: str) -> None:
        self.send(control.start_app(name))

    # -- receiving ----------------------------------------------------------
    def _read_loop(self) -> None:
        sock = self._sock
        while not self._stop.is_set():
            try:
                data = sock.recv(_RECV_CHUNK)
            except OSError as exc:
                if not self._stop.is_set():
                    log.debug("control socket read failed: %s", exc)
                return
            if not data:
                log.debug("control socket closed by the device")
                return

            try:
                messages = self._parser.feed(data)
            except Exception as exc:  # pragma: no cover - protocol corruption
                log.warning("dropping control data: %s", exc)
                return

            for message in messages:
                self._dispatch(message)

    def _dispatch(self, message) -> None:
        if isinstance(message, ClipboardMessage):
            if self._on_clipboard is not None:
                self._on_clipboard(message.text)
        elif isinstance(message, AckClipboardMessage):
            self.acked_sequence = message.sequence
            if self._on_ack is not None:
                self._on_ack(message.sequence)
        elif isinstance(message, UhidOutputMessage) and self._on_uhid_output is not None:
            self._on_uhid_output(message.device_id, message.data)
