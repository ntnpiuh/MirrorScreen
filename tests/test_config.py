"""Configuration and CLI parsing tests."""

from __future__ import annotations

import pytest
from typing import Literal, cast

from mirror_screen.cli import _config_from_args, build_parser, main
from mirror_screen.config import SessionConfig


def test_defaults_are_valid():
    config = SessionConfig()
    config.validate()
    assert config.phone_playback == "keep"
    assert config.audio_output is None


@pytest.mark.parametrize("policy", ["keep", "mute"])
def test_phone_playback_policy_is_valid(policy):
    config = SessionConfig(audio=True, phone_playback=policy)
    config.validate()


def test_invalid_phone_playback_policy_is_rejected():
    invalid_policy = cast(Literal["keep", "mute"], "pause")
    config = SessionConfig(phone_playback=invalid_policy)
    with pytest.raises(ValueError, match="phone playback policy"):
        config.validate()


def test_blank_audio_output_is_rejected():
    config = SessionConfig(audio_output="  ")
    with pytest.raises(ValueError, match="audio_output"):
        config.validate()


def test_invalid_codec_is_rejected():
    config = SessionConfig(video_codec="mpeg2")
    with pytest.raises(ValueError, match="unsupported video codec"):
        config.validate()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_size", -1, "max_size"),
        ("max_fps", -1.0, "max_fps"),
        ("video_bit_rate", 0, "video_bit_rate"),
        ("scale", 0.0, "scale"),
        ("scroll_scale", 0.0, "scroll_scale"),
        ("log_level", "loud", "log level"),
        ("filter_mode", "cubic", "filter mode"),
        ("color_matrix", "bt2020", "color matrix"),
        ("color_range", "narrow", "color range"),
    ],
)
def test_invalid_values_are_rejected(field, value, message):
    config = SessionConfig(**{field: value})
    with pytest.raises(ValueError, match=message):
        config.validate()


def test_codec_options_are_serialized_for_the_server():
    config = SessionConfig(
        video_codec_options=[("profile", "1"), ("i-frame-interval", "10")]
    )
    config.validate()
    assert config.codec_options_string == "profile:1,i-frame-interval:10"


def test_codec_option_names_are_validated():
    config = SessionConfig(video_codec_options=[("bad name", "1")])
    with pytest.raises(ValueError, match="invalid codec option name"):
        config.validate()

    config = SessionConfig(video_codec_options=[("profile", "a,b")])
    with pytest.raises(ValueError, match="invalid codec option value"):
        config.validate()


def test_cli_defaults_to_the_run_command():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.func is not None
    assert args.codec == "h264"
    assert args.max_size == 0


def test_ui_command_is_registered_and_defaults_are_valid():
    parser = build_parser()
    args = parser.parse_args(["ui"])
    assert args.func is not None
    config = _config_from_args(args)
    config.validate()
    assert config.vsync is True
    assert config.render_thread is False
    assert config.link_quality in {"fast", "balanced", "weak", "very_weak"}


def test_cli_translates_flags_into_configuration():
    from mirror_screen.cli import _config_from_args

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--max-size",
            "1920",
            "--bit-rate",
            "30000000",
            "--codec",
            "h265",
            "--no-vsync",
            "--integer-scale",
            "--scale",
            "2",
            "--codec-option",
            "profile:1",
            "--serial",
            "ABC123",
        ]
    )
    config = _config_from_args(args)
    config.validate()

    assert config.max_size == 1920
    assert config.video_bit_rate == 30_000_000
    assert config.video_codec == "h265"
    assert config.vsync is False
    assert config.integer_scale is True
    assert config.scale == 2.0
    assert config.video_codec_options == [("profile", "1")]
    assert config.serial == "ABC123"


def test_cli_translates_audio_policy_and_output():
    parser = build_parser()
    args = parser.parse_args(
        ["run", "--audio", "--mute-phone-audio", "--audio-output", "headphones"]
    )
    config = _config_from_args(args)
    config.validate()
    assert config.audio is True
    assert config.phone_playback == "mute"
    assert config.audio_output == "headphones"


def test_link_quality_maps_to_bandwidth_and_resolution():
    config = SessionConfig(link_quality="weak")
    config.apply_link_quality()
    assert config.max_size == 1280
    assert config.video_bit_rate == 8_000_000

    config = SessionConfig(link_quality="fast")
    config.apply_link_quality()
    assert config.max_size == 0
    assert config.video_bit_rate == 40_000_000


def test_cli_rejects_a_malformed_codec_option():
    from mirror_screen.cli import _config_from_args
    from mirror_screen.errors import MirrorScreenError

    parser = build_parser()
    args = parser.parse_args(["run", "--codec-option", "profile"])
    with pytest.raises(MirrorScreenError, match="KEY:VALUE"):
        _config_from_args(args)


def test_cli_main_maps_errors_to_exit_codes(capsys, monkeypatch):
    """A missing device must be reported, not raise a traceback."""
    from mirror_screen.errors import DeviceError

    def _boom(_config, progress=None):
        raise DeviceError("no Android device found")

    monkeypatch.setattr("mirror_screen.app.run", _boom)
    assert main([]) == 2
    assert "no Android device found" in capsys.readouterr().err


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "mirror-screen" in capsys.readouterr().out


def test_subcommands_are_registered():
    parser = build_parser()
    for command in ("run", "devices", "setup", "probe", "selftest"):
        args = parser.parse_args([command])
        assert callable(args.func)

    # The default (no subcommand) is handled in main(), not the parser.
    args = parser.parse_args(["run", "--fullscreen"])
    assert args.fullscreen is True
