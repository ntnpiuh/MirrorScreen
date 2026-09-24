"""Wire the device, the decoder and the UI together and run a session."""

from __future__ import annotations

import logging
import signal
import sys
import threading
from collections.abc import Iterator
from itertools import chain, repeat
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

from .adb import Adb, cache_root, ensure_adb
from .audio import AudioStats, AudioWorker
from .audio_sink import QtAudioSink
from .config import SessionConfig
from .control_channel import ControlChannel
from .errors import MirrorScreenError
from .scrcpy import ScrcpyServer, ServerSession, ensure_server_jar
from .ui.widget import VideoWidget
from .ui.window import MirrorWindow
from .ui.settings import PreMirrorSettingsDialog
from .video.pipeline import FrameMailbox, PipelineCallbacks, PipelineStats, VideoPipeline

log = logging.getLogger(__name__)

_DEFAULT_WINDOW = (1280, 720)

#: Delay before each reconnection attempt, in seconds. The last value repeats
#: for as long as the window stays open.
_RECONNECT_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)

#: Statistics are sampled every 500 ms; log every fourth sample when tracing.
_STATUS_INTERVAL_MS = 500
_TRACE_EVERY = 4


def reconnect_delays() -> Iterator[float]:
    """Delays between reconnection attempts, then the longest one forever."""
    return chain(_RECONNECT_DELAYS, repeat(_RECONNECT_DELAYS[-1]))


def configure_surface_format(*, vsync: bool = True) -> None:
    """Request a modern core profile, before any window is created."""
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    # Vsync keeps tearing away but can add up to one refresh interval of
    # latency; it is worth turning off when chasing the lowest latency.
    fmt.setSwapInterval(1 if vsync else 0)
    QSurfaceFormat.setDefaultFormat(fmt)


