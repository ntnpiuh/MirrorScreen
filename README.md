# Mirror Screen

Mirror an Android phone's screen to a MacBook (or Linux/Windows desktop) with
**native resolution and low latency**. Mostly Python, no Android app to install,
nothing to root.

```
┌──────────────┐   USB (adb)    ┌───────────────────────────────────────┐
│ Android      │  ───────────►  │ MacBook                               │
│ MediaCodec   │  H.264/H.265   │ adb → demux → PyAV decode → Qt OpenGL │
│ hardware     │  + input events│ YUV shader, no CPU colour conversion  │
│ encoder      │  ◄───────────  │                                       │
└──────────────┘                └───────────────────────────────────────┘
```

## Why this design

Three ways exist to get an Android screen onto a desktop, and only one of them
meets a "high resolution + low latency" bar:

| Approach | Result |
| --- | --- |
| `adb shell screencap` in a loop | ~2-5 fps, 300 ms+, unusable |
| `adb shell screenrecord` | seconds behind, and it re-encodes on the CPU |
| **The scrcpy server protocol** (used here) | 1080p60+ from the phone's **hardware** encoder, single-digit-to-low-tens of ms |

So Mirror Screen is a from-scratch Python **client for the scrcpy server
protocol**: it pushes the pinned, signed `scrcpy-server.jar` to the device, runs
it as `shell` via `app_process`, and speaks the wire protocol directly. The
device's hardware encoder does the heavy lifting; this process only demuxes,
decodes with FFmpeg (through PyAV, which releases the GIL) and uploads the three
YUV planes straight to the GPU, where a fragment shader does the colour
conversion.

The latency budget is deliberately short: the newest decoded frame **replaces**
any frame the UI has not picked up yet, so a slow repaint costs a dropped frame
instead of growing delay. Nothing is buffered or queued.

## Requirements

- macOS, Linux or Windows; Python **3.11+**
- A phone with **USB debugging** enabled
- `adb` — if you do not have it, `mirror-screen setup` downloads Google's
  official platform-tools into a cache directory
- `ffmpeg` is only needed by `mirror-screen selftest` (decoding uses PyAV's own
  bundled FFmpeg)

## Install

```bash
git clone <this repo> && cd MirrorScreen
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
source .venv/bin/activate          # optional, so 'mirror-screen' is on PATH
```

## Quick start

```bash
mirror-screen setup       # fetch adb + the pinned scrcpy server (once)
mirror-screen devices     # confirm the phone is visible
mirror-screen probe       # decode for 5s and report; no window, proves the setup
mirror-screen             # start mirroring
```

On the phone: **Settings → About phone → tap Build number 7 times**, then
**Developer options → USB debugging**, and accept the "Allow USB debugging"
prompt when you plug it in.

## Controls

| Input | Device action |
| --- | --- |
| Left click / drag | tap / swipe |
| Right click | Back |
| Middle click | Home |
| Scroll wheel, two-finger scroll | scroll (flip with `--scroll-scale -1`) |
| Keyboard | key events (US layout; printable characters fall back to text injection) |
| `F11` / `Cmd+F` / `Ctrl+F` | toggle full screen |
| `Esc` | leave full screen (otherwise sent to the device) |
| `Cmd/Ctrl+R` | rotate the device |
| `Cmd/Ctrl+P` | toggle the device screen on/off |
| `Cmd/Ctrl+V` | send the Mac clipboard to the device and paste |
| `Cmd/Ctrl+S` | save a full-resolution PNG screenshot |
| `Cmd/Ctrl+Q` | quit |

## Tuning

```bash
# Everything at native resolution, high bit rate, uncapped frame rate
mirror-screen --max-size 0 --bit-rate 40000000

# Lowest latency: no vsync, no stats, nearest-neighbour scaling
mirror-screen --no-vsync --no-stats --nearest

# Crisp pixel-perfect scaling for reading text or pixel art
mirror-screen --integer-scale --nearest

# Cap the bandwidth for a weak USB link
mirror-screen --max-size 1280 --max-fps 60 --bit-rate 8000000

# H.265 when the device supports it (smaller stream, more decode work)
mirror-screen --codec h265

# Wrong colours? Force the matrix/range the device is really using
mirror-screen --color-matrix bt601 --color-range full
```

Run `mirror-screen --help` for the full list, including `--codec-option` for
passing raw `MediaCodec` options such as `profile:1,i-frame-interval:10`.

### Where the latency goes

`--no-vsync` is the single biggest software knob: with vsync on, a frame can wait
up to one refresh interval (16.7 ms at 60 Hz) before it is shown. Everything else
is already minimal — no buffering, no queueing, no full-frame CPU copies.

The status bar shows two figures measured entirely on this machine's clock, which
is why they can be trusted:

- **`decode`** — milliseconds of CPU spent decoding each frame.
- **`% idle`** — share of pipeline time spent *waiting* for the device rather
  than working. Near 100% means the device cannot feed frames faster than we
  consume them, so the software adds no backlog; if it drops, decoding has
  become the bottleneck and delay will start to accumulate.

End-to-end latency (device encode + USB + decode + present) cannot be measured
from this side without synchronising two unrelated clocks. scrcpy estimates it by
comparing the host clock against device timestamps, but that figure also grows
linearly from device clock quantisation — measured here at ~25 ms per second on a
phone stamping frames at a nominal 60 Hz while actually delivering 58.5 fps. So
`probe` prints that raw comparison separately, clearly labelled, and it is
**not** a latency measurement.

## Architecture

