"""Session supervision and stream diagnostics."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

import mirror_screen.video.pipeline as pipeline_module
import mirror_screen.app as app_module
from mirror_screen.app import SessionRunner, _status_text, reconnect_delays
from mirror_screen.audio import AudioStats
from mirror_screen.config import SessionConfig
from mirror_screen.scrcpy.launcher import ServerSession
from mirror_screen.video.frame import VideoFrame
from mirror_screen.video.pipeline import FrameMailbox, PipelineStats, VideoPipeline


def test_reconnect_delays_back_off_then_hold():
    """Retries must ease off and then keep trying at a steady, cheap rate."""
    delays = list(itertools.islice(reconnect_delays(), 8))
    assert delays == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0]
    assert delays == sorted(delays)


def test_reconnect_delays_never_end():
    """The window waits for the device for as long as it is open."""
    delays = reconnect_delays()
    for _ in range(200):
        next(delays)


def _frame() -> VideoFrame:
    y = np.zeros((4, 4), dtype=np.uint8)
    chroma = np.zeros((2, 2), dtype=np.uint8)
    return VideoFrame(width=4, height=4, y=y, u=chroma, v=chroma)


@pytest.fixture
def fake_clock(monkeypatch):
    """Drive the pipeline's clock so timing is deterministic."""
    clock = {"now": 0.0}
    monkeypatch.setattr(pipeline_module.time, "perf_counter", lambda: clock["now"])
    return clock


def test_frame_gaps_are_recorded(fake_clock):
    """A stall between frames has to be visible in the statistics."""
    pipeline = VideoPipeline(object(), SessionConfig(), FrameMailbox())  # type: ignore[arg-type]
    frame = _frame()

    # 20 ms, then 50 ms, then a 500 ms stall.
    for now in (0.0, 0.02, 0.07, 0.57):
        fake_clock["now"] = now
        pipeline._publish(frame)

    stats = pipeline.stats
    assert stats.frames == 4
    assert stats.max_frame_gap_ms == pytest.approx(500.0)
    assert stats.long_frame_gaps == 1


def test_steady_stream_reports_no_long_gaps(fake_clock):
    pipeline = VideoPipeline(object(), SessionConfig(), FrameMailbox())  # type: ignore[arg-type]
    frame = _frame()

    for index in range(60):
        fake_clock["now"] = index / 60.0  # a smooth 60 fps
        pipeline._publish(frame)

    assert pipeline.stats.long_frame_gaps == 0
    assert pipeline.stats.max_frame_gap_ms == pytest.approx(1000.0 / 60.0, abs=0.5)
    assert pipeline.stats.fps == pytest.approx(60.0, rel=0.05)


def test_seconds_since_last_frame_tracks_the_clock(fake_clock):
    pipeline = VideoPipeline(object(), SessionConfig(), FrameMailbox())  # type: ignore[arg-type]
    fake_clock["now"] = 10.0
    pipeline._publish(_frame())

    fake_clock["now"] = 11.25
    assert pipeline.seconds_since_last_frame() == pytest.approx(1.25)


def test_rolling_rate_uses_the_sampled_window():
    """The UI rates must be rolling, not an average since startup.

    A cumulative average folds in every second the stream was down (during a
    reconnect, say) and makes a perfectly healthy UI look like it cannot keep
    up with the device.
    """
    from collections import deque

    from mirror_screen.ui.widget import _rolling_rate

    assert _rolling_rate(deque()) == 0.0
    assert _rolling_rate(deque([1.0])) == 0.0
    # 2 intervals over 1 second.
    assert _rolling_rate(deque([0.0, 0.5, 1.0])) == pytest.approx(2.0)
    # A long idle gap falling out of the window restores the real rate.
    assert _rolling_rate(deque([0.0, 1.0, 1.0 + 1 / 60, 1.0 + 2 / 60])) == pytest.approx(
        120.0, rel=0.01
    )


class _StubRunner:
    """Just enough of SessionRunner for the status line."""

    def __init__(self, stats: PipelineStats | None, quiet: float = 0.0) -> None:
        self._stats = stats
        self._quiet = quiet

    def stats(self) -> PipelineStats | None:
        return self._stats

    def seconds_since_last_frame(self) -> float:
        return self._quiet

    def audio_stats(self) -> AudioStats | None:
        return None


def test_status_line_waits_before_the_first_frame():
    text = _status_text(_StubRunner(None), FrameMailbox(), SessionConfig())
    assert text == "waiting for video"


