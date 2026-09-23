"""Start the on-device scrcpy server and connect its sockets.

Connection model (scrcpy 4.x, forward tunnel):

1. Push ``scrcpy-server.jar`` to ``/data/local/tmp``.
2. ``adb forward tcp:<port> localabstract:scrcpy_<scid>``.
3. Run ``app_process`` on the device; the server binds the abstract socket and
   *listens* because ``tunnel_forward=true``.
4. Connect the video socket, then audio (if enabled), then control — strictly
   in that order, because the server accepts them in that order.
5. On forward connections the server writes one dummy byte after each accept,
   which doubles as a readiness check while connecting.
6. With ``send_device_meta=true`` the first socket then carries a 64-byte
   device name field. After that the video socket carries the packet stream.
"""

from __future__ import annotations

import contextlib
import logging
import random
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..adb.device import Adb, Device
from ..config import SessionConfig
from ..errors import AdbError, ServerError
from ..protocol.framing import read_device_name
from ..protocol.io import SocketByteSource
from .asset import DEVICE_JAR_PATH, SCRCPY_VERSION, SERVER_MAIN_CLASS

log = logging.getLogger(__name__)

#: How long to wait for the server to start listening.
HANDSHAKE_TIMEOUT = 20.0

#: scid must fit in 31 bits (it is rendered as 8 hex digits in the socket name).
MAX_SCID = (1 << 31) - 1

_LOG_TAIL_LINES = 200


def pick_scid() -> int:
    """Return a random 31-bit session id."""
    return random.randrange(1, MAX_SCID)


def socket_name_for(scid: int) -> str:
    """Return the abstract socket name the server binds."""
    if scid == -1:
        return "scrcpy"
    return f"scrcpy_{scid:08x}"


@dataclass(slots=True)
class ServerSession:
    """A live server process plus its connected sockets."""

    device_name: str
    serial: str
    local_port: int
    scid: int
    video: socket.socket | None
    audio: socket.socket | None
    control: socket.socket | None

    @property
    def socket_name(self) -> str:
        return socket_name_for(self.scid)


@dataclass(slots=True)
class _LogTail:
    """Keep the most recent server log lines for error reporting."""

    lines: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_TAIL_LINES))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, line: str) -> None:
        with self.lock:
            self.lines.append(line.rstrip())

    def text(self, limit: int = 40) -> str:
        with self.lock:
            selected = list(self.lines)[-limit:]
        return "\n".join(selected)


