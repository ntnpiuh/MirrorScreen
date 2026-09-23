"""Layout and coordinate mapping tests."""

from __future__ import annotations

import pytest

from mirror_screen.ui.geometry import (
    Rect,
    device_point,
    display_scale,
    fit_scale,
    fit_window_to_video,
    quad_transform,
    video_layout,
)


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


def test_quad_places_the_video_top_row_at_the_top():
    """The orientation invariant that the upsidedown bug violated.

    The vertex carrying texture coordinate ``v = 0`` (the video's top row) must
    end up above the vertex carrying ``v = 1``, otherwise the picture is drawn
    mirrored. The renderer feeds this same transform to the shader, so this test
    covers what the window shows, not just the offscreen path.
    """
    for video_size in [(1080, 2340), (2340, 1080), (1920, 1080), (640, 480)]:
        for view_size in [(800, 600), (1280, 900), (400, 1000)]:
            layout = video_layout(*video_size, *view_size)
            scale_x, scale_y, offset_x, offset_y = quad_transform(
                layout, *view_size
            )

            assert scale_y > 0, "a negative y scale mirrors the video"
            assert scale_x > 0

            # aPos.y = +0.5 carries v = 0 (the top video row)
            top = 0.5 * scale_y + offset_y
            bottom = -0.5 * scale_y + offset_y
            assert top > bottom

            # ...and it lands exactly on the top edge of the layout, converted
            # from window coordinates (y down) into clip space (y up).
            view_height = view_size[1]
            assert top == pytest.approx(1.0 - 2.0 * layout.y / view_height)
            assert bottom == pytest.approx(
                1.0 - 2.0 * (layout.y + layout.height) / view_height
            )


def test_quad_transform_matches_the_layout_edges_horizontally():
    layout = video_layout(1080, 2340, 800, 600)
    scale_x, _, offset_x, _ = quad_transform(layout, 800, 600)
    assert 0.5 * scale_x + offset_x == pytest.approx(2.0 * (layout.x + layout.width) / 800 - 1.0)
    assert -0.5 * scale_x + offset_x == pytest.approx(2.0 * layout.x / 800 - 1.0)


def test_quad_transform_survives_a_degenerate_viewport():
    assert quad_transform(Rect(0, 0, 10, 10), 0, 0) == (1.0, 1.0, 0.0, 0.0)


def test_rotation_keeps_the_apparent_scale():
    """Rotating the phone must reshuffle the window, not resize the picture."""
    portrait = fit_window_to_video(
        1080, 2340, max_width=1800, max_height=1200, chrome_height=20
    )
    landscape = fit_window_to_video(
        2340, 1080,
        max_width=1800,
        max_height=1200,
        chrome_height=20,
        preferred_scale=portrait.scale,
    )

    assert landscape.scale == pytest.approx(portrait.scale)
    # The window simply swaps its long and short sides (plus the chrome).
    assert landscape.width == pytest.approx(portrait.height - 20, abs=1)
    assert landscape.height - 20 == pytest.approx(portrait.width, abs=1)
    assert landscape.width <= 1800
    assert landscape.height <= 1200


def test_fit_shrinks_when_the_video_no_longer_fits():
    """A rotated video that is too big must be scaled down to stay on screen."""
    fit = fit_window_to_video(
        3840, 2160, max_width=1600, max_height=1000, chrome_height=20
    )
    assert fit.width <= 1600
    assert fit.height <= 1000
    assert fit.width > 0 and fit.height > 0


def test_fit_without_a_preferred_scale_uses_the_available_room():
    fit = fit_window_to_video(
        1080, 2340, max_width=1000, max_height=1000, chrome_height=0
    )
    # Height-limited: 1000 / 2340.
    assert fit.scale == pytest.approx(1000 / 2340)
    assert fit.width == pytest.approx(round(1080 * 1000 / 2340), abs=1)
    assert fit.height == pytest.approx(1000, abs=1)


def test_fit_handles_zero_sized_video():
    fit = fit_window_to_video(0, 0, max_width=1000, max_height=800)
    assert fit.width == 320 and fit.height == 240


def test_display_scale_recovers_the_fitted_scale():
    """The window uses this to notice the user resized it by hand."""
    for video_size in [(1080, 2340), (2340, 1080)]:
        fit = fit_window_to_video(
            *video_size, max_width=1400, max_height=1000, chrome_height=25
        )
        recovered = display_scale(
            fit.width, fit.height, *video_size, chrome_height=25
        )
        assert recovered == pytest.approx(fit.scale, rel=0.01)


def test_display_scale_notices_a_manual_resize():
    fit = fit_window_to_video(
        1080, 2340, max_width=1400, max_height=1000, chrome_height=25
    )
    # Shrinking the window genuinely changes how big the video is shown.
    smaller = display_scale(
        fit.width, fit.height // 2, 1080, 2340, chrome_height=25
    )
    assert abs(smaller - fit.scale) > 0.02 * fit.scale


def test_display_scale_ignores_a_resize_that_does_not_change_the_picture():
    """Documented nuance of the auto-resize rule.

    When the window's height is what limits the video, making it wider changes
    nothing about how the video is displayed, so it is not treated as the user
    taking over the sizing and rotations keep reshaping the window.
    """
    fit = fit_window_to_video(
        1080, 2340, max_width=1400, max_height=1000, chrome_height=25
    )
    wider = display_scale(
        fit.width + 400, fit.height, 1080, 2340, chrome_height=25
    )
    assert wider == pytest.approx(fit.scale)


def test_rect_contains():
    rect = Rect(10, 20, 30, 40)
    assert rect.contains(10, 20)
    assert rect.contains(39.9, 59.9)
    assert not rect.contains(40, 20)
    assert not rect.contains(10, 60)
    assert (rect.center_x, rect.center_y) == (25, 40)
