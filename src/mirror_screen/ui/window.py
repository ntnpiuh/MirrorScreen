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
from .geometry import display_scale, fit_window_to_video
from .widget import VideoWidget

log = logging.getLogger(__name__)

_STATUS_INTERVAL_MS = 500

#: Fraction of the display a fitted window is allowed to occupy.
_WINDOW_MARGIN = 0.9
#: Relative tolerance when deciding whether the user resized the window.
_SCALE_TOLERANCE = 0.02


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
        #: Size of the last video session we were told about.
        self._video_size: tuple[int, int] = (0, 0)
        #: The scale we chose last time, so a rotation can keep the apparent size.
        self._auto_scale: float | None = None
        #: Cleared once the user resizes the window by hand.
        self._auto_resize = config.auto_resize_window

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
        add("Fit window to video", ["Ctrl+0", "Meta+0"], self.fit_window_to_video_now)
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
        if not self.isFullScreen():
            return
        self.showNormal()
        # Coming back from full screen, start from a fresh fit rather than the
        # scale we happened to have before.
        self.fit_to_video(*self._video_size, keep_scale=False)

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

    # -- window sizing ------------------------------------------------------
    def on_video_session(self, width: int, height: int) -> None:
        """Keep the window matched to the video, including on rotation.

        The first session sizes the window to the video. A later session means
        the capture size changed (the phone rotated or folded), so the window is
        reshaped to the new aspect ratio while keeping the same apparent scale —
        the picture stays the same size on screen, it just turns.
        """
        previous = self._video_size
        self._video_size = (width, height)

        if width <= 0 or height <= 0 or not self._auto_resize:
            return
        if self.isFullScreen() or self._config.fullscreen:
            return

        if previous != (0, 0) and not self._still_matches_auto_scale(previous):
            # The window is no longer the size we chose, so the user resized it
            # by hand. Stop moving their window; letterbox from here on.
            self._auto_resize = False
            self.statusBar().showMessage(
                "window size left alone because you resized it - press "
                "Cmd/Ctrl+0 to fit it to the video again",
                6000,
            )
            return

        self.fit_to_video(width, height)

    def _still_matches_auto_scale(self, video_size: tuple[int, int]) -> bool:
        """True if the window is still the size we last chose for it.

        Only a resize that changed the displayed scale counts: widening a window
        whose height is the limit leaves the picture exactly as it was, so it is
        not treated as the user taking over the sizing.
        """
        if self._auto_scale is None or video_size[0] <= 0 or video_size[1] <= 0:
            return True
        current = display_scale(
            self.width(),
            self.height(),
            video_size[0],
            video_size[1],
            chrome_height=self._chrome_height(),
        )
        return abs(current - self._auto_scale) <= _SCALE_TOLERANCE * self._auto_scale

    def _chrome_height(self) -> int:
        """Height taken by the window frame and status bar."""
        return self.statusBar().sizeHint().height()

    def available_area(self) -> tuple[int, int]:
        """Largest window we are willing to use, leaving a margin."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:  # pragma: no cover - no display attached
            return (1280, 800)
        area = screen.availableGeometry()
        return (
            int(area.width() * _WINDOW_MARGIN),
            int(area.height() * _WINDOW_MARGIN),
        )

    def fit_to_video(
        self, video_width: int, video_height: int, *, keep_scale: bool = True
    ) -> None:
        """Resize the window to show the video, optionally keeping the scale."""
        if video_width <= 0 or video_height <= 0:
            return
        max_width, max_height = self.available_area()
        fit = fit_window_to_video(
            video_width,
            video_height,
            max_width=max_width,
            max_height=max_height,
            chrome_height=self._chrome_height(),
            preferred_scale=self._auto_scale if keep_scale else None,
        )
        self._auto_scale = fit.scale
        self.resize(fit.width, fit.height)
        self._keep_on_screen()

    def _keep_on_screen(self) -> None:
        """Nudge the window back inside the display after a resize."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:  # pragma: no cover - no display attached
            return
        area = screen.availableGeometry()
        right = max(area.x() + area.width() - self.width(), area.x())
        bottom = max(area.y() + area.height() - self.height(), area.y())
        x = min(max(self.x(), area.x()), right)
        y = min(max(self.y(), area.y()), bottom)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def fit_window_to_video_now(self) -> None:
        """Re-enable automatic sizing and match the window to the video."""
        self._auto_resize = True
        self.fit_to_video(*self._video_size, keep_scale=False)
        self.statusBar().showMessage("window fitted to the video", 3000)

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
