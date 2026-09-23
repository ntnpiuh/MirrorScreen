"""Start the on-device scrcpy server and connect its sockets.

Two connection models exist, and the difference matters:

**Reverse tunnel (default).** The client binds a local TCP listener, then asks
adb for ``reverse localabstract:scrcpy_<scid> tcp:<port>``. The device connects
*out* to us, in the order video, audio, control. Because we own the listener
before the server starts, there is no race: accept() simply waits.

**Forward tunnel (``force_adb_forward``).** The device binds the abstract socket
and we connect to it through ``adb forward``. In this mode the local TCP connect
succeeds even while the device side is not listening yet (adb accepts locally and
closes afterwards), so the client must connect/retry until the server writes its
readiness dummy byte. That retry is inherently racy: a connection the client
abandoned can still be accepted by the server and consume one of the strict
accept slots, desynchronising video/audio/control and making the server abort.
It is kept only for devices where ``adb reverse`` is unavailable.

Common to both: ``send_device_meta=true`` makes the first socket carry a 64-byte
device name field, after which the video socket carries the packet stream.
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

#: How long to wait for the server to start and connect.
HANDSHAKE_TIMEOUT = 20.0

#: scid must fit in 31 bits (it is rendered as 8 hex digits in the socket name).
MAX_SCID = (1 << 31) - 1

_LOG_TAIL_LINES = 200

#: How long accept() blocks before we re-check whether the server is alive.
_ACCEPT_POLL = 0.25

#: Backlog for the reverse listener: video + audio + control.
_LISTEN_BACKLOG = 8


def pick_scid() -> int:
    """Return a random 31-bit session id."""
    return random.randrange(1, MAX_SCID)


def socket_name_for(scid: int) -> str:
    """Return the abstract socket name the server binds or connects to."""
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
    tunnel_forward: bool = False

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
        self._listener: socket.socket | None = None
        self._local_port: int | None = None
        self._scid = pick_scid()

    @property
    def tunnel_forward(self) -> bool:
        return self.config.force_adb_forward

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> ServerSession:
        """Push the server, run it, connect the sockets, return the session."""
        config = self.config
        config.validate()

        device: Device = self.adb.require_device()
        self.adb.serial = device.serial

        log.info("device: %s", device)
        self._push_server()

        deadline = time.monotonic() + self.handshake_timeout
        if self.tunnel_forward:
            video, audio, control = self._start_forward_tunnel(deadline)
        else:
            video, audio, control = self._start_reverse_tunnel(deadline)

        device_name = read_device_name(SocketByteSource(video)) if video else ""

        self._session = ServerSession(
            device_name=device_name or device.display_name,
            serial=device.serial,
            local_port=self._local_port or 0,
            scid=self._scid,
            video=video,
            audio=audio,
            control=control,
            tunnel_forward=self.tunnel_forward,
        )
        log.info(
            "server ready (%s tunnel, device=%r, video=%s, audio=%s, control=%s)",
            "forward" if self.tunnel_forward else "reverse",
            self._session.device_name,
            bool(video),
            bool(audio),
            bool(control),
        )
        return self._session

    def stop(self) -> None:
        """Tear everything down: sockets, tunnel rule, server process."""
        session, self._session = self._session, None
        if session is not None:
            for sock in (session.control, session.audio, session.video):
                _close_quietly(sock)

        if self._listener is not None:
            _close_quietly(self._listener)
            self._listener = None

        if self._local_port is not None:
            try:
                if self.tunnel_forward:
                    self.adb.forward_remove(self._local_port)
                else:
                    self.adb.reverse_remove(
                        f"localabstract:{socket_name_for(self._scid)}"
                    )
            except AdbError as exc:  # pragma: no cover - best effort
                log.debug("could not remove the tunnel: %s", exc)
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

    # -- connection models --------------------------------------------------
    def _start_reverse_tunnel(
        self, deadline: float
    ) -> tuple[socket.socket, socket.socket | None, socket.socket | None]:
        """Listen first, then let the device connect out to us."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(_LISTEN_BACKLOG)
        listener.settimeout(_ACCEPT_POLL)
        self._listener = listener
        self._local_port = listener.getsockname()[1]

        remote = f"localabstract:{socket_name_for(self._scid)}"
        self.adb.reverse(remote, f"tcp:{self._local_port}")
        log.debug("reverse %s -> tcp:%d", remote, self._local_port)

        self._start_process()

        video = self._accept(deadline, "video")
        audio = self._accept(deadline, "audio") if self.config.audio else None
        control = self._accept(deadline, "control") if self.config.control else None
        return video, audio, control

    def _accept(self, deadline: float, what: str) -> socket.socket:
        """Accept one device connection on the reverse listener."""
        assert self._listener is not None
        while time.monotonic() < deadline:
            self._raise_if_server_died(what)
            try:
                sock, _address = self._listener.accept()
            except TimeoutError:
                continue
            _tune_socket(sock)
            sock.settimeout(None)
            log.debug("accepted the %s socket", what)
            return sock

        raise ServerError(
            f"timed out waiting for the device to connect the {what} socket "
            f"({self.handshake_timeout:.0f}s).\nServer output:\n{self.log_tail()}"
        )

    def _start_forward_tunnel(
        self, deadline: float
    ) -> tuple[socket.socket, socket.socket | None, socket.socket | None]:
        """Connect out to the device, which is listening (racy on some setups)."""
        self._local_port = _free_local_port()
        self.adb.forward(
            self._local_port, f"localabstract:{socket_name_for(self._scid)}"
        )
        log.debug(
            "forward tcp:%d -> localabstract:%s",
            self._local_port,
            socket_name_for(self._scid),
        )

        self._start_process()

        video = self._connect(deadline)
        audio = self._connect(deadline) if self.config.audio else None
        control = self._connect(deadline) if self.config.control else None
        return video, audio, control

    def _connect(self, deadline: float) -> socket.socket:
        """Connect one socket, tolerating the server's startup race."""
        assert self._local_port is not None
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            self._raise_if_server_died("next")

            try:
                sock = socket.create_connection(
                    ("127.0.0.1", self._local_port), timeout=2.0
                )
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
                # adb accepted locally but the device side is not listening.
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

    def _raise_if_server_died(self, what: str) -> None:
        process = self._process
        if process is not None and process.poll() is not None:
            raise ServerError(
                f"the scrcpy server exited while connecting the {what} socket "
                f"(status {process.returncode}).\nServer output:\n{self.log_tail()}"
            )

    # -- process ------------------------------------------------------------
    def _push_server(self) -> None:
        log.debug("pushing %s to %s", self.jar, DEVICE_JAR_PATH)
        self.adb.push(self.jar, DEVICE_JAR_PATH)

    def _server_options(self) -> list[str]:
        config = self.config
        forward = self.tunnel_forward

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
            # In forward mode the device listens and we connect; in reverse mode
            # (the default) it is the other way round.
            ("tunnel_forward", _bool(forward)),
            # Framing we implement on this side.
            ("send_device_meta", "true"),
            ("send_frame_meta", "true"),
            # The dummy byte only exists in forward mode, where it is our
            # readiness signal.
            ("send_dummy_byte", _bool(forward)),
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
