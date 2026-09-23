#!/bin/zsh
# Double-click this file in Finder to start Mirror Screen.
#
# Double-clicking opens the pre-mirror settings UI. Explicit subcommands still
# behave like the CLI, and options can be passed to the UI: ./"Mirror Screen.command"
# --max-size 1280 --no-vsync

cd "$(dirname "$0")" || exit 1

if (( $# == 0 )); then
    set -- ui
elif [[ "$1" == -* ]]; then
    set -- ui "$@"
fi

if [ -x .venv/bin/mirror-screen ]; then
    exec .venv/bin/mirror-screen "$@"
fi

if [ -x .venv/bin/python ]; then
    exec .venv/bin/python -m mirror_screen "$@"
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
