"""Wire-format constants for the scrcpy server protocol.

The protocol is internal to scrcpy and may change between releases, so the
client and the on-device server must always run with the *exact* same version.
Everything here is verified against scrcpy 4.1 sources:

* ``server/src/main/java/com/genymobile/scrcpy/device/Streamer.java``
* ``app/src/demuxer.c``
* ``app/src/control_msg.c``
* ``app/src/device_msg.h``
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Codec identifiers (four ASCII characters packed big-endian into a u32).
# --------------------------------------------------------------------------
CODEC_H264 = 0x68323634  # "h264"
CODEC_H265 = 0x68323635  # "h265"
CODEC_AV1 = 0x00617631  # "av1"
CODEC_VP8 = 0x00767038  # "vp8"
CODEC_VP9 = 0x00767039  # "vp9"
CODEC_OPUS = 0x6F707573  # "opus"
CODEC_AAC = 0x00616163  # "aac"
CODEC_FLAC = 0x666C6163  # "flac"
CODEC_RAW = 0x00726177  # "raw"

VIDEO_CODECS: dict[str, int] = {
    "h264": CODEC_H264,
    "h265": CODEC_H265,
    "av1": CODEC_AV1,
    "vp8": CODEC_VP8,
    "vp9": CODEC_VP9,
}

AUDIO_CODECS: dict[str, int] = {
    "opus": CODEC_OPUS,
    "aac": CODEC_AAC,
    "flac": CODEC_FLAC,
    "raw": CODEC_RAW,
}

#: PyAV/FFmpeg decoder name for each protocol codec name.
FFMPEG_DECODERS: dict[str, str] = {
    "h264": "h264",
    "h265": "hevc",
    "av1": "av1",
    "vp8": "vp8",
    "vp9": "vp9",
    "opus": "opus",
}

#: Codecs where a MediaCodec config packet (SPS/PPS, VPS) must be prepended to
#: the following media packet, mirroring scrcpy's ``packet_merger``.
CODECS_MERGE_CONFIG_PACKET: frozenset[str] = frozenset({"h264", "h265"})

# --------------------------------------------------------------------------
# Stream framing
# --------------------------------------------------------------------------
#: Every packet on the video/audio sockets is prefixed by a 12-byte header.
PACKET_HEADER_SIZE = 12

#: Header flag: first byte has its MSB set -> session packet (video only).
PACKET_FLAG_SESSION = 1 << 63
#: Header flag: the packet is codec configuration data (not a frame).
PACKET_FLAG_CONFIG = 1 << 62
#: Header flag: the packet is a key frame.
PACKET_FLAG_KEY_FRAME = 1 << 61
#: Bit mask selecting the presentation timestamp from the header flags.
PACKET_PTS_MASK = PACKET_FLAG_KEY_FRAME - 1

#: The device name is a fixed-size, NUL-padded UTF-8 field.
DEVICE_NAME_FIELD_LENGTH = 64

#: Upper bound on a single media packet, as a sanity check on device input.
MAX_PACKET_SIZE = 16 * 1024 * 1024

# --------------------------------------------------------------------------
# Android input constants (android.view.KeyEvent / MotionEvent)
# --------------------------------------------------------------------------
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

# MotionEvent button states
BUTTON_PRIMARY = 1 << 0
BUTTON_SECONDARY = 1 << 1
BUTTON_TERTIARY = 1 << 2
BUTTON_BACK = 1 << 3
BUTTON_FORWARD = 1 << 4

# KeyEvent meta states
META_SHIFT_ON = 0x00000001
META_ALT_ON = 0x00000002
META_CTRL_ON = 0x00001000
META_META_ON = 0x00010000

#: Well-known pointer id: the physical mouse.
POINTER_ID_MOUSE = 0xFFFFFFFFFFFFFFFF
#: Well-known pointer id: a generic finger (not a real touch).
POINTER_ID_GENERIC_FINGER = 0xFFFFFFFFFFFFFFFE
#: Well-known pointer id: the extra virtual pointer used for pinch-to-zoom.
POINTER_ID_VIRTUAL_FINGER = 0xFFFFFFFFFFFFFFFD
