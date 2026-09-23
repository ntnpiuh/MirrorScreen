"""User-facing session configuration.

The split mirrors the two halves of the system: options that are forwarded to
the on-device scrcpy server (bandwidth, resolution, framerate) and options that
only affect the local client (window, scaling, colour handling).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .protocol.const import VIDEO_CODECS

#: Sensible default for USB 2.0/3.0 links: enough for 1440p60 with H.264
#: without starving the encoder.
DEFAULT_VIDEO_BIT_RATE = 24_000_000

_CODEC_OPTION_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


@dataclass(slots=True)
class SessionConfig:
    """Everything needed to start and drive one mirroring session."""

    # -- device / transport -------------------------------------------------
    serial: str | None = None
    adb_path: str | None = None
    cache_dir: Path | None = None

    # -- forwarded to the on-device server ---------------------------------
    video_codec: str = "h264"
    max_size: int = 0
    """Maximum dimension of the video, in pixels. 0 keeps the native size."""
    max_fps: float = 0.0
    """Frame rate cap. 0 lets the device encode as fast as it can."""
    video_bit_rate: int = DEFAULT_VIDEO_BIT_RATE
    audio: bool = False
    control: bool = True
    show_touches: bool = False
    stay_awake: bool = True
    power_on: bool = True
    keep_active: bool = False
    clipboard_autosync: bool = True
    log_level: str = "info"
    video_codec_options: list[tuple[str, str]] = field(default_factory=list)

    # -- client-side presentation ------------------------------------------
    fullscreen: bool = False
    always_on_top: bool = False
    scale: float = 1.0
    """Local zoom factor applied on top of the fit-to-window size."""
    integer_scale: bool = False
    """Prefer crisp integer scaling, falling back to fit when it does not fit."""
    filter_mode: str = "linear"
    """Texture filtering: ``linear`` or ``nearest``."""
    color_matrix: str = "auto"
    """``auto``, ``bt601`` or ``bt709``."""
    color_range: str = "auto"
    """``auto``, ``limited`` or ``full``."""
    scroll_scale: float = 1.0
    """Multiplier applied to scroll wheel / trackpad deltas. Negating it
    reverses the scroll direction."""
    show_stats: bool = True
    vsync: bool = True
    """Wait for the display refresh between frames. Disabling it removes up to
    one refresh interval of latency at the cost of possible tearing."""

    def validate(self) -> None:
        """Raise :class:`ValueError` if the configuration is not usable."""
        if self.video_codec not in VIDEO_CODECS:
            known = ", ".join(sorted(VIDEO_CODECS))
            raise ValueError(
                f"unsupported video codec {self.video_codec!r} (known: {known})"
            )

        if self.max_size < 0:
            raise ValueError("max_size must be >= 0 (0 means native resolution)")
        if self.max_fps < 0:
            raise ValueError("max_fps must be >= 0 (0 means no cap)")

        if self.video_bit_rate <= 0:
            raise ValueError("video_bit_rate must be > 0")

        if self.log_level not in {"verbose", "debug", "info", "warn", "error"}:
            raise ValueError(f"unsupported log level: {self.log_level!r}")

        if self.scale <= 0:
            raise ValueError("scale must be > 0")
        if self.scroll_scale == 0:
            raise ValueError("scroll_scale must not be zero")
        if self.filter_mode not in {"linear", "nearest"}:
            raise ValueError(f"unsupported filter mode: {self.filter_mode!r}")
        if self.color_matrix not in {"auto", "bt601", "bt709"}:
            raise ValueError(f"unsupported color matrix: {self.color_matrix!r}")
        if self.color_range not in {"auto", "limited", "full"}:
            raise ValueError(f"unsupported color range: {self.color_range!r}")

        for key, value in self.video_codec_options:
            if not _CODEC_OPTION_RE.match(key):
                raise ValueError(f"invalid codec option name: {key!r}")
            if "," in value or "=" in value:
                raise ValueError(f"invalid codec option value for {key}: {value!r}")

    @property
    def codec_options_string(self) -> str:
        """Encode ``video_codec_options`` the way the server expects."""
        return ",".join(f"{k}:{v}" for k, v in self.video_codec_options)