```
src/mirror_screen/
├── protocol/          wire format, verified against scrcpy 4.1 sources
│   ├── const.py       codec ids, packet flags, Android input constants
│   ├── io.py          byte sources, big-endian pack/unpack, fixed point
│   ├── framing.py     session + media packet demuxing
│   ├── control.py     control messages (touch, keys, clipboard, …)
│   └── device_msg.py  messages coming back from the device
├── adb/               platform-tools discovery/install, device listing, adb wrapper
├── scrcpy/            server jar download + process launch + socket handshake
├── video/             PyAV decoding, frame planes, the pipeline thread
├── ui/                geometry, colour maths, GL renderer, widget, window
├── control_channel.py input sink + device message reader
├── app.py             wires device + decoder + UI together
├── selfcheck.py       device-free end-to-end verification
└── cli.py             command line interface
```

### Protocol notes

The protocol is internal to scrcpy and changes between releases, so the server
jar is **pinned to v4.1** and the client refuses to talk to a different version
(it detects scrcpy 2.x framing and says so explicitly). Everything implemented
here was verified against the scrcpy 4.1 sources, and the non-obvious parts are
documented where they live:

- **Socket order matters**: video, then audio, then control — the server accepts
  them in exactly that order, and with `tunnel_forward=true` it writes a dummy
  byte after each accept (which is also how the client detects readiness).
- **Stream metadata** is a 4-byte codec id followed by a session packet. This
  was the one detail the published documentation does not pin down, and getting
  it wrong is invisible until you plug in a phone: a real device was observed
  sending `68 32 36 34` ("h264"), then `80 00 00 00` (session flag), then the
  width and height. The self-check now builds its synthetic streams the same
  way, so the unit tests cover it too.
- **Session packets** announce the capture size and are re-sent on rotation;
  bytes 4..8 and 8..12 of the 12-byte header carry the new width and height.
- **Two tunnel modes.** By default the client listens and the device connects
  out to it (`adb reverse`), exactly like scrcpy, so there is no startup race.
  `--force-adb-forward` flips it (`adb forward`), where adb completes the local
  connect even before the device is listening, so the client must retry — and a
  retry that the client abandons can still be accepted by the server, consuming
  one of the strict video/audio/control accept slots and aborting the session.
  That race is real and was observed on hardware; prefer the default.
- **Config packets** (SPS/PPS) are prepended to the next media packet for
  H.264/H.265, mirroring scrcpy's packet merger.
- **PyAV packs `yuv420p` planes back-to-back** with no row padding, so slicing
  chroma as `packed[h:h+ch, :cw]` yields garbage. `video/frame.py` documents the
  real layout, which is pinned down by tests.

## Testing

```bash
.venv/bin/python -m pytest        # 100+ unit and integration tests
mirror-screen selftest            # full pipeline check, no phone required
```

`probe` is the quickest way to check a real phone without opening a window. It
starts the server, decodes for a few seconds, reports what it saw, and can render
a frame to a PNG:

```bash
mirror-screen probe --duration 6 --screenshot phone.png
```

It also proves the input path works, using a read-only clipboard round trip on
the control socket (falling back to a write that the server must acknowledge).

`selftest` builds a lossless H.264 clip from a known pattern, wraps it in a
synthetic scrcpy stream (including a mid-stream resolution change), then runs the
real demuxer, decoder, colour maths and GPU shader over it, comparing the
rendered result against the source colours to within 1/255. It also writes a PNG
of the rendered frame so the result can be eyeballed:

```
ok   session packets parsed: 2 sessions, sizes [(320, 240), (240, 320)]
ok   decoded YUV matches the source: largest deviation 0.5 (tolerance 2)
ok   shader output matches the reference: largest deviation 1.0/255
ok   image is not vertically flipped
```

## Verified on real hardware

Against a Samsung Galaxy A17 (SM-A176B, Android 16) over USB:

```
video          1080x2340 h264
frames         447 in 8.0s (58.5 fps)
decode         1.64 ms/frame average
keeping up     90.6% idle (waiting vs decoding)
throughput     148 MPix/s
stream         3.91 Mbit/s
control        ok (write acknowledged; the device clipboard now holds ...)
```

A screenshot rendered from the live stream matches the phone's screen exactly.

## Known limitations

- **No audio playback yet.** The audio socket can be opened (`--audio`) and the
  protocol constants are in place, but nothing plays OPUS on the host.
- Video comes from `MediaCodec`, so only codecs the device can encode are
  usable (H.264 always; H.265/AV1/VP8/VP9 on supporting hardware).
- Decoding is software (FFmpeg): 1080p is far from the limit (a 1080x2340 stream
  decodes in ~1.6 ms per frame). PyAV reports `videotoolbox` as an available
  hardware device, so hardware decode is the next step for 4K if needed.
- The keyboard map assumes a US layout. Printable characters without a key code
  are injected as text.
- No gamepad/UHID support and no clipboard auto-sync: clipboard transfer is
  manual (`Cmd/Ctrl+V`), though device clipboard changes are mirrored to the Mac.
- One device at a time; pick it with `--serial` when several are attached.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `no Android device found` | Unlock the phone, re-accept the USB debugging prompt, check `mirror-screen devices` |
| `device is unauthorized` | Accept the prompt on the phone; if it never appears, revoke USB debugging authorizations and replug |
| Version mismatch error | Delete the cache (`~/Library/Caches/mirror-screen`) and re-run `mirror-screen setup` |
| Black window | The device screen may be off — press `Cmd/Ctrl+P` or power the phone on |
| Washed-out or tinted colours | Try `--color-matrix bt601` or `--color-range full` |
| Feels laggy | Add `--no-vsync`, reduce `--max-size`, raise `--bit-rate`, or prefer H.264 |

## Credits

The wire protocol, the server jar and the overall approach come from
[scrcpy](https://github.com/Genymobile/scrcpy) by Romain Vimont (Genymobile),
licensed under Apache-2.0. Mirror Screen is an independent client and downloads
the official server release rather than bundling it.
