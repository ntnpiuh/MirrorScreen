#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
    print -u2 "Python environment not found: $PYTHON"
    print -u2 "Create it and install the project dependencies first."
    exit 1
fi

if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
    print -u2 "PyInstaller is not installed in $PYTHON"
    print -u2 "Install it with: $PYTHON -m pip install pyinstaller"
    exit 1
fi

rm -rf "$ROOT_DIR/build" "$ROOT_DIR/dist"

"$PYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name "Mirror Screen" \
    --osx-bundle-identifier com.ntnpiuh.mirrorscreen \
    --paths "$ROOT_DIR/src" \
    --distpath "$ROOT_DIR/dist" \
    --workpath "$ROOT_DIR/build" \
    --specpath "$ROOT_DIR/build" \
    "$ROOT_DIR/packaging/macos_entry.py"

ARCH="$(uname -m)"
ZIP_PATH="$ROOT_DIR/dist/Mirror-Screen-macos-$ARCH.zip"
CHECKSUM_PATH="$ROOT_DIR/dist/SHA256SUMS.txt"
rm -f "$ZIP_PATH"
ditto -c -k --sequesterRsrc --keepParent \
    "$ROOT_DIR/dist/Mirror Screen.app" "$ZIP_PATH"
shasum -a 256 "$ZIP_PATH" > "$CHECKSUM_PATH"

print "Created: $ROOT_DIR/dist/Mirror Screen.app"
print "Created: $ZIP_PATH"
print "Created: $CHECKSUM_PATH"