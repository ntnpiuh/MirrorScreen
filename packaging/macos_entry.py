"""Launch the macOS bundle directly into the pre-mirror settings UI."""

from mirror_screen.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["ui"]))