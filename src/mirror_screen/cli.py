"""Command line interface for Mirror Screen."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .adb import Adb, cache_root, ensure_adb
from .config import DEFAULT_VIDEO_BIT_RATE, SessionConfig
from .errors import MirrorScreenError
from .protocol.const import VIDEO_CODECS
from .scrcpy import SCRCPY_VERSION, ensure_server_jar

LOG_LEVELS = ("verbose", "debug", "info", "warn", "error")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mirror-screen",
        description="Mirror an Android screen to this computer, with high "
        "resolution and low latency.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="start mirroring (default)")
    _add_run_options(run)
    run.set_defaults(func=_cmd_run)

    devices = subparsers.add_parser("devices", help="list connected Android devices")
    _add_common_options(devices)
    devices.set_defaults(func=_cmd_devices)

    setup = subparsers.add_parser(
        "setup", help="download adb and the on-device server"
    )
    _add_common_options(setup)
    setup.add_argument("--force", action="store_true", help="re-download the server")
    setup.set_defaults(func=_cmd_setup)

    probe = subparsers.add_parser(
        "probe",
        help="decode from a real device for a few seconds and report",
    )
    _add_common_options(probe)
    _add_video_options(probe)
    probe.add_argument(
        "--duration",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="how long to decode for",
    )
    probe.add_argument(
        "--screenshot",
        type=Path,
        default=None,
        help="render the last decoded frame to this PNG",
    )
    probe.add_argument(
        "--no-check-control",
        action="store_true",
        help="skip the control-socket round trip check",
    )
    probe.set_defaults(func=_cmd_probe)

    selftest = subparsers.add_parser(
        "selftest",
        help="verify demuxing, decoding and rendering without a device",
    )
    selftest.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="where to write temporary clips (default: the cache directory)",
    )
    selftest.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write a rendered PNG of the test pattern here",
    )
    selftest.set_defaults(func=_cmd_selftest)

    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--serial", "-s", help="device serial (needed if several)")
    parser.add_argument("--adb", help="path to an adb binary or platform-tools dir")
    parser.add_argument("--cache-dir", type=Path, help="override the cache location")


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    _add_common_options(parser)
    _add_video_options(parser)
    _add_window_options(parser)


def _add_video_options(parser: argparse.ArgumentParser) -> None:
    """Options that are forwarded to the on-device server."""
    video = parser.add_argument_group("video (forwarded to the device)")
    video.add_argument(
        "--max-size",
        "-m",
        type=int,
        default=0,
        metavar="PX",
        help="cap the largest dimension; 0 keeps the native resolution",
    )
    video.add_argument(
        "--max-fps",
        type=float,
        default=0.0,
        metavar="FPS",
        help="cap the frame rate; 0 leaves it uncapped",
    )
    video.add_argument(
        "--bit-rate",
        "-b",
        type=int,
        default=DEFAULT_VIDEO_BIT_RATE,
        metavar="BPS",
        help="target bit rate in bits per second",
    )
    video.add_argument(
        "--codec",
        choices=sorted(VIDEO_CODECS),
        default="h264",
        help="video codec; h264 is the most compatible and the fastest to decode",
    )
    video.add_argument(
        "--codec-option",
        action="append",
        default=[],
        metavar="KEY:VALUE",
        help="extra MediaCodec option, repeatable (for example "
        "profile:1,i-frame-interval:10)",
    )
    video.add_argument(
        "--audio",
        action="store_true",
        help="reserve the audio socket (audio playback is not implemented yet)",
    )

    device = parser.add_argument_group("device behaviour")
    device.add_argument("--show-touches", action="store_true", help="draw touch points")
    device.add_argument(
        "--no-control",
        action="store_true",
        help="do not send input events to the device",
    )
    device.add_argument(
        "--no-stay-awake",
        action="store_true",
        help="let the device screen sleep while mirroring",
    )
    device.add_argument(
        "--no-power-on",
        action="store_true",
        help="do not wake the device when the session starts",
    )
    device.add_argument(
        "--keep-active",
        action="store_true",
        help="keep the device awake without changing global settings",
    )
    device.add_argument(
        "--force-adb-forward",
        action="store_true",
        help="connect out to the device (adb forward) instead of the default "
        "reverse tunnel; only needed where 'adb reverse' is unavailable",
    )
    device.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default="info",
        help="verbosity of the on-device server",
    )

def _add_window_options(parser: argparse.ArgumentParser) -> None:
    """Options that only affect the client-side window."""
    window = parser.add_argument_group("window")
    window.add_argument("--fullscreen", "-f", action="store_true", help="start fullscreen")
    window.add_argument(
        "--always-on-top", action="store_true", help="keep the window above others"
    )
    window.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="extra zoom on top of the fit-to-window size (try 2.0)",
    )
    window.add_argument(
        "--integer-scale",
        action="store_true",
        help="prefer whole-number scaling to keep pixels crisp",
    )
    window.add_argument(
        "--nearest",
        action="store_true",
        help="use nearest-neighbour filtering instead of linear",
    )
    window.add_argument(
        "--no-vsync",
        action="store_true",
        help="do not wait for the display refresh (lower latency, may tear)",
    )
    window.add_argument(
        "--no-stats", action="store_true", help="hide the live statistics"
    )
    window.add_argument(
        "--color-matrix",
        choices=["auto", "bt601", "bt709"],
        default="auto",
        help="override the YUV colour matrix",
    )
    window.add_argument(
        "--color-range",
        choices=["auto", "limited", "full"],
        default="auto",
        help="override the YUV colour range",
    )
    window.add_argument(
        "--scroll-scale",
        type=float,
        default=1.0,
        help="scroll amount per wheel notch; use a negative value to flip it",
    )


def _config_from_args(args: argparse.Namespace) -> SessionConfig:
    codec_options: list[tuple[str, str]] = []
    for item in getattr(args, "codec_option", []) or []:
        if ":" not in item:
            raise MirrorScreenError(
                f"invalid --codec-option {item!r}; expected KEY:VALUE"
            )
        key, _, value = item.partition(":")
        codec_options.append((key.strip(), value.strip()))

    return SessionConfig(
        serial=args.serial,
        adb_path=args.adb,
        cache_dir=args.cache_dir,
        max_size=args.max_size,
        max_fps=args.max_fps,
        video_bit_rate=args.bit_rate,
        video_codec=args.codec,
        video_codec_options=codec_options,
        audio=getattr(args, "audio", False),
        control=not getattr(args, "no_control", False),
        show_touches=getattr(args, "show_touches", False),
        stay_awake=not getattr(args, "no_stay_awake", False),
        power_on=not getattr(args, "no_power_on", False),
        keep_active=getattr(args, "keep_active", False),
        log_level=getattr(args, "log_level", "info"),
        force_adb_forward=getattr(args, "force_adb_forward", False),
        fullscreen=getattr(args, "fullscreen", False),
        always_on_top=getattr(args, "always_on_top", False),
        scale=getattr(args, "scale", 1.0),
        integer_scale=getattr(args, "integer_scale", False),
        filter_mode="nearest" if getattr(args, "nearest", False) else "linear",
        vsync=not getattr(args, "no_vsync", False),
        show_stats=not getattr(args, "no_stats", False),
        color_matrix=getattr(args, "color_matrix", "auto"),
        color_range=getattr(args, "color_range", "auto"),
        scroll_scale=getattr(args, "scroll_scale", 1.0),
    )


def _cmd_run(args: argparse.Namespace) -> int:
    from .app import run

    config = _config_from_args(args)
    config.validate()
    return run(config, progress=lambda message: print(message, file=sys.stderr))


def _cmd_devices(args: argparse.Namespace) -> int:
    adb_path = ensure_adb(args.adb, progress=lambda message: print(message, file=sys.stderr))
    adb = Adb(adb_path)
    devices = adb.devices()

    if not devices:
        print("No devices found.")
        print(
            "Connect a phone over USB, enable Developer options > USB debugging, "
            "then accept the prompt on the phone."
        )
        return 1

    for device in devices:
        state = "ready" if device.is_usable else device.state
        print(f"{device.serial}\t{state}\t{device.display_name}")
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    say = lambda message: print(message, file=sys.stderr)  # noqa: E731

    adb_path = ensure_adb(args.adb, progress=say)
    print(f"adb:          {adb_path}")

    cache = args.cache_dir or cache_root()
    jar = ensure_server_jar(cache, progress=say, refresh=args.force)
    print(f"server jar:   {jar}")
    print(f"server ver.:  {SCRCPY_VERSION}")
    print(f"cache dir:    {cache}")
    print()
    print("Setup complete. Run 'mirror-screen' to start mirroring.")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    from .probe import run_probe

    config = _config_from_args(args)
    config.validate()

    result = run_probe(
        config,
        duration=args.duration,
        screenshot=args.screenshot,
        check_control=not args.no_check_control,
        progress=lambda message: print(message, file=sys.stderr),
    )
    print()
    print(result.render())
    return 0 if result.ok else 1


def _cmd_selftest(args: argparse.Namespace) -> int:
    from .selfcheck import run_self_check

    work_dir = args.work_dir or (cache_root() / "selftest")
    report = run_self_check(
        work_dir,
        output_image=args.output,
        progress=lambda message: print(message, file=sys.stderr),
    )
    print(report.render())
    if report.image_path is not None:
        print(f"\nRendered test pattern: {report.image_path}")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Default to "run" when no subcommand is given, but leave the parser-level
    # flags (help, version) alone.
    if not arguments or (
        arguments[0].startswith("-")
        and arguments[0] not in {"-h", "--help", "--version"}
    ):
        arguments.insert(0, "run")

    parser = build_parser()
    args = parser.parse_args(arguments)

    level = logging.DEBUG if "debug" in arguments or "-v" in arguments else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        return int(args.func(args))
    except MirrorScreenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
