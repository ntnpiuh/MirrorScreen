#!/bin/zsh
# Double-click this file in Finder to test the source checkout in Mirror Screen.
# Run the macOS packaging script only after this launcher passes the real-device
# checks; this file intentionally does not start a build.
#
# Double-clicking opens the pre-mirror settings UI. Explicit subcommands still
# behave like the CLI, and options can be passed to the UI: ./"Mirror Screen.command"
# --max-size 1280 --no-vsync

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR" || exit 1

# Keep this launcher usable before an editable install, and make sure manual
# testing uses the current source rather than stale frozen output.
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

if (( $# == 0 )); then
    set -- ui
elif [[ "$1" == -* ]]; then
    set -- ui "$@"
fi

if [ -x "$ROOT_DIR/.venv/bin/mirror-screen" ]; then
    exec "$ROOT_DIR/.venv/bin/mirror-screen" "$@"
fi

if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    exec "$ROOT_DIR/.venv/bin/python" -m mirror_screen "$@"
fi

echo "Mirror Screen has no Python environment yet."
echo
echo "Set it up once with:"
echo "    python3 -m venv .venv"
echo "    .venv/bin/pip install -e ."
echo
echo "Then double-click this file again."
echo
read -k 1 "?Press any key to close..."
