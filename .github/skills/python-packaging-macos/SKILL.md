---
name: python-packaging-macos
description: "Use when configuring pyproject.toml, uv or Python dependencies, PyInstaller, Briefcase, macOS .app bundles, DMG or ZIP distribution, bundled adb binaries, scrcpy-server.jar, signing, or release verification."
---
# Python and macOS Packaging

Use this skill for reproducible application builds and distribution.

## Repository fit
- Inspect `pyproject.toml`, `packaging/`, `build/`, and the existing macOS entrypoint before changing packaging.
- Keep source imports and runtime resource lookup valid both from an editable install and from a frozen `.app` bundle.
- Treat `adb` and the pinned scrcpy server JAR as explicit runtime assets. Verify architecture, permissions, cache location, version, and SHA-256 before use.
- Do not commit generated `build/`, `dist/`, caches, or secrets. Keep build metadata changes separate from application logic changes.
- Prefer the existing Hatch/PyInstaller flow. Introduce `uv`, Briefcase, or another backend only with a clear migration plan and equivalent CI coverage.

## macOS release rules
- Test the packaged app outside the source checkout.
- Verify the bundle launches without an activated virtual environment and can locate resources with spaces in paths.
- Preserve arm64/x86_64 assumptions and make unsupported architectures fail clearly.
- Document signing, notarization, Gatekeeper, permissions, and first-run network/cache behavior rather than silently bypassing them.
- Generate checksums for distributable archives and validate the archive contents before release.

## Verification
- Run `pytest` and the project self-check before building.
- Build the app with `packaging/build_macos_app.sh`, inspect the bundle, launch it, and verify the resulting archive checksum when macOS build prerequisites are available.
- Report unavailable signing, notarization, device, or display prerequisites explicitly.

## References
- Project packaging: `packaging/`, `pyproject.toml`, and `README.md`.
- Python skills reference: https://github.com/Paldom/python-skills
