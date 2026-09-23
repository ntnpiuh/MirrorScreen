"""Download the pinned scrcpy-server jar.

The scrcpy client and server versions must match *exactly* (the server refuses
to start otherwise), so the version is pinned here rather than discovered.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from ..errors import ServerError

#: The scrcpy release whose protocol this client implements.
SCRCPY_VERSION = "4.1"

#: Asset name inside the GitHub release.
SERVER_ASSET_NAME = f"scrcpy-server-v{SCRCPY_VERSION}"

#: Where the server jar is pushed on the device.
DEVICE_JAR_PATH = "/data/local/tmp/scrcpy-server.jar"

#: Fully qualified main class of the server.
SERVER_MAIN_CLASS = "com.genymobile.scrcpy.Server"

_RELEASE_URL = (
    "https://github.com/Genymobile/scrcpy/releases/download/"
    f"v{SCRCPY_VERSION}/{SERVER_ASSET_NAME}"
)

#: Sanity limits on the downloaded asset size.
_MIN_JAR_BYTES = 50_000
_MAX_JAR_BYTES = 4_000_000

ProgressFn = Callable[[str], None]


def download_url() -> str:
    """Return the release URL of the pinned server jar."""
    return _RELEASE_URL


def cached_jar_path(cache_root: Path) -> Path:
    """Return the local cache path of the pinned server jar."""
    return cache_root / "scrcpy-server" / SERVER_ASSET_NAME


def ensure_server_jar(
    cache_root: Path,
    *,
    progress: ProgressFn | None = None,
    refresh: bool = False,
) -> Path:
    """Return a local copy of the server jar, downloading it on first use."""
    report = progress or (lambda message: print(message, file=sys.stderr))

    jar = cached_jar_path(cache_root)
    if jar.is_file() and not refresh and _looks_valid(jar):
        return jar

    report(f"downloading scrcpy server v{SCRCPY_VERSION}")
    report(f"  {_RELEASE_URL}")

    jar.parent.mkdir(parents=True, exist_ok=True)
    tmp = jar.with_suffix(".part")
    try:
        with urllib.request.urlopen(_RELEASE_URL, timeout=120) as response:  # noqa: S310
            payload = response.read()
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise ServerError(
            f"could not download the scrcpy server from {_RELEASE_URL}: {exc}"
        ) from exc

    if not _MIN_JAR_BYTES <= len(payload) <= _MAX_JAR_BYTES:
        raise ServerError(
            f"downloaded server jar has an unexpected size ({len(payload)} bytes); "
            "the release URL may have changed"
        )

    tmp.write_bytes(payload)
    tmp.replace(jar)
    report(f"  cached at {jar}")
    return jar


def _looks_valid(jar: Path) -> bool:
    try:
        size = jar.stat().st_size
    except OSError:
        return False
    return _MIN_JAR_BYTES <= size <= _MAX_JAR_BYTES
