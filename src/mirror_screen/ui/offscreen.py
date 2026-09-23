"""Render a frame into an offscreen framebuffer.

Used by the self-check and by screenshot tooling: it exercises the exact same
shader path as the live window, but without needing a visible window.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import (
    QGuiApplication,
    QImage,
    QOffscreenSurface,
    QOpenGLContext,
    QSurfaceFormat,
)
from PySide6.QtOpenGL import (
    QOpenGLFramebufferObject,
    QOpenGLFramebufferObjectFormat,
)

from ..errors import MirrorScreenError
from ..video.frame import VideoFrame
from .color import ColorConversion, get_conversion
from .geometry import video_layout
from .gl_renderer import YuvQuadRenderer

class OffscreenRenderer:
    """Draws frames into an offscreen buffer and hands back a ``QImage``."""

    def __init__(self, *, filter_mode: str = "linear") -> None:
        self._filter_mode = filter_mode
        self._surface: QOffscreenSurface | None = None
        self._context: QOpenGLContext | None = None
        self._renderer: YuvQuadRenderer | None = None
        self._fbo: QOpenGLFramebufferObject | None = None

    def __enter__(self) -> OffscreenRenderer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # -- lifecycle ----------------------------------------------------------
    def _ensure_context(self) -> None:
        if self._context is not None:
            return

        if QGuiApplication.instance() is None:
            raise MirrorScreenError(
                "an offscreen render needs a Qt application; create a "
                "QGuiApplication or QApplication first"
            )

        fmt = QSurfaceFormat.defaultFormat()
        if fmt.majorVersion() < 3:
            fmt = QSurfaceFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)

        surface = QOffscreenSurface()
        surface.setFormat(fmt)
        surface.create()
        if not surface.isValid():
            raise MirrorScreenError("could not create an offscreen surface")

        context = QOpenGLContext()
        context.setFormat(fmt)
        if not context.create():
            raise MirrorScreenError("could not create an offscreen GL context")

        self._surface = surface
        self._context = context

        if not context.makeCurrent(surface):
            raise MirrorScreenError("could not make the offscreen context current")
        self._renderer = YuvQuadRenderer(filter_mode=self._filter_mode)
        self._renderer.initialize()

    def close(self) -> None:
        if self._context is not None and self._surface is not None:
            self._context.makeCurrent(self._surface)
            if self._renderer is not None:
                self._renderer.release()
            self._fbo = None
            self._context.doneCurrent()
        self._renderer = None
        self._context = None
        self._surface = None

    # -- rendering ----------------------------------------------------------
    def render(
        self,
        frame: VideoFrame,
        width: int,
        height: int,
        *,
        conversion: ColorConversion | None = None,
        scale: float = 1.0,
        integer_scale: bool = False,
    ) -> QImage:
        """Render ``frame`` into a ``width x height`` image."""
        self._ensure_context()
        assert self._context is not None and self._surface is not None
        assert self._renderer is not None

        self._context.makeCurrent(self._surface)

        if conversion is None:
            conversion = get_conversion(frame.color_matrix, frame.color_range)

        fbo_format = QOpenGLFramebufferObjectFormat()
        fbo_format.setAttachment(QOpenGLFramebufferObject.Attachment.NoAttachment)
        fbo_format.setSamples(0)
        if self._fbo is None or self._fbo.size() != QSize(width, height):
            self._fbo = QOpenGLFramebufferObject(width, height, fbo_format)

        self._fbo.bind()
        try:
            self._renderer.set_viewport(width, height)
            self._renderer.clear()
            layout = video_layout(
                frame.width,
                frame.height,
                width,
                height,
                scale=scale,
                integer_scale=integer_scale,
            )
            self._renderer.render(frame, layout, (width, height), conversion)
            # Reading an FBO back yields rows bottom-up relative to what the
            # window shows, so mirror it. Without this, screenshots and the
            # self-check would see an upside-down image.
            image = _upright(self._fbo.toImage())
        finally:
            self._fbo.release()

        assert self._surface is not None
        self._context.doneCurrent()
        return image

    def save(
        self,
        frame: VideoFrame,
        path: Path,
        width: int,
        height: int,
        *,
        conversion: ColorConversion | None = None,
    ) -> Path:
        image = self.render(frame, width, height, conversion=conversion)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not image.save(str(path)):
            raise MirrorScreenError(f"could not write {path}")
        return path


def _upright(image: QImage) -> QImage:
    """Return the image with row 0 at the top of the frame."""
    try:
        return image.flipped(Qt.Orientation.Vertical)
    except (AttributeError, TypeError):  # pragma: no cover - older Qt
        return image.mirrored(False, True)


__all__ = ["OffscreenRenderer"]