def test_status_line_reports_the_useful_numbers():
    stats = PipelineStats(
        frames=500, fps=58.4, width=1080, height=2340, decode_seconds=0.5
    )
    text = _status_text(_StubRunner(stats), FrameMailbox(), SessionConfig())
    assert "1080x2340" in text
    assert "58.4 fps" in text
    assert "decode 1.0 ms" in text
    assert "% idle" in text


def test_status_line_says_when_nothing_has_arrived():
    """A quiet stream is worth showing: it is normal on a static screen, and it
    is also what a stalled encoder looks like."""
    stats = PipelineStats(frames=500, fps=0.0, width=1080, height=2340)
    text = _status_text(_StubRunner(stats, quiet=3.2), FrameMailbox(), SessionConfig())
    assert "no frames for 3.2s" in text


def test_status_line_mentions_stalls_and_drops():
    stats = PipelineStats(
        frames=500, fps=58.0, width=1080, height=2340, long_frame_gaps=7
    )
    mailbox = FrameMailbox()
    mailbox.put(_frame())
    mailbox.put(_frame())  # the first one is now reported as dropped

    text = _status_text(_StubRunner(stats), mailbox, SessionConfig())
    assert "gaps>250ms: 7" in text
    assert "dropped 1" in text


def test_status_line_reports_audio_startup_and_fault_counters():
    class AudioRunner(_StubRunner):
        def audio_stats(self) -> AudioStats:
            return AudioStats(startup_ms=42.0, dropped=2, underruns=1)

    text = _status_text(
        AudioRunner(PipelineStats(frames=1, width=1080, height=2340)),
        FrameMailbox(),
        SessionConfig(),
    )
    assert "audio start 42 ms" in text
    assert "audio dropped 2" in text
    assert "audio underruns 1" in text


def test_frame_mailbox_keeps_only_the_newest_frame():
    mailbox = FrameMailbox()
    first = _frame()
    second = _frame()

    mailbox.put(first)
    mailbox.put(second)

    assert mailbox.take() is second
    assert mailbox.take() is None
    assert mailbox.overwritten == 1


def test_session_runner_teardown_is_ordered_and_idempotent(tmp_path):
    events: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        def stop(self) -> None:
            events.append(self.name)

    runner = SessionRunner(
        SessionConfig(),
        FrameMailbox(),
        adb_path=tmp_path / "adb",
        jar=tmp_path / "server.jar",
    )
    runner._pipeline = Resource("video")  # type: ignore[assignment]
    runner._audio = Resource("audio")  # type: ignore[assignment]
    runner._channel = Resource("control")  # type: ignore[assignment]
    runner._server = Resource("server")  # type: ignore[assignment]

    runner._teardown()
    runner._teardown()

    assert events == ["video", "audio", "control", "server"]


def test_session_runner_cleans_partial_startup_in_reverse_ownership_order(
    tmp_path, monkeypatch
):
    events: list[str] = []

    class FakeServer:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> ServerSession:
            events.append("server.start")
            return ServerSession(
                device_name="test-device",
                serial="test-serial",
                local_port=0,
                scid=1,
                video=object(),
                audio=None,
                control=object(),
            )

        def stop(self) -> None:
            events.append("server.stop")

    class FakeChannel:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            events.append("control.start")

        def stop(self) -> None:
            events.append("control.stop")

    class FailingPipeline:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            events.append("video.start")
            raise RuntimeError("decoder setup failed")

        def stop(self) -> None:
            events.append("video.stop")

    monkeypatch.setattr(app_module, "ScrcpyServer", FakeServer)
    monkeypatch.setattr(app_module, "ControlChannel", FakeChannel)
    monkeypatch.setattr(app_module, "VideoPipeline", FailingPipeline)

    runner = SessionRunner(
        SessionConfig(),
        FrameMailbox(),
        adb_path=tmp_path / "adb",
        jar=tmp_path / "server.jar",
    )

    with pytest.raises(RuntimeError, match="decoder setup failed"):
        runner._open_session()

    assert events == [
        "server.start",
        "control.start",
        "video.start",
        "video.stop",
        "control.stop",
        "server.stop",
    ]


def test_async_startup_failure_is_reported_and_torn_down(tmp_path, monkeypatch):
    messages: list[str] = []
    teardown_calls: list[bool] = []
    runner = SessionRunner(
        SessionConfig(),
        FrameMailbox(),
        adb_path=tmp_path / "adb",
        jar=tmp_path / "server.jar",
    )
    runner.startup_failed.connect(messages.append)
    monkeypatch.setattr(
        runner, "_open_session", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    monkeypatch.setattr(runner, "_teardown", lambda: teardown_calls.append(True))

    runner._start_and_supervise()

    assert messages == ["boom"]
    assert teardown_calls == [True]
