"""Optional QtMultimedia audio output adapter."""

from __future__ import annotations

from typing import Any

from .audio import AudioChunk, AudioError


def audio_output_devices() -> list[tuple[str, str]]:
    """Return ``(id, description)`` pairs, or an empty list without Qt audio."""
    try:
        from PySide6.QtMultimedia import QMediaDevices
    except ImportError:
        return []
    return [(_device_id(device), device.description()) for device in QMediaDevices.audioOutputs()]


def default_audio_output_id() -> str | None:
    """Return the platform default id when QtMultimedia is available."""
    try:
        from PySide6.QtMultimedia import QMediaDevices
    except ImportError:
        return None
    return _device_id(QMediaDevices.defaultAudioOutput())


class QtAudioSink:
    """Push signed 16-bit PCM into Qt's platform audio backend.

    ``__init__`` only picks the output device; it does no Qt Multimedia work
    yet, so it is safe to construct off the GUI thread. The underlying
    ``QAudioSink`` is created lazily by :meth:`start`, and every other method
    (``write``, ``flush``, ``close``) must run on that same thread afterwards
    - :class:`AudioWorker` owns that contract by calling all of them from its
    own worker thread and never from whatever thread asks it to stop.
    """

    def __init__(self, output_id: str | None = None) -> None:
        try:
            from PySide6.QtMultimedia import QAudioSink, QMediaDevices
        except ImportError as exc:
            raise AudioError(
                "audio playback requires the PySide6 QtMultimedia backend"
            ) from exc

        devices = QMediaDevices.audioOutputs()
        selected = QMediaDevices.defaultAudioOutput()
        if output_id is not None:
            for device in devices:
                if _device_id(device) == output_id:
                    selected = device
                    break
            else:
                raise AudioError(f"audio output device not found: {output_id!r}")
        self._audio_sink_type = QAudioSink
        self._sink: Any = None
        self._io: Any = None
        self._selected = selected

    def start(self, sample_rate: int, channels: int) -> None:
        from PySide6.QtMultimedia import QAudioFormat

        fmt = QAudioFormat()
        fmt.setSampleRate(sample_rate)
        fmt.setChannelCount(channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        sink = self._audio_sink_type(self._selected, fmt)
        sink.setBufferSize(max(sample_rate * channels * 2 // 10, 4096))
        self._sink = sink
        self._io = sink.start()
        if self._io is None:
            raise AudioError("Qt could not start the audio output")

    def write(self, chunk: AudioChunk) -> None:
        if self._io is None:
            return
        self._io.write(chunk.data)

    def flush(self) -> None:
        if self._sink is not None:
            self._sink.reset()

    def close(self) -> None:
        if self._sink is not None:
            self._sink.stop()
        self._io = None
        self._sink = None


def _device_id(device: Any) -> str:
    raw = device.id()
    return bytes(raw).decode("utf-8", errors="replace")


__all__ = ["QtAudioSink", "audio_output_devices", "default_audio_output_id"]