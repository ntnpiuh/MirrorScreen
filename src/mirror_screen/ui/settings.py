"""Pre-mirror tuning UI for latency-focused configuration."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from ..config import LINK_QUALITY_PRESETS, SessionConfig


class PreMirrorSettingsDialog(QDialog):
    """A minimal pre-flight configuration panel for latency tuning."""

    def __init__(self, config: SessionConfig | None = None, parent=None) -> None:
        super().__init__(parent)
        self.config = config or SessionConfig()
        self._updating = False

        self.setWindowTitle("Mirror Screen • Latency preset")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(420, 260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("Tuning before mirroring")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title)

        hint = QLabel(
            "Use these settings before the mirror session starts. They help trade "
            "quality for lower delay depending on your machine, cable, and USB link."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #b3b3b3;")
        layout.addWidget(hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.link_quality = QComboBox()
        self.link_quality.addItems(LINK_QUALITY_PRESETS)
        self.link_quality.setCurrentText(self.config.link_quality or "balanced")
        form.addRow("USB / link quality", self.link_quality)

        self.vsync = QCheckBox("Enable vsync")
        self.vsync.setChecked(self.config.vsync)
        form.addRow("Display sync", self.vsync)

        self.render_thread = QCheckBox("Use render thread")
        self.render_thread.setChecked(self.config.render_thread)
        form.addRow("Rendering", self.render_thread)

        self.max_size = QSpinBox()
        self.max_size.setRange(0, 4096)
        self.max_size.setSingleStep(64)
        self.max_size.setSpecialValueText("native")
        self.max_size.setValue(self.config.max_size or 0)
        form.addRow("Max stream size", self.max_size)

        self.max_fps = QDoubleSpinBox()
        self.max_fps.setRange(0.0, 120.0)
        self.max_fps.setSingleStep(5.0)
        self.max_fps.setValue(float(self.config.max_fps or 60.0))
        form.addRow("Max FPS", self.max_fps)

        self.bit_rate = QSpinBox()
        self.bit_rate.setRange(1_000_000, 100_000_000)
        self.bit_rate.setSingleStep(1_000_000)
        self.bit_rate.setValue(int(self.config.video_bit_rate))
        form.addRow("Bit rate", self.bit_rate)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.link_quality.currentTextChanged.connect(self._apply_preset)
        self._apply_preset(self.link_quality.currentText())

    def _apply_preset(self, quality: str) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            preset = LINK_QUALITY_PRESETS.get(quality, LINK_QUALITY_PRESETS["balanced"])
            self.max_size.setValue(int(preset["max_size"]))
            self.max_fps.setValue(float(preset["max_fps"]))
            self.bit_rate.setValue(int(preset["video_bit_rate"]))
        finally:
            self._updating = False

    def build_config(self) -> SessionConfig:
        config = SessionConfig(
            serial=self.config.serial,
            adb_path=self.config.adb_path,
            cache_dir=self.config.cache_dir,
            max_size=int(self.max_size.value()),
            max_fps=float(self.max_fps.value()),
            video_bit_rate=int(self.bit_rate.value()),
            video_codec=self.config.video_codec,
            video_codec_options=list(self.config.video_codec_options),
            audio=self.config.audio,
            control=self.config.control,
            show_touches=self.config.show_touches,
            stay_awake=self.config.stay_awake,
            power_on=self.config.power_on,
            keep_active=self.config.keep_active,
            clipboard_autosync=self.config.clipboard_autosync,
            log_level=self.config.log_level,
            fullscreen=self.config.fullscreen,
            always_on_top=self.config.always_on_top,
            scale=self.config.scale,
            integer_scale=self.config.integer_scale,
            filter_mode=self.config.filter_mode,
            color_matrix=self.config.color_matrix,
            color_range=self.config.color_range,
            scroll_scale=self.config.scroll_scale,
            show_stats=self.config.show_stats,
            auto_reconnect=self.config.auto_reconnect,
            trace=self.config.trace,
            auto_resize_window=self.config.auto_resize_window,
            render_thread=self.render_thread.isChecked(),
            link_quality=self.link_quality.currentText(),
            vsync=self.vsync.isChecked(),
        )
        config.validate()
        return config


def run_pre_mirror_settings(config: SessionConfig | None = None) -> SessionConfig | None:
    """Return the selected config, or ``None`` when the dialog is cancelled."""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    dialog = PreMirrorSettingsDialog(config)
    result = dialog.exec()
    if result != QDialog.DialogCode.Accepted:
        return None
    return dialog.build_config()
