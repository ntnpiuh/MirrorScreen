---
name: python-quality
description: "Use when improving Python linting, pytest coverage, typing, pyright or mypy configuration, CI quality gates, dependency hygiene, or maintainability in Mirror Screen."
---
# Python Quality

Use this skill for deterministic quality improvements in the Python codebase.

## Repository fit
- Read `pyproject.toml` and existing tests before adding tools or changing configuration.
- Preserve the `src/` layout, Python 3.11 minimum, pytest configuration, and existing public APIs.
- Prefer focused tests for behavior and typed protocols/dataclasses over broad snapshot-style tests.
- Use Ruff, pytest, pyright, or mypy only when the repository has adopted the tool or the task explicitly requests it; do not add a dependency merely to silence one warning.

## Type and test rules
- Keep annotations precise around sockets, subprocesses, Qt callbacks, decoder frames, and unions. Avoid `Any` and unchecked casts at concurrency boundaries.
- Test error messages and exit behavior where users depend on diagnostics.
- Cover malformed protocol input, cancellation, reconnect, resource cleanup, and platform fallbacks.
- Keep tests deterministic: fake clocks, byte sources, subprocesses, and devices instead of sleeps or real USB devices.
- Run the narrowest test selection after each edit, then the full suite for shared changes.

## CI and dependencies
- Keep CI fast and reproducible. Pin only where the project policy requires it and avoid unnecessary dependency churn.
- Do not weaken lint, type, or test gates to make a change pass; fix the underlying issue or document an intentional boundary.

## References
- Project configuration: `pyproject.toml` and `tests/`.
- External Python skills reference: https://github.com/Paldom/python-skills