class SessionRunner(QObject):
    """Owns the device session and keeps it alive across disconnects.

    Starting a session means adb work (push the server, set up the tunnel,
    launch the process), after which the pipeline owns the video socket. When
    the stream ends — the phone slept, the cable moved, the server exited — the
    runner tears the session down and rebuilds it, so the window recovers by
    itself instead of freezing on the last frame with no explanation.

    Signals are emitted from the supervisor thread; Qt queues them onto the GUI
    thread for us.
    """

    stream_state = Signal(str)
    """Text for the overlay over the video; an empty string hides it."""

    stream_message = Signal(str)
    """One-off message for the status bar."""

    startup_failed = Signal(str)
    """Emitted when the first session cannot be started."""

    device_ready = Signal(str)
    """Emitted with the device name once a session is up."""

    frame_ready = Signal()
    session_changed = Signal(int, int)
    clipboard_received = Signal(str)

    def __init__(
        self,
        config: SessionConfig,
        mailbox: FrameMailbox,
        *,
        adb_path: Path,
        jar: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._mailbox = mailbox
        self._adb_path = adb_path
        self._jar = jar

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: ScrcpyServer | None = None
        self._pipeline: VideoPipeline | None = None
        self._audio: AudioWorker | None = None
        self._channel: ControlChannel | None = None
        self._device_name = ""

    # -- public API, called from the GUI thread ----------------------------
    def start(self) -> None:
        """Connect once, raising if that fails, then keep the session alive."""
        self._open_session()  # a first failure reaches the command line
        self._thread = threading.Thread(
            target=self._supervise, name="session-supervisor", daemon=True
        )
        self._thread.start()

    def start_async(self) -> None:
        """Start without blocking the Qt GUI while adb performs handshakes."""
        if self._thread is not None:
            raise RuntimeError("session already started")
        self._thread = threading.Thread(
            target=self._start_and_supervise, name="session-supervisor", daemon=True
        )
        self._thread.start()

    def _start_and_supervise(self) -> None:
        self.stream_state.emit("connecting")
        try:
            self._open_session()
        except Exception as exc:
            log.error("session startup failed: %s", exc)
            self.stream_state.emit("startup failed")
            self.stream_message.emit(str(exc))
            self._teardown()
            self.startup_failed.emit(str(exc))
            return
        self._supervise()

    def stop(self) -> None:
        """Shut the session down and stop supervising."""
        self._stop.set()
        self._teardown()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    @property
    def device_name(self) -> str:
        return self._device_name

    def stats(self) -> PipelineStats | None:
        """Live counters, or ``None`` when no stream is running."""
        with self._lock:
            pipeline = self._pipeline
        return pipeline.stats if pipeline is not None else None

    def audio_stats(self) -> AudioStats | None:
        """Return audio counters for the active session, if audio is running."""
        with self._lock:
            audio = self._audio
        return audio.stats() if audio is not None else None

    def seconds_since_last_frame(self) -> float:
        with self._lock:
            pipeline = self._pipeline
        return pipeline.seconds_since_last_frame() if pipeline is not None else 0.0

    # -- device commands (the MirrorWindow's DeviceCommands interface) ------
    def send_control(self, payload: bytes) -> None:
        self._with_channel(lambda channel: channel.send(payload))

    def rotate_device(self) -> None:
        self._with_channel(lambda channel: channel.rotate_device())

    def set_display_power(self, on: bool) -> None:
        self._with_channel(lambda channel: channel.set_display_power(on))

    def set_clipboard(self, text: str, *, paste: bool = False) -> None:
        self._with_channel(lambda channel: channel.set_clipboard(text, paste=paste))

    def _with_channel(self, action) -> None:
        with self._lock:
            channel = self._channel
        if channel is not None:
            action(channel)

    # -- session lifecycle -------------------------------------------------
    def _open_session(self) -> None:
        """Start the server and the pipeline. Raises on failure."""
        config = self._config
        adb = Adb(self._adb_path, config.serial)
        server = ScrcpyServer(adb, self._jar, config)
        channel: ControlChannel | None = None
        pipeline: VideoPipeline | None = None
        audio: AudioWorker | None = None
        try:
            session = server.start()
            self._server = server
            self._device_name = session.device_name

            control_socket = session.control
            if control_socket is not None:
                channel = ControlChannel(
                    control_socket,
                    on_clipboard=self.clipboard_received.emit,
                )
                channel.start()

            video_socket = session.video
            if video_socket is None:
                raise MirrorScreenError("server did not provide a video socket")
            pipeline = VideoPipeline(
                video_socket,
                config,
                self._mailbox,
                PipelineCallbacks(
                    on_frame=lambda _frame: self.frame_ready.emit(),
                    on_session=lambda packet: self.session_changed.emit(
                        packet.width, packet.height
                    ),
                    on_end=lambda reason: self.stream_message.emit(reason),
                    on_error=lambda exc: self.stream_message.emit(str(exc)),
                ),
            )

            if config.audio and session.audio is not None:
                try:
                    sink = QtAudioSink(config.audio_output)
                    audio = AudioWorker(
                        session.audio,
                        sink,
                        on_error=lambda exc: self.stream_message.emit(
                            f"audio stopped: {exc}"
                        ),
                    )
                    audio.start()
                except Exception as exc:
                    # Audio is an optional stream; keep video usable when the
                    # platform backend is unavailable or cannot start.
                    log.warning("audio unavailable: %s", exc)
                    self.stream_message.emit(f"audio unavailable: {exc}")

            with self._lock:
                self._channel = channel
                self._pipeline = pipeline
                self._audio = audio
            pipeline.start()
        except Exception:
            if audio is not None:
                audio.stop()
            if pipeline is not None:
                pipeline.stop()
            if channel is not None:
                channel.stop()
            server.stop()
            with self._lock:
                self._server = None
                self._channel = None
                self._pipeline = None
                self._audio = None
            self._device_name = ""
            raise

        log.info("session up: %s", session.device_name)
        self.device_ready.emit(session.device_name)
        self.stream_state.emit("")

    def _teardown(self) -> None:
        with self._lock:
            pipeline, audio, channel, server = (
                self._pipeline,
                self._audio,
                self._channel,
                self._server,
            )
            self._pipeline = None
            self._audio = None
            self._channel = None
            self._server = None

        if pipeline is not None:
            pipeline.stop()  # also unblocks the supervisor's join()
        if audio is not None:
            audio.stop()
        if channel is not None:
            channel.stop()
        if server is not None:
            server.stop()

    def _supervise(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                pipeline = self._pipeline
            if pipeline is None:
                return
            pipeline.join()  # returns when the stream ends or we are stopping
            if self._stop.is_set():
                return

            self._teardown()
            if not self._config.auto_reconnect:
                self.stream_state.emit("stream ended")
                return

            if self._reconnect():
                continue
            return

    def _reconnect(self) -> bool:
        """Keep trying to rebuild the session. False means we are stopping."""
        for delay in reconnect_delays():
            self.stream_state.emit("stream stopped - reconnecting")
            if self._stop.wait(delay):
                return False
            try:
                self._open_session()
            except MirrorScreenError as exc:
                log.info("reconnect failed: %s", exc)
                self.stream_state.emit(
                    "disconnected - waiting for the device\n"
                    "check the cable, and that USB debugging is still enabled"
                )
                continue
            except Exception:  # pragma: no cover - defensive
                log.exception("unexpected failure while reconnecting")
                continue

            self.stream_message.emit(f"reconnected to {self._device_name}")
            return True

        return False  # pragma: no cover - reconnect_delays() never ends


class _ResourceResolver(QObject):
    """Resolve bundled resources away from the Qt GUI thread."""

    ready = Signal(object, object)
    failed = Signal(str)
    resolved = Signal(object, object)
    failed_on_gui = Signal(str)

    def __init__(self, config: SessionConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._stop = threading.Event()
        self.ready.connect(self._forward_ready)
        self.failed.connect(self._forward_failure)

    @Slot(object, object)
    def _forward_ready(self, adb_path: Path, jar: Path) -> None:
        self.resolved.emit(adb_path, jar)

    @Slot(str)
    def _forward_failure(self, message: str) -> None:
        self.failed_on_gui.emit(message)

    def stop(self) -> None:
        self._stop.set()

    def resolve(self) -> None:
        if self._stop.is_set():
            return
        try:
            progress = lambda message: log.info("startup: %s", message)
            adb_path = ensure_adb(self._config.adb_path, progress=progress)
            cache = (
                Path(self._config.cache_dir)
                if self._config.cache_dir
                else cache_root()
            )
            jar = ensure_server_jar(cache, progress=progress)
        except Exception as exc:
            if self._stop.is_set():
                return
            self.failed.emit(str(exc))
            return
        if self._stop.is_set():
            return
        self.ready.emit(adb_path, jar)


class _GuiCallback(QObject):
    """Marshal a callback emitted by a worker onto the Qt GUI thread."""

    def __init__(self, callback, parent: QObject) -> None:
        super().__init__(parent)
        self._callback = callback

    @Slot(str)
    def call(self, message: str) -> None:
        self._callback(message)


def run(config: SessionConfig, *, progress=None) -> int:
    """Run a full mirroring session; returns a process exit code."""
    config.validate()
    progress = progress or (lambda message: print(message, file=sys.stderr))

    return _run_gui(config, progress=progress)


def start_device_session(
    config: SessionConfig, *, progress=None
) -> tuple[ScrcpyServer, ServerSession]:
    """Connect once and hand back the server and its session.

    For one-shot, non-GUI use (the probe and the self-check). The window uses
    :class:`SessionRunner` instead, which needs the resolved adb path and jar so
    it can rebuild the session without resolving them again.
    """
    config.validate()
    progress = progress or (lambda message: print(message, file=sys.stderr))

    adb_path = ensure_adb(config.adb_path, progress=progress)
    cache = Path(config.cache_dir) if config.cache_dir else cache_root()
    jar = ensure_server_jar(cache, progress=progress)

    adb = Adb(adb_path, config.serial)
    server = ScrcpyServer(adb, jar, config)
    return server, server.start()


def _run_gui(config: SessionConfig, *, progress=None) -> int:
    configure_surface_format(vsync=config.vsync)
    existing_app = QApplication.instance()
    if existing_app is None:
        app = QApplication(sys.argv[:1])
    elif isinstance(existing_app, QApplication):
        app = existing_app
    else:
        raise RuntimeError("a non-GUI Qt application is already running")
    app.setApplicationName("Mirror Screen")
    app.setApplicationDisplayName("Mirror Screen")
    # The settings dialog is accepted (and hidden) before adb/server resource
    # resolution finishes and the stream window is shown. Do not let Qt quit
    # the event loop during that gap; Cancel and the explicit shutdown path
    # below own application exit.
    app.setQuitOnLastWindowClosed(False)

    control = PreMirrorSettingsDialog(config)
    mailbox: FrameMailbox | None = None
    runner: SessionRunner | None = None
    window: MirrorWindow | None = None
    resolver: _ResourceResolver | None = None
    resolver_thread: threading.Thread | None = None
    widget: VideoWidget | None = None
    failure_bridge: _GuiCallback | None = None
    tracer: QTimer | None = None

    def start_stream() -> None:
        nonlocal mailbox, runner, window, resolver, resolver_thread
        selected = control.build_config()
        control.setEnabled(False)
        control.setWindowTitle("Mirror Screen - connecting")
        resolver = _ResourceResolver(selected)

        def resolve() -> None:
            assert resolver is not None
            resolver.resolve()

        resolver_thread = threading.Thread(target=resolve, name="resource-resolver", daemon=True)
        resolver.resolved.connect(open_stream)
        resolver.failed_on_gui.connect(resource_failed)
        resolver_thread.start()

    def resource_failed(message: str) -> None:
        control.setEnabled(True)
        control.setWindowTitle(f"Mirror Screen - startup failed: {message}")
        control.show()
        log.error("startup failed: %s", message)

    def open_stream(adb_path: Path, jar: Path) -> None:
        nonlocal mailbox, runner, window, widget, failure_bridge, tracer
        selected = control.build_config()
        active_mailbox = FrameMailbox()
        active_runner = SessionRunner(
            selected, active_mailbox, adb_path=adb_path, jar=jar
        )
        active_widget = VideoWidget(selected, active_mailbox, active_runner.send_control)
        active_window = MirrorWindow(
            active_widget,
            selected,
            device_name="connecting",
            commands=active_runner,
            stats_provider=lambda: _status_text(
                active_runner, active_mailbox, selected, active_widget
            ),
            on_close=stream_closed,
        )
        mailbox = active_mailbox
        runner = active_runner
        widget = active_widget
        window = active_window
        active_runner.frame_ready.connect(active_widget.refresh)
        active_runner.device_ready.connect(
            lambda name: active_window.setWindowTitle(f"Mirror Screen - {name}")
        )
        active_runner.session_changed.connect(active_widget.set_video_size)
        active_runner.session_changed.connect(active_window.on_video_session)
        active_runner.stream_state.connect(active_window.show_overlay)
        active_runner.stream_message.connect(active_window.statusBar().showMessage)
        active_runner.clipboard_received.connect(
            active_window.set_clipboard_from_device
        )
        if failure_bridge is None:
            failure_bridge = _GuiCallback(startup_failed, app)
        active_runner.startup_failed.connect(failure_bridge.call)
        active_window.resize(*_DEFAULT_WINDOW)
        control.hide()
        active_window.show()
        active_runner.start_async()
        if tracer is not None:
            tracer.stop()
        tracer = _install_tracer(
            selected, app, active_runner, active_mailbox, active_widget
        )

    def _stop_tracer() -> None:
        nonlocal tracer
        if tracer is not None:
            tracer.stop()
            tracer = None

    def startup_failed(message: str) -> None:
        """Return to the control panel when async session startup fails."""
        nonlocal window, runner
        _stop_tracer()
        if runner is not None:
            runner.stop()
        if window is not None:
            window.deleteLater()
            window = None
        runner = None
        control.setEnabled(True)
        control.setWindowTitle(f"Mirror Screen - startup failed: {message}")
        control.show()

    def stream_closed() -> None:
        nonlocal window, runner
        _stop_tracer()
        if runner is not None:
            runner.stop()
        if window is not None:
            window.deleteLater()
            window = None
        runner = None
        control.setEnabled(True)
        control.show()

    control.accepted.connect(start_stream)
    control.rejected.connect(app.quit)
    control.show()

    # Release GL resources while the context is still alive: Qt does not do it
    # for us when a window closes.
    def shutdown() -> None:
        _stop_tracer()
        if resolver is not None:
            resolver.stop()
        if resolver_thread is not None and resolver_thread.is_alive():
            resolver_thread.join(timeout=5)
        if window is not None:
            window._widget.release_gpu_resources()
        if runner is not None:
            runner.stop()

    app.aboutToQuit.connect(shutdown)

    _install_sigint_handler(app)

    try:
        return app.exec()
    finally:
        if tracer is not None:
            tracer.stop()
        if runner is not None:
            runner.stop()
        if widget is not None:
            widget.release_gpu_resources()  # no-op if aboutToQuit already ran


def _status_text(
    runner: SessionRunner,
    mailbox: FrameMailbox,
    config: SessionConfig,
    widget: VideoWidget | None = None,
) -> str:
    """The line shown in the status bar, and logged when tracing."""
    stats = runner.stats()
    audio_stats = runner.audio_stats()
    parts: list[str] = []

    if stats is None or stats.width == 0:
        parts.append("waiting for video")
    else:
        parts.append(f"{stats.width}x{stats.height}")
        parts.append(f"{stats.fps:.1f} fps")
        parts.append(f"decode {stats.average_decode_ms:.1f} ms")
        # Waiting vs working is measured entirely on our own clock, so unlike a
        # host/device timestamp comparison it cannot drift: it tells you whether
        # the software is keeping up with the device.
        parts.append(f"{stats.idle_fraction * 100:.0f}% idle")
        parts.append(config.video_codec)

        quiet_for = runner.seconds_since_last_frame()
        if quiet_for > 1.5:
            # Frames only arrive when something changes on screen, so this is
            # normal while nothing moves - and it is also what a stalled encoder
            # looks like, which is exactly why it is worth showing.
            parts.append(f"no frames for {quiet_for:.1f}s")
        if stats.long_frame_gaps:
            parts.append(f"gaps>250ms: {stats.long_frame_gaps}")

    if widget is not None:
        paints = widget.paint_stats()
        if paints["paints"]:
            # Paints per second versus frames per second is what shows whether
            # the UI thread, rather than the device, is the limit; the request
            # rate shows how many repaints the stream asked for.
            parts.append(
                f"ui {paints['paints_per_second']:.0f}/s "
                f"(req {paints['requests_per_second']:.0f}/s)"
            )
            parts.append(f"paint {paints['average_paint_ms']:.1f} ms")

    if audio_stats is not None:
        parts.append(f"audio start {audio_stats.startup_ms:.0f} ms")
        if audio_stats.dropped:
            parts.append(f"audio dropped {audio_stats.dropped}")
        if audio_stats.underruns:
            parts.append(f"audio underruns {audio_stats.underruns}")

    if mailbox.overwritten:
        parts.append(f"dropped {mailbox.overwritten}")
    return "  ·  ".join(parts)


def _install_tracer(
    config: SessionConfig,
    app: QApplication,
    runner: SessionRunner,
    mailbox: FrameMailbox,
    widget: VideoWidget,
) -> QTimer | None:
    """Log the status line periodically so stutter can be diagnosed later."""
    if not config.trace:
        return None

    ticks = {"count": 0}

    def _log() -> None:
        ticks["count"] += 1
        if ticks["count"] % _TRACE_EVERY:
            return
        stats = runner.stats()
        gaps = f"{stats.max_frame_gap_ms:.0f}ms" if stats is not None else "n/a"
        paints = widget.paint_stats()
        log.info(
            "stats: %s | worst gap %s | worst paint %.0f ms",
            _status_text(runner, mailbox, config, widget),
            gaps,
            paints["max_paint_ms"],
        )

    timer = QTimer(app)
    timer.setInterval(_STATUS_INTERVAL_MS)
    timer.timeout.connect(_log)
    timer.start()
    # Keep a reference alive on the application object.
    setattr(app, "_mirror_screen_tracer", timer)
    return timer


def _install_sigint_handler(app: QApplication) -> None:
    """Let Ctrl+C in the terminal quit the Qt event loop."""

    def _handler(_signum, _frame):
        app.quit()

    signal.signal(signal.SIGINT, _handler)
    # Python only runs signal handlers between bytecode instructions, so give
    # the interpreter a chance to notice by waking up regularly.
    timer = QTimer(app)
    timer.start(200)
    timer.timeout.connect(lambda: None)
    setattr(app, "_mirror_screen_signal_timer", timer)


__all__ = [
    "SessionRunner",
    "configure_surface_format",
    "reconnect_delays",
    "run",
    "start_device_session",
]
