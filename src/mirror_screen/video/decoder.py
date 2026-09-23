"""Decode MediaCodec access units with FFmpeg's native decoder (via PyAV)."""

from __future__ import annotations

import logging
from fractions import Fraction

import av

from ..errors import DecodeError
from ..protocol.const import CODECS_MERGE_CONFIG_PACKET, FFMPEG_DECODERS
from ..protocol.framing import MediaPacket
from .frame import VideoFrame, frame_from_av

log = logging.getLogger(__name__)

#: Consecutive failed packets before we give up on the stream.
_MAX_CONSECUTIVE_ERRORS = 30


class VideoDecoder:
    """Stateful decoder for one video stream.

    Threading note: PyAV releases the GIL while decoding, so this can run on a
    dedicated thread without blocking the UI thread.
    """

    def __init__(
        self,
        codec_name: str,
        width: int,
        height: int,
        *,
        low_delay: bool = True,
        thread_type: str = "SLICE",
    ) -> None:
        if codec_name not in FFMPEG_DECODERS:
            raise DecodeError(f"no FFmpeg decoder mapped for codec {codec_name!r}")

        self.codec_name = codec_name
        self.width = width
        self.height = height
        self.low_delay = low_delay
        self.thread_type = thread_type
        self._merge_config = codec_name in CODECS_MERGE_CONFIG_PACKET

        self._ctx: av.CodecContext | None = None
        self._pending_config: bytes | None = None
        self.decode_errors = 0
        self.frames_decoded = 0
        self._consecutive_errors = 0

    # -- lifecycle ----------------------------------------------------------
    def _create_context(self) -> av.CodecContext:
        decoder_name = FFMPEG_DECODERS[self.codec_name]
        try:
            ctx = av.CodecContext.create(decoder_name, "r")
        except Exception as exc:  # pragma: no cover - depends on the FFmpeg build
            raise DecodeError(f"could not create a {decoder_name} decoder: {exc}") from exc

        if self.low_delay:
            # Do not hold frames back to reorder them: scrcpy's encoder never
            # emits B-frames, so there is nothing to reorder.
            try:
                ctx.flags |= av.codec.context.Flags.low_delay
            except Exception as exc:  # pragma: no cover - old PyAV
                log.debug("could not set the low-delay flag: %s", exc)

        try:
            # Frame-level threading would add a whole frame of latency.
            ctx.thread_type = self.thread_type
        except Exception as exc:  # pragma: no cover - old PyAV
            log.debug("could not set thread_type=%s: %s", self.thread_type, exc)

        ctx.width = self.width
        ctx.height = self.height
        return ctx

    def reset(self, width: int, height: int) -> None:
        """Recreate the decoder after a session change (rotation, resize)."""
        log.debug("resetting decoder for %dx%d", width, height)
        self.width = width
        self.height = height
        self._ctx = None
        self._pending_config = None
        self._consecutive_errors = 0

    def close(self) -> None:
        self._ctx = None
        self._pending_config = None

    # -- decoding -----------------------------------------------------------
    def decode(self, packet: MediaPacket) -> list[VideoFrame]:
        """Decode one access unit, returning any frames it completed."""
        if packet.config:
            if self._merge_config:
                # Mirror scrcpy's packet merger: prepend the codec
                # configuration (SPS/PPS) to the next media packet so the
                # decoder sees it in-band with the frame that needs it.
                self._pending_config = packet.payload
                return []
            payload = packet.payload
        elif self._pending_config is not None:
            payload = self._pending_config + packet.payload
            self._pending_config = None
        else:
            payload = packet.payload

        ctx = self._ctx or self._create_context()
        self._ctx = ctx

        av_packet = av.Packet(payload)
        av_packet.pts = packet.pts_us
        av_packet.time_base = Fraction(1, 1_000_000)

        try:
            decoded = ctx.decode(av_packet)
        except av.AVError as exc:
            self._handle_decode_error(exc)
            return []

        self._consecutive_errors = 0
        self.frames_decoded += len(decoded)
        return [
            frame_from_av(
                av_frame,
                pts_us=packet.pts_us,
                key_frame=packet.key_frame,
            )
            for av_frame in decoded
        ]

    def decode_payload(self, payload: bytes, pts_us: int = 0, key_frame: bool = False):
        """Decode a raw access unit (used by the self-check)."""
        return self.decode(
            MediaPacket(payload=payload, pts_us=pts_us, config=False, key_frame=key_frame)
        )

    def _handle_decode_error(self, exc: Exception) -> None:
        self.decode_errors += 1
        self._consecutive_errors += 1
        if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            raise DecodeError(
                f"decoder failed on {self._consecutive_errors} consecutive packets: {exc}"
            ) from exc
        log.warning("decode error (%s), skipping packet", exc)
