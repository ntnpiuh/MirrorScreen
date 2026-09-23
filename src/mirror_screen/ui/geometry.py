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
