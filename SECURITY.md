# Security Policy

## Supported versions

Only the latest version on the `main` branch is supported.

## Reporting a vulnerability

Please report security issues privately through [GitHub Security Advisories](https://github.com/ntnpiuh/MirrorScreen/security/advisories/new).
Do not open a public issue for an undisclosed vulnerability.

Include the affected version, operating system, reproduction steps, and any
relevant logs with secrets or personal data removed. We will acknowledge a
report within 7 days and coordinate a fix and disclosure timeline with the
reporter.

## Trust and permissions

Mirror Screen uses ADB to connect to an Android device. A connected and
authorized device allows the application to send input, clipboard data, and
screen-control commands. The application downloads Google's platform-tools and
the pinned scrcpy server over HTTPS. The scrcpy server is verified against its
published SHA-256 digest before use.

The macOS release is currently not notarized by Apple. Users should verify the
release checksum and may see a Gatekeeper warning when opening it.