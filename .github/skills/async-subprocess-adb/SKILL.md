---
name: async-subprocess-adb
description: "Use when implementing or debugging asyncio subprocess wrappers, adb server/device lifecycle, scrcpy process management, USB or Wi-Fi discovery, offline devices, stream stdout/stderr, cancellation, or non-blocking system I/O in Mirror Screen."
---
# Async Subprocess and ADB

Use this skill for system-process and device-lifecycle work in Mirror Screen.

## Repository fit
- Inspect `src/mirror_screen/adb/`, `src/mirror_screen/scrcpy/`, `control_channel.py`, and nearby tests before changing process behavior.
- Preserve the existing synchronous public API unless an async boundary is explicitly required. Do not introduce an event loop into the Qt GUI thread casually.
- Treat ADB and the on-device scrcpy server as separate lifecycles: discover, validate, start, stream, stop, and clean up each explicitly.

## Implementation rules
- Prefer `asyncio.create_subprocess_exec` for new async wrappers; never build shell command strings when argument arrays are sufficient.
- Drain stdout and stderr concurrently, bound captured output, and report exit code, signal, timeout, and device serial in errors.
- Make cancellation idempotent: cancel readers, terminate the child, wait with a deadline, then kill only as a final step.
- Do not block the Qt event loop with `subprocess.run`, `communicate`, or synchronous ADB polling. Move blocking compatibility code to a worker or retain the existing thread boundary.
- Parse `adb devices -l` defensively. Distinguish missing device, unauthorized, offline, transport failure, and multiple-device ambiguity.
- For Wi-Fi devices, validate the `host:port` shape and do not silently reconnect forever. Use bounded backoff and an observable retry state.
- Never log clipboard contents, authentication material, or complete raw protocol payloads.

## Verification
- Add deterministic tests with fake process/device outputs for success, timeout, cancellation, unauthorized, offline, and non-zero exit paths.
- Run the narrow ADB/process tests first, then `pytest` for shared lifecycle changes.
- For real-device checks, record the required ADB state and keep them separate from the default test suite.

## References
- Project implementation: `src/mirror_screen/adb/` and `src/mirror_screen/scrcpy/`.
- Upstream skill catalog: https://github.com/Paldom/python-skills
- Related catalog: https://github.com/VoltAgent/awesome-agent-skills