class ScrcpyServer:
    """Owns the device-side server lifecycle for one session."""

    def __init__(
        self,
        adb: Adb,
        jar: Path,
        config: SessionConfig,
        *,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
    ) -> None:
        self.adb = adb
        self.jar = jar
        self.config = config
        self.handshake_timeout = handshake_timeout

        self._process: subprocess.Popen[str] | None = None
        self._log = _LogTail()
        self._log_thread: threading.Thread | None = None
        self._session: ServerSession | None = None
        self._local_port: int | None = None
        self._scid = pick_scid()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> ServerSession:
        """Push the server, run it, connect the sockets, return the session."""
        config = self.config
        config.validate()

        device: Device = self.adb.require_device()
        self.adb.serial = device.serial

        log.info("device: %s", device)
        self._push_server()

        self._local_port = _free_local_port()
        self.adb.forward(self._local_port, f"localabstract:{socket_name_for(self._scid)}")
        log.debug("forwarded tcp:%d -> localabstract:%s", self._local_port, socket_name_for(self._scid))

        self._start_process()

        deadline = time.monotonic() + self.handshake_timeout
        video = self._connect(deadline)
        audio = self._connect(deadline) if config.audio else None
        control = self._connect(deadline) if config.control else None

        device_name = read_device_name(SocketByteSource(video)) if video else ""

        self._session = ServerSession(
            device_name=device_name or device.display_name,
            serial=device.serial,
            local_port=self._local_port,
            scid=self._scid,
            video=video,
            audio=audio,
            control=control,
        )
        log.info(
            "server ready (device=%r, video=%s, audio=%s, control=%s)",
            self._session.device_name,
            bool(video),
            bool(audio),
            bool(control),
        )
        return self._session

    def stop(self) -> None:
        """Tear everything down: sockets, forward rule, server process."""
        session, self._session = self._session, None
        if session is not None:
            for sock in (session.control, session.audio, session.video):
                _close_quietly(sock)

        if self._local_port is not None:
            try:
                self.adb.forward_remove(self._local_port)
            except AdbError as exc:  # pragma: no cover - best effort
                log.debug("could not remove forward: %s", exc)
            self._local_port = None

        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - best effort
                process.kill()
                process.wait(timeout=5)

        if self._log_thread is not None:
            self._log_thread.join(timeout=2)
            self._log_thread = None

    def __enter__(self) -> ServerSession:
        return self.start()

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def log_tail(self) -> str:
        """Recent server output, for error messages."""
        return self._log.text()

    # -- internals ----------------------------------------------------------
    def _push_server(self) -> None:
        log.debug("pushing %s to %s", self.jar, DEVICE_JAR_PATH)
        self.adb.push(self.jar, DEVICE_JAR_PATH)

    def _server_options(self) -> list[str]:
        config = self.config

        options: list[tuple[str, str]] = [
            # scid is parsed as hexadecimal by the server.
            ("scid", f"{self._scid:x}"),
            ("log_level", config.log_level),
            ("video", "true"),
            ("audio", _bool(config.audio)),
            ("control", _bool(config.control)),
            ("video_codec", config.video_codec),
            ("video_bit_rate", str(config.video_bit_rate)),
            ("max_size", str(config.max_size)),
            ("max_fps", _float(config.max_fps)),
            # We are the listener: the device binds the socket and we connect.
            ("tunnel_forward", "true"),
            # Framing we implement on this side.
            ("send_device_meta", "true"),
            ("send_frame_meta", "true"),
            ("send_dummy_byte", "true"),
            ("send_stream_meta", "true"),
            ("power_on", _bool(config.power_on)),
            ("stay_awake", _bool(config.stay_awake)),
            ("keep_active", _bool(config.keep_active)),
            ("show_touches", _bool(config.show_touches)),
            ("clipboard_autosync", _bool(config.clipboard_autosync)),
            # Let the server clean up the pushed jar when it exits.
            ("cleanup", "true"),
        ]

        if config.video_codec_options:
            options.append(("video_codec_options", config.codec_options_string))

        return [f"{key}={value}" for key, value in options]

    def _start_process(self) -> None:
        device_command = Adb.build_shell_command(
            "CLASSPATH=" + DEVICE_JAR_PATH,
            "app_process",
            "/",
            SERVER_MAIN_CLASS,
            SCRCPY_VERSION,
            *self._server_options(),
        )
        log.debug("starting server: %s", device_command)

        try:
            process = self.adb.popen("shell", device_command)
        except AdbError as exc:
            raise ServerError(str(exc)) from exc

        if process.stdout is None:  # pragma: no cover - defensive
            raise ServerError("could not capture the server output")

        self._process = process

        def _pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                self._log.append(line)
                if self.config.log_level in {"verbose", "debug"}:
                    print(f"[server] {line.rstrip()}")

        self._log_thread = threading.Thread(
            target=_pump, name="scrcpy-server-log", daemon=True
        )
        self._log_thread.start()

    def _connect(self, deadline: float) -> socket.socket:
        """Connect one socket, tolerating the server's startup race."""
        assert self._local_port is not None
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            process = self._process
            if process is not None and process.poll() is not None:
                raise ServerError(
                    "the scrcpy server exited during startup "
                    f"(status {process.returncode}).\nServer output:\n"
                    f"{self.log_tail()}"
                )

            try:
                sock = socket.create_connection(("127.0.0.1", self._local_port), timeout=2.0)
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
                continue

            try:
                _tune_socket(sock)
                # The server writes a dummy byte once it has accepted us, which
                # also tells us the device-side socket is really connected.
                sock.settimeout(2.0)
                dummy = sock.recv(1)
            except OSError as exc:
                last_error = exc
                _close_quietly(sock)
                time.sleep(0.05)
                continue

            if not dummy:
                # adb accepted but the device side is not listening yet.
                _close_quietly(sock)
                last_error = ConnectionResetError("device socket not ready")
                time.sleep(0.05)
                continue

            sock.settimeout(None)
            return sock

        raise ServerError(
            "timed out waiting for the scrcpy server to accept connections.\n"
            f"Last error: {last_error}\nServer output:\n{self.log_tail()}"
        )


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _float(value: float) -> str:
    """Format a float without a trailing ``.0`` (the server parses either)."""
    if value == int(value):
        return str(int(value))
    return f"{value:.6g}"


def _free_local_port() -> int:
    """Ask the OS for a free localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _tune_socket(sock: socket.socket) -> None:
    """Reduce latency on the loopback link."""
    with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
        # A generous receive buffer keeps the pipe full without adding
        # queueing delay in our own code.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)


def _close_quietly(sock: socket.socket | None) -> None:
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):  # pragma: no cover - defensive
        sock.close()


__all__ = ["ScrcpyServer", "ServerSession", "pick_scid", "socket_name_for"]
