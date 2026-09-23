"""Colour conversion tests.

The decode matrices must be the exact inverse of the standard encoder formulas,
otherwise correctly decoded YUV values would show up as wrong colours.
"""

from __future__ import annotations

import pytest

from mirror_screen.ui.color import get_conversion, resolve

# Textbook encoder outputs for solid colours, in 8-bit YUV, limited range.
# These are what FFmpeg's swscale and Android's encoders produce.
REFERENCE = {
    "bt601": {
        (1.0, 0.0, 0.0): (81, 90, 240),  # red
        (0.0, 0.0, 1.0): (41, 240, 110),  # blue
        (0.0, 1.0, 0.0): (145, 54, 34),  # green
        (1.0, 1.0, 1.0): (235, 128, 128),  # white
        (0.0, 0.0, 0.0): (16, 128, 128),  # black
    },
    "bt709": {
        (1.0, 0.0, 0.0): (63, 102, 240),
        (0.0, 0.0, 1.0): (32, 240, 118),
        (0.0, 1.0, 0.0): (173, 42, 26),
        (1.0, 1.0, 1.0): (235, 128, 128),
    },
}


@pytest.mark.parametrize("matrix", ["bt601", "bt709"])
@pytest.mark.parametrize("rgb", [(1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)])
def test_encode_matches_reference_encoders(matrix, rgb):
    """Our RGB -> YUV inverse must agree with the standard coefficients."""
    conversion = get_conversion(matrix, "limited")
    y, u, v = conversion.encode(*rgb)
    expected_y, expected_u, expected_v = REFERENCE[matrix][rgb]
    assert y * 255 == pytest.approx(expected_y, abs=1.0)
    assert u * 255 == pytest.approx(expected_u, abs=1.5)
    assert v * 255 == pytest.approx(expected_v, abs=1.5)


@pytest.mark.parametrize("matrix", ["bt601", "bt709"])
@pytest.mark.parametrize("color_range", ["limited", "full"])
@pytest.mark.parametrize(
    "rgb",
    [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
        (1.0, 1.0, 1.0),
        (0.0, 0.0, 0.0),
        (0.5, 0.25, 0.75),
    ],
)
def test_encode_decode_round_trip(matrix, color_range, rgb):
    """Encoding then decoding a colour must return it unchanged."""
    conversion = get_conversion(matrix, color_range)
    yuv = conversion.encode(*rgb)
    decoded = conversion.apply(*yuv)
    assert decoded == pytest.approx(rgb, abs=1e-9)


def test_white_and_black_are_neutral_in_both_matrices():
    """Neutral colours must have zero chroma, or the picture would be tinted."""
    for matrix in ("bt601", "bt709"):
        conversion = get_conversion(matrix, "limited")
        white = conversion.encode(1.0, 1.0, 1.0)
        black = conversion.encode(0.0, 0.0, 0.0)
        assert white[1] * 255 == pytest.approx(128.0, abs=1.0)
        assert white[2] * 255 == pytest.approx(128.0, abs=1.0)
        assert black[1] * 255 == pytest.approx(128.0, abs=1.0)
        assert black[2] * 255 == pytest.approx(128.0, abs=1.0)


def test_columns_are_the_transpose_of_rows():
    """GLSL builds ``mat3`` from columns, so the ordering must be flipped."""
    conversion = get_conversion("bt709", "limited")
    for row in range(3):
        for col in range(3):
            assert conversion.columns[col][row] == conversion.rows[row][col]


def test_resolve_honours_overrides():
    assert resolve("bt601", "full").name == "bt601-full"
    # "auto" defers to what the frame itself declares.
    assert resolve("auto", "auto", frame_matrix="bt601", frame_range="full").name == (
        "bt601-full"
    )
    # An explicit override wins over the frame metadata.
    assert resolve("bt709", "limited", frame_matrix="bt601").name == "bt709-limited"


def test_get_conversion_falls_back_to_bt709_limited():
    assert get_conversion("auto", "auto").name == "bt709-limited"
    assert get_conversion("nonsense", "nonsense").name == "bt709-limited"
