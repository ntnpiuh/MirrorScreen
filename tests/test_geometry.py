"""Layout and coordinate mapping tests."""

from __future__ import annotations

import pytest

from mirror_screen.ui.geometry import Rect, device_point, fit_scale, video_layout


def test_fit_scale_preserves_aspect_ratio():
    # A 16:9 video in a 1000x1000 view is limited by the width.
    assert fit_scale(1920, 1080, 1000, 1000) == pytest.approx(1000 / 1920)


def test_fit_scale_handles_degenerate_inputs():
    assert fit_scale(0, 0, 100, 100) == 1.0
    assert fit_scale(100, 100, 0, 0) == 1.0


def test_layout_is_centred_with_letterboxing():
    layout = video_layout(1920, 1080, 1000, 1000)
    assert layout.width == pytest.approx(1000)
    assert layout.height == pytest.approx(562.5)
    assert layout.x == pytest.approx(0)
    assert layout.y == pytest.approx((1000 - 562.5) / 2)


def test_layout_scales_by_the_zoom_factor():
    layout = video_layout(100, 50, 100, 100, scale=2.0)
    assert layout.width == pytest.approx(200)
    assert layout.height == pytest.approx(100)
    # Zooming larger than the view is allowed; it is centred and clipped.
    assert layout.x == pytest.approx(-50)


def test_integer_scale_snaps_to_whole_pixels():
    # 1000/320 = 3.125 -> snap to 3 so each video pixel covers 3 screen pixels.
    layout = video_layout(320, 240, 1000, 1000, integer_scale=True)
    assert layout.width == pytest.approx(960)
    assert layout.height == pytest.approx(720)


def test_integer_scale_falls_back_when_nothing_fits():
    # A video larger than the view cannot be shown at 1:1 without cropping, so
    # the fractional fit is kept.
    layout = video_layout(2000, 1000, 1000, 1000, integer_scale=True)
    assert layout.width == pytest.approx(1000)


def test_device_point_maps_corners_and_centre():
    layout = video_layout(200, 100, 400, 400)  # 400x200, centred at y=100
    assert device_point(layout, 200, 100, 0, 100) == (0, 0)
    assert device_point(layout, 200, 100, 399, 299) == (199, 99)
    assert device_point(layout, 200, 100, 200, 200) == (100, 50)


def test_device_point_clamps_outside_the_video():
    layout = video_layout(200, 100, 400, 400)
    # Above and below the letterboxed area.
    assert device_point(layout, 200, 100, 200, 0) == (100, 0)
    assert device_point(layout, 200, 100, 200, 399) == (100, 99)
    # Far outside to the left and right.
    assert device_point(layout, 200, 100, -500, 200) == (0, 50)
    assert device_point(layout, 200, 100, 9999, 200) == (199, 50)


def test_device_point_never_exceeds_the_video_bounds():
    layout = video_layout(100, 100, 100, 100)
    for px in range(-10, 120):
        x, _ = device_point(layout, 100, 100, px, 50)
        assert 0 <= x < 100


def test_rect_contains():
    rect = Rect(10, 20, 30, 40)
    assert rect.contains(10, 20)
    assert rect.contains(39.9, 59.9)
    assert not rect.contains(40, 20)
    assert not rect.contains(10, 60)
    assert (rect.center_x, rect.center_y) == (25, 40)
