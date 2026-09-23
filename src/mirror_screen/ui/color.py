"""YUV -> RGB conversion matrices, shared by the shader and the tests.

The device sends YCbCr, so the GPU has to convert it. Doing this in a fragment
shader keeps the frame path copy-free; the exact matrices are defined here so a
CPU reference implementation can validate the shader.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

# Rows of the conversion matrix, applied as ``rgb = M @ (yuv - offset)``.
_MATRICES: dict[tuple[str, str], tuple[tuple[tuple[float, float, float], ...], tuple[float, float, float]]] = {
    ("bt601", "limited"): (
        (
            (1.164383, 0.000000, 1.596027),
            (1.164383, -0.391762, -0.812968),
            (1.164383, 2.017232, 0.000000),
        ),
        (16.0 / 255.0, 0.5, 0.5),
    ),
    ("bt601", "full"): (
        (
            (1.000000, 0.000000, 1.402000),
            (1.000000, -0.344136, -0.714136),
            (1.000000, 1.772000, 0.000000),
        ),
        (0.0, 0.5, 0.5),
    ),
    ("bt709", "limited"): (
        (
            (1.164383, 0.000000, 1.792741),
            (1.164383, -0.213249, -0.532909),
            (1.164383, 2.112402, 0.000000),
        ),
        (16.0 / 255.0, 0.5, 0.5),
    ),
    ("bt709", "full"): (
        (
            (1.000000, 0.000000, 1.574800),
            (1.000000, -0.187324, -0.468124),
            (1.000000, 1.855600, 0.000000),
        ),
        (0.0, 0.5, 0.5),
    ),
}


@dataclass(frozen=True, slots=True)
class ColorConversion:
    """A resolved conversion, ready to hand to the shader."""

    name: str
    rows: tuple[tuple[float, float, float], ...]
    offset: tuple[float, float, float]

    @property
    def columns(self) -> tuple[tuple[float, float, float], ...]:
        """The same matrix in column-major order, as GLSL ``mat3`` wants."""
        return tuple(
            tuple(self.rows[row][col] for row in range(3)) for col in range(3)
        )

    def inverse_rows(self) -> tuple[tuple[float, float, float], ...]:
        """The inverse matrix, i.e. RGB -> YUV (values in ``[0, 1]``)."""
        return _inverse_of(self)

    def encode(self, red: float, green: float, blue: float) -> tuple[float, float, float]:
        """Encode an RGB triple into Y, U, V.

        Used to build test patterns, and to prove that the decode matrices are
        consistent with the standard encoder definitions.
        """
        inverse = self.inverse_rows()
        values = (red, green, blue)
        yuv = tuple(
            sum(inverse[row][col] * values[col] for col in range(3)) + self.offset[row]
            for row in range(3)
        )
        return yuv  # type: ignore[return-value]

    def apply(self, y: float, u: float, v: float) -> tuple[float, float, float]:
        """CPU reference conversion (values in ``[0, 1]``)."""
        c0, c1, c2 = self.columns
        sy = y - self.offset[0]
        su = u - self.offset[1]
        sv = v - self.offset[2]
        rgb = tuple(
            max(0.0, min(1.0, c0[i] * sy + c1[i] * su + c2[i] * sv))
            for i in range(3)
        )
        return rgb  # type: ignore[return-value]


#: Inverses are computed once per matrix/range pair.
_INVERSES: dict[str, tuple[tuple[float, float, float], ...]] = {}


def _inverse_of(conversion: ColorConversion) -> tuple[tuple[float, float, float], ...]:
    """Return the inverse of a conversion's matrix, caching the result."""
    cached = _INVERSES.get(conversion.name)
    if cached is None:
        inverse = np.linalg.inv(np.array(conversion.rows, dtype=float))
        cached = tuple(tuple(float(value) for value in row) for row in inverse)
        _INVERSES[conversion.name] = cached
    return cached


@lru_cache(maxsize=8)
def get_conversion(matrix: str = "bt709", color_range: str = "limited") -> ColorConversion:
    """Return the conversion for a matrix/range pair.

    ``"auto"`` falls back to the most common Android combination: BT.709 with
    limited range.
    """
    resolved_matrix = "bt601" if matrix == "bt601" else "bt709"
    resolved_range = "full" if color_range == "full" else "limited"
    rows, offset = _MATRICES[(resolved_matrix, resolved_range)]
    return ColorConversion(
        name=f"{resolved_matrix}-{resolved_range}", rows=rows, offset=offset
    )


def resolve(
    matrix_override: str,
    range_override: str,
    *,
    frame_matrix: str = "auto",
    frame_range: str = "auto",
) -> ColorConversion:
    """Pick the conversion to use, honouring user overrides."""
    matrix = frame_matrix if matrix_override == "auto" else matrix_override
    color_range = frame_range if range_override == "auto" else range_override
    return get_conversion(matrix, color_range)
