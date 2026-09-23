"""Wire the device, the decoder and the UI together and run a session."""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

from .adb import Adb, cache_root, ensure_adb
from .config import SessionConfig
from .control_channel import ControlChannel
from .scrcpy import ScrcpyServer, ServerSession, ensure_server_jar
from .ui.widget import VideoWidget
from .ui.window import MirrorWindow
from .video.pipeline import FrameMailbox, PipelineCallbacks, VideoPipeline

log = logging.getLogger(__name__)

_DEFAULT_WINDOW = (1280, 720)


class UiBridge(QObject):
    """Marshals worker-thread events onto the GUI thread."""

    frame_ready = Signal()
    session_changed = Signal(int, int)
    pipeline_ended = Signal(str)
    pipeline_failed = Signal(str)
    clipboard_received = Signal(str)
    ack_received = Signal(int)


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


def start_device_session(
    config: SessionConfig,
    *,
    progress=None,
) -> tuple[ScrcpyServer, ServerSession]:
    """Resolve adb, fetch the server jar and start the on-device server."""
    config.validate()
    progress = progress or (lambda message: print(message, file=sys.stderr))

    adb_path = ensure_adb(config.adb_path, progress=progress)
    log.info("using adb at %s", adb_path)

    cache = Path(config.cache_dir) if config.cache_dir else cache_root()
    jar = ensure_server_jar(cache, progress=progress)

    adb = Adb(adb_path, config.serial)
    server = ScrcpyServer(adb, jar, config)
    return server, server.start()


def run(config: SessionConfig, *, progress=None) -> int:
    """Run a full mirroring session; returns a process exit code."""
    server, session = start_device_session(config, progress=progress)
    try:
        return _run_gui(config, session)
    finally:
        server.stop()


def _run_gui(config: SessionConfig, session: ServerSession) -> int:
    configure_surface_format(vsync=config.vsync)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Mirror Screen")
    app.setApplicationDisplayName("Mirror Screen")

    mailbox = FrameMailbox()
    bridge = UiBridge()

    channel: ControlChannel | None = None
    if session.control is not None:
        channel = ControlChannel(
            session.control,
            on_clipboard=bridge.clipboard_received.emit,
            on_ack=bridge.ack_received.emit,
        )
        channel.start()

    send_control = channel.send if channel is not None else _discard
    widget = VideoWidget(config, mailbox, send_control)

    pipeline = VideoPipeline(
        session.video,
        config,
        mailbox,
        PipelineCallbacks(
            on_frame=lambda _frame: bridge.frame_ready.emit(),
            on_session=lambda packet: bridge.session_changed.emit(
                packet.width, packet.height
            ),
            on_end=bridge.pipeline_ended.emit,
            on_error=lambda exc: bridge.pipeline_failed.emit(str(exc)),
        ),
    )

    window = MirrorWindow(
        widget,
        config,
        device_name=session.device_name,
        channel=channel,
        stats_provider=lambda: _status_text(pipeline, mailbox, config),
    )

    bridge.frame_ready.connect(widget.refresh)
    bridge.session_changed.connect(widget.set_video_size)
    bridge.session_changed.connect(window.on_video_session)
    bridge.pipeline_ended.connect(window.show_pipeline_end)
    bridge.pipeline_failed.connect(window.show_pipeline_error)
    bridge.clipboard_received.connect(window.set_clipboard_from_device)

    width, height = _DEFAULT_WINDOW
    window.resize(width, height)
    window.show()

    # Release GL resources while the context is still alive: Qt does not do it
    # for us when a window closes.
    app.aboutToQuit.connect(widget.release_gpu_resources)

    pipeline.start()
    _install_sigint_handler(app)

    try:
        return app.exec()
    finally:
        widget.release_gpu_resources()  # no-op if aboutToQuit already ran
        pipeline.stop()
        if channel is not None:
            channel.stop()


def _status_text(
    pipeline: VideoPipeline, mailbox: FrameMailbox, config: SessionConfig
) -> str:
    stats = pipeline.stats
    parts = [
        f"{stats.width}x{stats.height}",
        f"{stats.fps:.1f} fps",
        f"lag {stats.lag_ms:.0f} ms",
        f"decode {stats.average_decode_ms:.1f} ms",
        config.video_codec,
    ]
    if mailbox.overwritten:
        parts.append(f"dropped {mailbox.overwritten}")
    if stats.frames:
        parts.append(f"avg {stats.bytes_received / stats.frames / 1024:.0f} KiB/frame")
    return "  ·  ".join(parts)


def _discard(_payload: bytes) -> None:
    """Placeholder control sink when control is disabled."""


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
    app._mirror_screen_signal_timer = timer  # keep a reference alive


__all__ = [
    "UiBridge",
    "configure_surface_format",
    "run",
    "start_device_session",
]
