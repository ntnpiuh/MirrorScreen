---
name: low-latency-video-input
description: "Use when decoding H.264 or H.265 from scrcpy or adb, tuning PyAV/OpenCV low-latency video, handling frame/session metadata, YUV or OpenGL rendering, coordinate transforms, mouse gestures, scrolling, keyboard input, or scrcpy control protocol."
---
# Low-Latency Video and Input

Use this skill for the media pipeline and Mac-to-Android input path.

## Video pipeline
- Inspect `src/mirror_screen/protocol/`, `src/mirror_screen/video/`, and `src/mirror_screen/ui/gl_renderer.py` before changing packet or frame ownership.
- Preserve the scrcpy protocol contract: validate packet sizes, flags, codec/session metadata, dimensions, and truncated reads before handing data to PyAV.
- Keep the newest-frame mailbox semantics. Do not add queues or buffering unless the task explicitly changes the latency contract and includes measurements.
- Avoid CPU color conversion and full-frame copies when the existing YUV/OpenGL path can handle the data.
- Treat codec reconfiguration, rotation, folding, and session packets as normal lifecycle events, not decoder corruption.
- Use monotonic host timestamps for latency and gap measurements; do not compare unrelated device and host clocks as end-to-end latency.

## Input redirection
- Map window coordinates to the current device session dimensions using the same rotation and aspect-ratio transform used for rendering.
- Bound coordinates, normalize gesture duration, and make button/modifier mappings explicit.
- Prefer the existing control channel over shelling out for every event. Never inject untrusted shell text through a command string.
- Preserve clipboard privacy and avoid logging typed text or clipboard contents.

## Verification
- Add malformed-header, oversized-packet, session-change, decoder-reset, coordinate-boundary, gesture, and keyboard mapping tests as appropriate.
- Run protocol/video tests first, then the full test suite for cross-module changes.
- Measure decode, paint, dropped-frame, and request rates before and after performance work.

## References
- Project protocol/video/UI: `src/mirror_screen/protocol/`, `src/mirror_screen/video/`, `src/mirror_screen/ui/`.
- PyAV documentation: https://pyav.org/docs/develop/
