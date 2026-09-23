"""Decoded video frames in a form the GPU renderer can upload directly.

Keeping Y, U and V separate means the renderer uploads three single-channel
textures and does the colour conversion in a fragment shader, which avoids a
full-frame CPU conversion per frame.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..errors import DecodeError

# avutil AVColorSpace
_AVCOL_SPC_BT709 = 1
_AVCOL_SPC_BT470BG = 5
_AVCOL_SPC_SMPTE170M = 6

# avutil AVColorRange
_AVCOL_RANGE_MPEG = 2  # limited (16-235 / 16-240)
_AVCOL_RANGE_JPEG = 3  # full (0-255)


@dataclass(frozen=True, slots=True)
class VideoFrame:
    """One decoded frame, split into planes."""

    width: int
    height: int
    y: np.ndarray
    u: np.ndarray
    v: np.ndarray
    pts_us: int = 0
    key_frame: bool = False
    color_matrix: str = "auto"
    color_range: str = "auto"

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def nbytes(self) -> int:
        return self.y.nbytes + self.u.nbytes + self.v.nbytes

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"VideoFrame({self.width}x{self.height}, pts={self.pts_us}us, "
            f"key={self.key_frame})"
        )


def split_yuv420p_planes(
    packed: np.ndarray, width: int, height: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Split a PyAV ``yuv420p`` array into its Y, U and V planes.

    PyAV's ``to_ndarray(format="yuv420p")`` returns a C-contiguous
    ``(height * 3 // 2, width)`` uint8 array in which the planes are stored
    **tightly packed, back to back**, with no row padding::

        [0, w*h)                  Y, shape (h, w)
        [w*h, w*h + cw*ch)        U, shape (ch, cw)
        [w*h + cw*ch, + cw*ch)    V, shape (ch, cw)

    So U's rows are ``w/2`` bytes apart inside a buffer whose rows are ``w``
    bytes apart: slicing with ``packed[h:h + ch, :cw]`` yields interleaved
    *garbage*, not the chroma plane. This layout was verified empirically
    against PyAV 18.1 with a horizontally split red/blue source, and
    :func:`tests.test_frame` pins it down.
    """
    chroma_width = (width + 1) // 2
    chroma_height = (height + 1) // 2

    expected = (height + chroma_height, width)
    if packed.shape != expected:
        raise DecodeError(
            f"unexpected packed frame shape {packed.shape}, expected {expected}"
        )

    y_size = width * height
    chroma_size = chroma_width * chroma_height
    flat = packed.reshape(-1)

    if flat.size < y_size + 2 * chroma_size:
        raise DecodeError(
            f"frame buffer too small: {flat.size} < {y_size + 2 * chroma_size}"
        )

    y = flat[:y_size].reshape(height, width)
    u = flat[y_size : y_size + chroma_size].reshape(chroma_height, chroma_width)
    v = flat[y_size + chroma_size : y_size + 2 * chroma_size].reshape(
        chroma_height, chroma_width
    )
    return y, u, v


def frame_from_av(
    av_frame,
    *,
    pts_us: int = 0,
    key_frame: bool = False,
) -> VideoFrame:
    """Convert a PyAV ``VideoFrame`` into a :class:`VideoFrame`.

    Non-4:2:0 pixel formats (for example 10-bit HEVC) are converted first, which
    costs one extra copy but keeps the shader path single-format.
    """
    if av_frame.format.name != "yuv420p":
        av_frame = av_frame.reformat(format="yuv420p")

    packed = av_frame.to_ndarray(format="yuv420p")
    y, u, v = split_yuv420p_planes(packed, av_frame.width, av_frame.height)

    return VideoFrame(
        width=av_frame.width,
        height=av_frame.height,
        y=y,
        u=u,
        v=v,
        pts_us=pts_us,
        key_frame=key_frame,
        color_matrix=_matrix_hint(av_frame),
        color_range=_range_hint(av_frame),
    )


def _matrix_hint(av_frame) -> str:
    """Best guess at the colour matrix, from the frame's own metadata."""
    colorspace = getattr(av_frame, "colorspace", None)
    if colorspace in (_AVCOL_SPC_BT470BG, _AVCOL_SPC_SMPTE170M):
        return "bt601"
    if colorspace == _AVCOL_SPC_BT709:
        return "bt709"
    # Unspecified: mirror what MediaCodec does, i.e. SD is BT.601 and HD is
    # BT.709. The user can always override with --color-matrix.
    return "bt709" if av_frame.height >= 720 else "bt601"


def _range_hint(av_frame) -> str:
    """Best guess at the colour range, from the frame's own metadata."""
    color_range = getattr(av_frame, "color_range", None)
    if color_range == _AVCOL_RANGE_JPEG:
        return "full"
    if color_range == _AVCOL_RANGE_MPEG:
        return "limited"
    # Android's encoder defaults to limited range for camera and display
    # content alike.
    return "limited"
