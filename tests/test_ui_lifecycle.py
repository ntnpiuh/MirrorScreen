"""Headless Qt checks for the stream window lifecycle boundary.

Runs the actual check in a fresh subprocess. Constructing a QWidget touches
the platform paint engine, and on the offscreen QPA platform that can abort
the *entire* process - not just fail one test - if an earlier test already
left Qt's GL/offscreen state broken (this happens in practice: an offscreen
GL context failure, such as the one test_pipeline_e2e.py tolerates on
GL-less machines, can wedge the platform plugin for the rest of the run).
Isolating the widget-touching check in its own interpreter keeps that
failure mode from taking the whole suite down with it.
"""

from __future__ import annotations

import subprocess
import sys

_CHECK = """
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from mirror_screen.config import SessionConfig
from mirror_screen.ui.window import MirrorWindow

app = QApplication.instance() or QApplication([])
closed = []
widget = QWidget()

# Test 1: Stream window close notifies session owner
window = MirrorWindow(
    widget,
    SessionConfig(),
    device_name="test-device",
    commands=None,
    on_close=lambda: closed.append(True),
)
window.close()
app.processEvents()
assert closed == [True], closed
print("OK - Stream window close notified owner")
"""


def test_stream_window_close_notifies_session_owner():
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"widget lifecycle check exited with {result.returncode}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK - Stream window close notified owner" in result.stdout
