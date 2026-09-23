"""Locating (and, if needed, installing) the Android platform tools."""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from ..errors import AdbError

ProgressFn = Callable[[str], None]

_PLATFORM_TOOLS_URLS = {
    ("Darwin", "arm64"): "platform-tools-latest-darwin.zip",
    ("Darwin", "x86_64"): "platform-tools-latest-darwin.zip",
    ("Linux", "x86_64"): "platform-tools-latest-linux.zip",
    ("Linux", "aarch64"): "platform-tools-latest-linux.zip",
    ("Windows", "AMD64"): "platform-tools-latest-windows.zip",
    ("Windows", "ARM64"): "platform-tools-latest-windows.zip",
}

_PLATFORM_TOOLS_BASE = "https://dl.google.com/android/repository/"


def cache_root() -> Path:
    """Return the per-user cache directory for Mirror Screen."""
    override = os.environ.get("MIRROR_SCREEN_CACHE")
    if override:
        return Path(override).expanduser()

    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Caches" / "mirror-screen"
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "mirror-screen"
        return Path.home() / "AppData" / "Local" / "mirror-screen"
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "mirror-screen"


def _adb_names() -> list[str]:
    return ["adb.exe", "adb"] if platform.system() == "Windows" else ["adb"]


def _search_paths() -> list[Path]:
    """Common places adb may already live, in priority order."""
    candidates: list[Path] = []

    env = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if env:
        candidates.append(Path(env) / "platform-tools")

    candidates.append(Path.home() / "Library" / "Android" / "sdk" / "platform-tools")
    candidates.append(Path.home() / "Android" / "Sdk" / "platform-tools")
    candidates.append(Path("/usr/local/bin"))
    candidates.append(Path("/opt/homebrew/bin"))
    candidates.append(cache_root() / "platform-tools")
    return candidates


def find_adb(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the path to a usable ``adb``, or ``None`` if there is none."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_dir():
            for name in _adb_names():
                candidate = path / name
                if candidate.is_file():
                    return candidate
        elif path.is_file():
            return path
        raise AdbError(f"no adb executable at {path}")

    on_path = shutil.which("adb")
    if on_path:
        return Path(on_path)

    for directory in _search_paths():
        for name in _adb_names():
            candidate = directory / name
            if candidate.is_file():
                return candidate

    return None


def ensure_adb(
    explicit: str | os.PathLike[str] | None = None,
    *,
    auto_install: bool = True,
    progress: ProgressFn | None = None,
) -> Path:
    """Return an adb path, downloading the platform tools when necessary.

    Args:
        explicit: An adb path or platform-tools directory supplied by the user.
        auto_install: Download Google's platform-tools into the cache directory
            when no adb can be found.
        progress: Callback for human-readable progress messages.

    Raises:
        AdbError: if adb is missing and cannot be installed.
    """
    found = find_adb(explicit)
    if found is not None:
        return found

    if not auto_install:
        raise AdbError(
            "adb was not found. Install it with 'brew install --cask "
            "android-platform-tools', or run 'mirror-screen setup' to download "
            "Google's platform-tools automatically."
        )

    return install_platform_tools(progress=progress)


def run_adb_version(adb: Path) -> str:
    """Return the version banner of an adb binary."""
    try:
        result = subprocess.run(
            [str(adb), "version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except OSError as exc:
        raise AdbError(f"could not execute adb at {adb}: {exc}") from exc

    first_line = (result.stdout or result.stderr).strip().splitlines()
    return first_line[0] if first_line else "unknown"


def _download(url: str, destination: Path, progress: ProgressFn | None) -> None:
    report = progress or (lambda _message: None)
    report(f"downloading {url}")
    tmp = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
            total = int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            with tmp.open("wb") as handle:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        percent = downloaded * 100 // total
                        report(f"  {percent:3d}% ({downloaded >> 20} MiB)")
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise AdbError(f"failed to download platform-tools: {exc}") from exc

    tmp.replace(destination)


def _unzip_adb(archive: Path, destination: Path, progress: ProgressFn | None) -> Path:
    report = progress or (lambda _message: None)
    report(f"extracting {archive.name}")
    destination.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive) as zf:
        zf.extractall(destination)

    extracted = destination / "platform-tools"
    adb = None
    for name in _adb_names():
        candidate = extracted / name
        if candidate.is_file():
            adb = candidate
            break

    if adb is None:
        raise AdbError("the downloaded archive did not contain adb")

    # Zip files do not carry the executable bit on POSIX systems.
    for entry in extracted.iterdir():
        if entry.is_file():
            entry.chmod(entry.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

    if platform.system() == "Darwin":
        # urllib does not set the quarantine attribute, but strip it anyway so
        # a manually placed copy keeps working.
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(extracted)],
            check=False,
            capture_output=True,
        )

    return adb


def install_platform_tools(
    *,
    destination: Path | None = None,
    progress: ProgressFn | None = None,
    force: bool = False,
) -> Path:
    """Download and unpack Google's platform-tools; return the adb path."""
    report = progress or (lambda message: print(message, file=sys.stderr))

    system = platform.system()
    machine = platform.machine()
    asset = _PLATFORM_TOOLS_URLS.get((system, machine))
    if asset is None:
        supported = ", ".join(f"{s}/{m}" for s, m in _PLATFORM_TOOLS_URLS)
        raise AdbError(
            f"no platform-tools build for {system}/{machine} (supported: {supported})"
        )

    target = destination or (cache_root() / "platform-tools")
    adb = target / "platform-tools" / _adb_names()[0]

    if adb.is_file() and not force:
        report(f"platform-tools already installed at {adb}")
        return adb

    archive = target / asset
    target.mkdir(parents=True, exist_ok=True)

    _download(_PLATFORM_TOOLS_BASE + asset, archive, report)
    adb = _unzip_adb(archive, target, report)
    archive.unlink(missing_ok=True)
    report(f"installed adb at {adb}")
    return adb
