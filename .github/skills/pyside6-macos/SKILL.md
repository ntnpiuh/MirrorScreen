---
name: pyside6-macos
description: "Use when building or debugging PySide6 desktop UI, QThread or worker boundaries, Qt signals and slots, OpenGL rendering, Retina scaling, macOS dark mode, menu-bar integration, or Screen Recording and Accessibility permission handling."
---
# PySide6 and macOS Desktop UI

Use this skill for responsive desktop behavior and native macOS integration.

## Threading and rendering
- Keep QApplication and all QWidget, QOpenGLWidget, texture upload, and paint operations on the GUI thread.
- Keep ADB, socket reads, decode, and blocking device operations off the GUI thread. Communicate with typed Qt signals or the repository's existing callback boundary.
- Prefer the existing `threading.Thread` and Qt signal patterns unless a `QThread` migration solves a demonstrated lifecycle problem.
- Make worker shutdown explicit and test that stop/join cannot deadlock while the UI is closing.
- Coalesce repaint requests when frame rate exceeds UI capacity; do not create an unbounded queued signal backlog.

## macOS behavior
- Preserve device-pixel-ratio correctness for Retina displays and avoid assuming logical pixels equal framebuffer pixels.
- Detect appearance changes through Qt APIs; do not hard-code a dark-only palette.
- Treat Menu Bar Extra, Screen Recording, and Accessibility integration as optional capabilities with clear failure states.
- Keep platform-specific imports behind narrow adapters so Linux and Windows imports remain testable.
- Do not request permissions repeatedly or claim that a permission was granted without checking the platform result.

## Verification
- Test geometry, resize, rotation, input mapping, and close/reconnect behavior without requiring a real display when possible.
- Run the relevant unit tests first. For UI changes, run a headless/self-check path where available and manually verify on macOS when a display and permission are required.
- Profile paint/upload duration before optimizing rendering code.

## References
- Project UI: `src/mirror_screen/ui/` and `src/mirror_screen/app.py`.
- PySide6 documentation: https://doc.qt.io/qtforpython/
