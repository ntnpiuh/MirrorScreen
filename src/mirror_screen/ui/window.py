"""The application window: video widget, status line and keyboard shortcuts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QMainWindow, QMessageBox

from ..config import SessionConfig
from ..control_channel import ControlChannel
from .widget import VideoWidget

log = logging.getLogger(__name__)

_STATUS_INTERVAL_MS = 500


class MirrorWindow(QMainWindow):
    """Hosts the video widget and the controls around it."""

    def __init__(
        self,
        widget: VideoWidget,
        config: SessionConfig,
        *,
        device_name: str,
        channel: ControlChannel | None,
        stats_provider: Callable[[], str] | None = None,
        screenshot_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._widget = widget
        self._config = config
        self._device_name = device_name
        self._channel = channel
        self._stats_provider = stats_provider
        self._screenshot_dir = screenshot_dir or _default_screenshot_dir()
        self._display_on = True
        self._fitted_to_video = False

        self.setCentralWidget(widget)
        self.setWindowTitle(f"Mirror Screen - {device_name}")
        self.statusBar().showMessage("connecting...")

        self._build_actions()

        if config.always_on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        if stats_provider is not None and config.show_stats:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._update_status)
            self._timer.start(_STATUS_INTERVAL_MS)

        if config.fullscreen:
            self.showFullScreen()

    # -- actions ------------------------------------------------------------
    def _build_actions(self) -> None:
        def add(
            text: str, shortcuts: str | list[str], slot: Callable[[], None]
        ) -> QAction:
            action = QAction(text, self)
            names = [shortcuts] if isinstance(shortcuts, str) else shortcuts
            action.setShortcuts([QKeySequence(name) for name in names])
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(slot)
            self.addAction(action)
            return action

        add(
            "Toggle full screen",
            ["F11", "Ctrl+Meta+F", "Ctrl+F", "Meta+F"],
            self.toggle_fullscreen,
        )
        # Only grabs Escape while actually full screen, so the key still
        # reaches the device the rest of the time.
        self._escape_action = add("Leave full screen", "Esc", self.leave_fullscreen)
        add("Rotate device", ["Ctrl+R", "Meta+R"], self.rotate_device)
        add("Toggle device screen", ["Ctrl+P", "Meta+P"], self.toggle_display_power)
        add("Paste host clipboard", ["Ctrl+V", "Meta+V"], self.push_clipboard)
        add("Save screenshot", ["Ctrl+S", "Meta+S"], self.save_screenshot)
        add("Quit", ["Ctrl+Q", "Meta+Q"], self.close)
        self._update_escape_action()

    # -- slots --------------------------------------------------------------
    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_escape_action()

    def _update_escape_action(self) -> None:
        action = getattr(self, "_escape_action", None)
        if action is not None:
            action.setEnabled(self.isFullScreen())

    def leave_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        self._update_escape_action()

    def rotate_device(self) -> None:
        if self._channel is not None:
            self._channel.rotate_device()

    def toggle_display_power(self) -> None:
        if self._channel is None:
            return
        self._display_on = not self._display_on
        self._channel.set_display_power(self._display_on)

    def push_clipboard(self) -> None:
        if self._channel is None:
            return
        text = QGuiApplication.clipboard().text()
        if text:
            self._channel.set_clipboard(text, paste=True)

    def save_screenshot(self) -> None:
        image = self._widget.screenshot()
        if image.isNull():
            self.statusBar().showMessage("nothing to capture yet", 3000)
            return
        name = datetime.now().strftime("mirror-screen-%Y%m%d-%H%M%S.png")
        path = self._screenshot_dir / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(str(path))
        except OSError as exc:
            self.statusBar().showMessage(f"could not save screenshot: {exc}", 5000)
            return
        log.info("screenshot written to %s", path)
        self.statusBar().showMessage(f"saved {path.name}", 4000)

    def on_video_session(self, width: int, height: int) -> None:
        """Fit the window to the video aspect ratio on the first session."""
        if self._fitted_to_video or self.isFullScreen() or self._config.fullscreen:
            return
        if width <= 0 or height <= 0:
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return

        available = screen.availableGeometry()
        scale = min(
            available.width() * 0.9 / width,
            available.height() * 0.9 / height,
            1.0,
        )
        if scale <= 0:
            return

        self._fitted_to_video = True
        chrome = self.statusBar().sizeHint().height()
        self.resize(
            max(320, int(width * scale)),
            max(240, int(height * scale) + chrome),
        )

    def set_clipboard_from_device(self, text: str) -> None:
        """Adopt the device clipboard, unless it already matches ours."""
        clipboard = QGuiApplication.clipboard()
        if clipboard.text() != text:
            clipboard.setText(text)

    def show_pipeline_error(self, message: str) -> None:
        self.statusBar().showMessage(f"stream stopped: {message}")

    def show_pipeline_end(self, reason: str) -> None:
        self.statusBar().showMessage(reason)

    def show_fatal_error(self, message: str) -> None:
        QMessageBox.critical(self, "Mirror Screen", message)

    def _update_status(self) -> None:
        if self._stats_provider is not None:
            self.statusBar().showMessage(self._stats_provider())


def _default_screenshot_dir() -> Path:
    pictures = Path.home() / "Pictures"
    return pictures if pictures.is_dir() else Path.cwd()
