"""Launch the Windows bundle directly into the main control UI."""

from mirror_screen.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["ui"]))
