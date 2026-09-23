"""Mapping between widget pixels and device video pixels.

Kept free of Qt so the maths can be unit tested.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rect:
    """An axis-aligned rectangle in widget pixels (top-left origin)."""

    x: float
    y: float
    width: float
    height: float

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2.0

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2.0

    def contains(self, px: float, py: float) -> bool:
        return (
            self.x <= px < self.x + self.width and self.y <= py < self.y + self.height
        )


def fit_scale(
    video_width: int,
    video_height: int,
    view_width: int,
    view_height: int,
    *,
    scale: float = 1.0,
    integer_scale: bool = False,
) -> float:
    """Return the factor mapping video pixels onto view pixels.

    The result always preserves the aspect ratio. ``integer_scale`` snaps the
    factor to a whole number so that one video pixel covers exactly N screen
    pixels, which keeps text and 1px lines crisp; if no whole number fits, it
    falls back to the fractional fit.
    """
    if video_width <= 0 or video_height <= 0 or view_width <= 0 or view_height <= 0:
        return 1.0

    base = min(view_width / video_width, view_height / video_height) * scale
    if base <= 0:
        return 1.0

    if integer_scale:
        whole = int(base)
        if whole >= 1:
            return float(whole)

    return base


def video_layout(
    video_width: int,
    video_height: int,
    view_width: int,
    view_height: int,
    *,
    scale: float = 1.0,
    integer_scale: bool = False,
) -> Rect:
    """Return where the video should be drawn inside the view, centred."""
    factor = fit_scale(
        video_width,
        video_height,
        view_width,
        view_height,
        scale=scale,
        integer_scale=integer_scale,
    )
    width = video_width * factor
    height = video_height * factor
    return Rect(
        x=(view_width - width) / 2.0,
        y=(view_height - height) / 2.0,
        width=width,
        height=height,
    )


def device_point(
    layout: Rect,
    video_width: int,
    video_height: int,
    px: float,
    py: float,
) -> tuple[int, int]:
    """Map a widget pixel onto a device video coordinate.

    Coordinates are clamped to the video area, so a drag that leaves the window
    still produces valid events (the touch stays at the edge).
    """
    if layout.width <= 0 or layout.height <= 0:
        return 0, 0

    dx = (px - layout.x) * video_width / layout.width
    dy = (py - layout.y) * video_height / layout.height

    x = int(min(max(dx, 0.0), max(video_width - 1, 0)))
    y = int(min(max(dy, 0.0), max(video_height - 1, 0)))
    return x, y


def quad_transform(
    layout: Rect, view_width: int, view_height: int
) -> tuple[float, float, float, float]:
    """Return the ``(scale_x, scale_y, offset_x, offset_y)`` quad placement.

    The drawn quad is a unit square spanning ``[-0.5, 0.5]``, and its vertices
    carry texture coordinates where ``v = 0`` is the *top* row of the video.
    OpenGL's clip space points ``y`` upwards while ``layout`` is measured
    downwards from the top of the window, so the sign of ``scale_y`` is what
    decides whether the picture is upright:

    * ``scale_y > 0`` — the vertex carrying ``v = 0`` lands in the upper half of
      the viewport, so the video appears the right way up.
    * ``scale_y < 0`` — the quad is mirrored, and the video appears **upside
      down** even though the letterboxing looks correct.

    That second case was a real bug (the image was mirrored on screen while the
    offscreen screenshots looked fine, because a compensating flip in the
    readback cancelled it out). Keeping the maths here, with
    ``tests/test_geometry.py`` asserting the top edge of the video lands exactly
    on the top edge of ``layout``, is what stops it coming back.
    """
    if view_width <= 0 or view_height <= 0:
        return (1.0, 1.0, 0.0, 0.0)
    return (
        layout.width * 2.0 / view_width,
        layout.height * 2.0 / view_height,
        layout.center_x * 2.0 / view_width - 1.0,
        1.0 - layout.center_y * 2.0 / view_height,
    )


@dataclass(frozen=True, slots=True)
class WindowFit:
    """A window size that shows a video at a particular scale."""

    width: int
    height: int
    scale: float


def display_scale(
    window_width: int,
    window_height: int,
    video_width: int,
    video_height: int,
    *,
    chrome_height: int = 0,
) -> float:
    """Recover the scale a window is currently showing a video at."""
    view_width = max(window_width, 1)
    view_height = max(window_height - chrome_height, 1)
    if video_width <= 0 or video_height <= 0:
        return 1.0
    return min(view_width / video_width, view_height / video_height)


def fit_window_to_video(
    video_width: int,
    video_height: int,
    *,
    max_width: int,
    max_height: int,
    chrome_height: int = 0,
    preferred_scale: float | None = None,
    min_width: int = 320,
    min_height: int = 240,
) -> WindowFit:
    """Choose a window size that shows the whole video, chrome included.

    ``preferred_scale`` keeps the apparent size when the video changes shape —
    which is what makes a rotation resize the window instead of shrinking the
    picture. ``None`` fits the video into the available area instead. Either
    way the result never exceeds ``max_width`` x ``max_height``.
    """
    if video_width <= 0 or video_height <= 0 or max_width <= 0 or max_height <= 0:
        return WindowFit(min_width, min_height, 1.0)

    tallest_video = max(max_height - chrome_height, 1)
    fitted = min(max_width / video_width, tallest_video / video_height)
    scale = fitted if preferred_scale is None else min(preferred_scale, fitted)
    scale = max(scale, 0.01)

    width = max(int(round(video_width * scale)), min_width)
    height = max(int(round(video_height * scale)) + chrome_height, min_height)
    return WindowFit(width, height, scale)
