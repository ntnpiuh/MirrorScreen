# Mirror Screen

Mirror an Android device on a desktop over USB with a Python client for the
scrcpy server protocol. The device uses its hardware encoder; the client
decodes the stream with PyAV and renders YUV planes with Qt/OpenGL. No Android
app or root access is required.

## User

### Requirements

- macOS, Linux, or Windows
- Python 3.11 or newer for source installs
- An Android device with USB debugging enabled
- `adb`; `mirror-screen setup` can download Google's official platform-tools

The desktop must be able to run PySide6 and OpenGL. `ffmpeg` is only needed by
the `selftest` command; video decoding uses the FFmpeg libraries bundled with
PyAV.

### Install

```bash
git clone <this-repository> && cd MirrorScreen
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
source .venv/bin/activate
```

### Start mirroring

Enable **Developer options > USB debugging** on the phone and accept the USB
debugging prompt. Then run:

```bash
mirror-screen setup       # install adb and the pinned scrcpy server
mirror-screen devices     # check that the phone is authorized and visible
mirror-screen             # open the mirroring window
```

The first run downloads the tools into the Mirror Screen cache. Use
`mirror-screen --help` for all options. If several devices are connected, add
`--serial SERIAL`.

For a settings window before connecting, use:

```bash
mirror-screen ui
```

The settings window provides link-quality, resolution, FPS, bitrate, vsync,
render scheduling, and audio controls. The stream window is dedicated to the
phone display; closing it stops the session and returns to the main control
window.

On macOS, `Mirror Screen.command` can also be double-clicked in Finder:

```bash
./"Mirror Screen.command" --max-size 1280 --no-vsync
```

### Audio

Audio is disabled by default. Enable it with `--audio`:

```bash
mirror-screen --audio
mirror-screen --audio --audio-output OUTPUT_ID --mute-phone-audio
```

Forwarded audio plays through the system default output unless an output device
is selected. The phone keeps playing audio by default; use
`--mute-phone-audio` to play it only on the desktop. Output-device enumeration
depends on the available PySide6 multimedia backend, and the system default is
always the fallback. If audio cannot start, video continues and the error is
reported.

### Controls

| Input | Action |
| --- | --- |
| Left click / drag | Tap / swipe |
| Right click | Back |
| Middle click | Home |
| Scroll wheel / trackpad | Scroll; use `--scroll-scale -1` to reverse it |
| Keyboard | Send key events; printable characters use text injection when needed |
| `F11`, `Cmd+F`, `Ctrl+F` | Toggle full screen |
| `Esc` | Leave full screen, otherwise send Escape to the device |
| `Cmd/Ctrl+R` | Rotate the device |
| `Cmd/Ctrl+0` | Refit the window after rotation or resizing |
| `Cmd/Ctrl+P` | Toggle the device display |
| `Cmd/Ctrl+V` | Send the desktop clipboard and paste |
| `Cmd/Ctrl+S` | Save a full-resolution PNG screenshot |
| `Cmd/Ctrl+Q` | Quit |

### Tuning and diagnostics

```bash
mirror-screen --max-size 0 --bit-rate 40000000  # native size, high bitrate
mirror-screen --no-vsync --no-stats             # prioritize latency
mirror-screen --integer-scale --nearest         # crisp pixel scaling
mirror-screen --max-size 1280 --max-fps 60 --bit-rate 8000000
mirror-screen --codec h265                      # when supported by the device
mirror-screen --trace                            # log rolling pipeline metrics
```

The client keeps only the newest decoded frame. A slow repaint drops stale
frames instead of building latency. `--no-vsync` can remove up to one display
refresh interval of latency, at the cost of possible tearing. Rotation and
folding update the capture size and window aspect ratio automatically unless
`--no-auto-resize` is used.

Use `probe` to test a real device without opening a window:

```bash
mirror-screen probe --duration 6 --screenshot phone.png
```

Use `selftest` to verify demuxing, decoding, colour conversion, and rendering
without a device:

```bash
mirror-screen selftest
```

Common fixes:

- `no Android device found`: unlock the phone, reconnect USB, and accept the
  debugging prompt.
- Unauthorized device: revoke USB debugging authorizations on the phone and
  accept the new prompt.
- Version mismatch: remove the Mirror Screen cache and run `mirror-screen setup`.
- Stutter: try `--no-vsync`, a smaller `--max-size`, a higher bitrate, or H.264.
- Incorrect colours: try `--color-matrix bt601` or `--color-range full`.

### macOS app bundle

Install PyInstaller and build locally:

```bash
.venv/bin/python -m pip install pyinstaller
./packaging/build_macos_app.sh
```

The script creates `dist/Mirror Screen.app`, a platform-named ZIP, and
`dist/SHA256SUMS.txt`. Verify the archive with:

```bash
shasum -a 256 -c dist/SHA256SUMS.txt
```

The app is not signed or notarized. The first launch may still download `adb`
and the pinned server into the user's cache.

## Developer

### Project setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Run the deterministic test suite with:

```bash
.venv/bin/python -m pytest
```

The `tests/` package covers protocol framing, control messages, configuration,
audio transport and buffering, decoding, the low-latency pipeline, UI/session
lifecycle, and device probing. `mirror-screen selftest` is the device-free
end-to-end check. Real-device checks still require an authorized Android phone.

### Architecture

```text
src/mirror_screen/
  adb/       platform-tools discovery, installation, and device commands
  scrcpy/    pinned server download, launch, and socket setup
  protocol/  scrcpy wire framing, constants, control, and device messages
  video/     PyAV decoding, frame planes, and the single-slot frame mailbox
  ui/        settings, window, input mapping, geometry, and OpenGL rendering
  audio.py   audio worker, decode/resample, and bounded low-latency buffering
  audio_sink.py  Qt audio output selection and system-default fallback
  app.py     session startup, supervision, reconnection, and teardown
  cli.py     command-line parsing and command dispatch
```

The server is pinned to scrcpy 4.1. The client speaks its video, audio, and
control socket protocol directly and validates the expected framing. The
default tunnel uses `adb reverse`; `--force-adb-forward` exists for devices
where reverse tunnels are unavailable.

Qt/OpenGL work stays on the GUI thread. Device I/O, decoding, audio, and
session supervision run outside it. The video mailbox has one slot, so new
frames replace stale work. The renderer uploads YUV planes directly and does
colour conversion in the shader rather than converting each frame to RGB on
the CPU.

### Packaging

`packaging/build_macos_app.sh` builds the macOS PyInstaller application and
archive. Generated `build/` and `dist/` output should not be committed. The
repository also contains Windows and macOS entry points for frozen builds.

### Current limitations and validation gaps

- One device is supported per session; select it with `--serial` when needed.
- Keyboard mapping targets a US layout.
- There is no gamepad/UHID support or automatic clipboard synchronization.
- H.264 is the compatibility default; other codecs require device encoder and
  decoder support.
- Output selection depends on the host multimedia backend.
- Manual validation remains for native-resolution performance, rotation/folding,
  reconnects, audio keep/mute behavior, output selection, Retina displays, and
  light/dark mode.

### Credits

The wire protocol, server, and overall approach come from
[scrcpy](https://github.com/Genymobile/scrcpy) by Genymobile, licensed under
Apache-2.0. Mirror Screen is an independent client and downloads the official
server release rather than bundling it.
