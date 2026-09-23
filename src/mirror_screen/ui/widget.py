"""The widget that shows the video stream and turns input into touch events."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from ..config import SessionConfig
from ..protocol import control
from ..protocol.const import (
    ACTION_DOWN,
    ACTION_MOVE,
    ACTION_UP,
    BUTTON_PRIMARY,
    BUTTON_SECONDARY,
    BUTTON_TERTIARY,
    POINTER_ID_MOUSE,
)
from ..video.frame import VideoFrame
from ..video.pipeline import FrameMailbox
from .color import resolve
from .geometry import Rect, device_point, video_layout
from .gl_renderer import YuvQuadRenderer
from .offscreen import OffscreenRenderer
from .keymap import (
    KEYCODE_HOME,
    is_printable,
    keycode_for_qt_key,
    metastate_for_modifiers,
)

log = logging.getLogger(__name__)

#: Wheel deltas: Qt reports angle deltas in eighths of a degree with 120 per
#: notch, and pixel deltas for trackpads.
_ANGLE_PER_NOTCH = 120.0
#: Trackpad pixels that correspond to one wheel notch.
_PIXELS_PER_NOTCH = 40.0


class VideoWidget(QOpenGLWidget):
    """Displays decoded frames and forwards input to the device."""

    def __init__(
        self,
        config: SessionConfig,
        mailbox: FrameMailbox,
        send_control: Callable[[bytes], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._mailbox = mailbox
        self._send = send_control

        self._renderer = YuvQuadRenderer(filter_mode=config.filter_mode)
        self._frame: VideoFrame | None = None
        self._video_size: tuple[int, int] = (
            config.max_size or 0,
            0,
        )
        self._pressed_buttons = 0
        self._key_repeats: dict[int, int] = {}
        self._display_on = True

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(320, 240)

        self._conversion = resolve("bt709", "limited")

    # -- frame plumbing -----------------------------------------------------
    def set_video_size(self, width: int, height: int) -> None:
        self._video_size = (width, height)

    def refresh(self) -> None:
        """Called when new frames are available."""
        self.update()

    @property
    def current_frame(self) -> VideoFrame | None:
        return self._frame

    @property
    def video_size(self) -> tuple[int, int]:
        if self._frame is not None:
            return self._frame.size
        return self._video_size

    def screenshot(self) -> QImage:
        """Render the current frame at its native resolution.

        Going through the offscreen renderer rather than grabbing the widget's
        framebuffer gives a full-resolution image, and is the readback path
        whose orientation is actually verified by the self-check.
        """
        frame = self._frame
        if frame is None:
            return QImage()

        conversion = resolve(
            self._config.color_matrix,
            self._config.color_range,
            frame_matrix=frame.color_matrix,
            frame_range=frame.color_range,
        )
        with OffscreenRenderer(filter_mode="nearest") as renderer:
            return renderer.render(
                frame, frame.width, frame.height, conversion=conversion
            )

    # -- OpenGL -------------------------------------------------------------
    def initializeGL(self) -> None:
        self._renderer.initialize()

        # GPU resources must be freed while the context is current, otherwise
        # Qt warns and the textures leak. aboutToBeDestroyed is emitted with the
        # context current, which is exactly what we need.
        context = self.context()
        if context is not None:
            context.aboutToBeDestroyed.connect(self.release_gpu_resources)

    def release_gpu_resources(self) -> None:
        """Free the renderer's GPU resources; safe to call repeatedly.

        Qt does not reliably emit ``aboutToBeDestroyed`` for a widget's context
        (closing a window does not destroy it), so the application calls this
        explicitly while the context is still usable. Without it, Qt warns
        about textures being destroyed without a current context.
        """
        if not self._renderer.is_initialized:
            return

        made_current = False
        try:
            self.makeCurrent()
            made_current = True
        except Exception:  # pragma: no cover - context already torn down
            log.debug("could not make the GL context current for cleanup")

        try:
            self._renderer.release()
        except Exception:  # pragma: no cover - defensive
            log.debug("renderer cleanup failed", exc_info=True)
        finally:
            if made_current:
                self.doneCurrent()

    def resizeGL(self, width: int, height: int) -> None:
        self._renderer.set_viewport(width, height)

    def paintGL(self) -> None:
        self._renderer.clear()

        frame = self._mailbox.take()
        if frame is not None:
            self._frame = frame
        frame = self._frame
        if frame is None:
            return

        width, height = self.video_size
        if width <= 0 or height <= 0:
            return

        self._conversion = resolve(
            self._config.color_matrix,
            self._config.color_range,
            frame_matrix=frame.color_matrix,
            frame_range=frame.color_range,
        )

        layout = self.current_layout()
        self._renderer.render(
            frame, layout, (self.width(), self.height()), self._conversion
        )

    # -- helpers ------------------------------------------------------------
    def current_layout(self) -> Rect:
        video_width, video_height = self.video_size
        return video_layout(
            video_width,
            video_height,
            max(self.width(), 1),
            max(self.height(), 1),
            scale=self._config.scale,
            integer_scale=self._config.integer_scale,
        )

    def _to_device(self, x: float, y: float) -> tuple[int, int]:
        video_width, video_height = self.video_size
        return device_point(self.current_layout(), video_width, video_height, x, y)

    def _can_send(self) -> bool:
        video_width, video_height = self.video_size
        return video_width > 0 and video_height > 0

    # -- mouse input --------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._can_send():
            return
        button = event.button()
        x, y = self._to_device(event.position().x(), event.position().y())

        if button == Qt.MouseButton.RightButton:
            # Right click is the back button, mirroring scrcpy.
            self._send(control.back_or_screen_on(ACTION_DOWN))
            return
        if button == Qt.MouseButton.MiddleButton:
            self._send(control.inject_keycode(ACTION_DOWN, KEYCODE_HOME))
            self._send(control.inject_keycode(ACTION_UP, KEYCODE_HOME))
            return

        mask = self._button_mask(button)
        self._pressed_buttons |= mask
        self._send_touch(ACTION_DOWN, x, y, pressure=1.0, action_button=mask)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if not self._can_send():
            return
        button = event.button()
        x, y = self._to_device(event.position().x(), event.position().y())

        if button == Qt.MouseButton.RightButton:
            self._send(control.back_or_screen_on(ACTION_UP))
            return

        mask = self._button_mask(button)
        self._pressed_buttons &= ~mask
        self._send_touch(ACTION_UP, x, y, pressure=0.0, action_button=mask)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._can_send() or not self._pressed_buttons:
            return
        x, y = self._to_device(event.position().x(), event.position().y())
        self._send_touch(ACTION_MOVE, x, y, pressure=1.0, action_button=0)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._can_send():
            return

        pixel = event.pixelDelta()
        if pixel.isNull():
            vertical = event.angleDelta().y() / _ANGLE_PER_NOTCH
            horizontal = event.angleDelta().x() / _ANGLE_PER_NOTCH
        else:
            vertical = pixel.y() / _PIXELS_PER_NOTCH
            horizontal = pixel.x() / _PIXELS_PER_NOTCH

        scale = self._config.scroll_scale
        x, y = self._to_device(event.position().x(), event.position().y())
        video_width, video_height = self.video_size
        self._send(
            control.inject_scroll(
                x,
                y,
                video_width,
                video_height,
                hscroll=-horizontal * scale,
                vscroll=-vertical * scale,
                buttons=self._pressed_buttons,
            )
        )
        event.accept()

    def _button_mask(self, button: Qt.MouseButton) -> int:
        if button == Qt.MouseButton.LeftButton:
            return BUTTON_PRIMARY
        if button == Qt.MouseButton.RightButton:
            return BUTTON_SECONDARY
        if button == Qt.MouseButton.MiddleButton:
            return BUTTON_TERTIARY
        return 0

    def _send_touch(
        self,
        action: int,
        x: int,
        y: int,
        *,
        pressure: float,
        action_button: int,
    ) -> None:
        video_width, video_height = self.video_size
        self._send(
            control.inject_touch(
                action,
                POINTER_ID_MOUSE,
                x,
                y,
                video_width,
                video_height,
                pressure=pressure,
                action_button=action_button,
                buttons=self._pressed_buttons,
            )
        )

    # -- keyboard input -----------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._can_send():
            super().keyPressEvent(event)
            return

        qt_key = event.key()
        keycode = keycode_for_qt_key(qt_key)

        if keycode is None:
            text = event.text()
            if is_printable(text) and not event.modifiers():
                self._send(control.inject_text(text))
                return
            super().keyPressEvent(event)
            return

        repeat = self._key_repeats.get(qt_key, 0)
        self._key_repeats[qt_key] = repeat + 1
        # The first press reports repeat 0, like a physical key.
        self._send(
            control.inject_keycode(
                ACTION_DOWN,
                keycode,
                repeat=repeat,
                metastate=metastate_for_modifiers(event.modifiers()),
            )
        )

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        qt_key = event.key()
        keycode = keycode_for_qt_key(qt_key)
        if keycode is None:
            super().keyReleaseEvent(event)
            return

        self._key_repeats.pop(qt_key, None)
        if not self._can_send():
            return
        self._send(
            control.inject_keycode(
                ACTION_UP,
                keycode,
                repeat=0,
                metastate=metastate_for_modifiers(event.modifiers()),
            )
        )
