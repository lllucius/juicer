"""Juicer PySide6 GUI — complete graphical interface for the UPS controller.

Panels:
  - Overview: serial settings, connection status, bank states
  - Manual Controls: all-on, all-off, per-bank switches
  - Boot Sequence Editor: per-bank action/delay config
  - Shutdown Sequence Editor: same for shutdown
  - Service Panel: install/uninstall/start/stop with status
  - Import/Export: TOML config import/export
  - Log Viewer: scrolling log messages

All controls have ``setAccessibleName()`` and proper labels.
Keyboard-navigable with correct tab order.
Uses protocol/config/service APIs — never raw serial strings.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import sys
from functools import partial
from typing import Any, Callable, Optional, cast

from juicer.config import (
    BankAction,
    BankConfig,
    ConfigStore,
    GlobalConfig,
    SequenceConfig,
    TomlStore,
)

logger = logging.getLogger(__name__)

# Guard PySide6 import for environments where it's not installed
try:
    from PySide6.QtCore import QObject, QThread, Signal, Slot
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QFileDialog,
        QFormLayout,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSpinBox,
        QStatusBar,
        QTabWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    _PYSIDE6_AVAILABLE = True
except ImportError:
    _PYSIDE6_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────────
# Logging handler that emits to GUI
# ──────────────────────────────────────────────────────────────────────

if _PYSIDE6_AVAILABLE:

    class _QtLogSignal(QObject):
        """Bridge: logging → Qt signal."""

        message = Signal(str)

    class QtLogHandler(logging.Handler):
        """Logging handler that emits records to a Qt signal."""

        def __init__(self) -> None:
            super().__init__()
            self._signal = _QtLogSignal()

        @property
        def message_signal(self) -> Signal:
            return self._signal.message

        def emit(self, record: logging.LogRecord) -> None:
            msg = self.format(record)
            self._signal.message.emit(msg)

    # ──────────────────────────────────────────────────────────────────
    # Worker for background serial operations
    # ──────────────────────────────────────────────────────────────────

    class _SerialWorker(QObject):
        """Run serial operations off the main thread."""

        finished = Signal(object)  # result or exception
        error = Signal(str)

        def __init__(self, func: Any, *args: Any, **kwargs: Any) -> None:
            super().__init__()
            self._func = func
            self._args = args
            self._kwargs = kwargs

        @Slot()
        def run(self) -> None:
            try:
                result = self._func(*self._args, **self._kwargs)
                self.finished.emit(result)
            except Exception as exc:
                self.error.emit(str(exc))

    # ──────────────────────────────────────────────────────────────────
    # Panels
    # ──────────────────────────────────────────────────────────────────

    class OverviewPanel(QWidget):
        """Connection status and bank states at a glance."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)

            # Connection status
            conn_group = QGroupBox("Connection")
            conn_group.setAccessibleName("Connection Status Group")
            conn_layout = QFormLayout(conn_group)

            self.lbl_port = QLabel("Not connected")
            self.lbl_port.setAccessibleName("Connected Port")
            conn_layout.addRow("Port:", self.lbl_port)

            self.lbl_status = QLabel("Disconnected")
            self.lbl_status.setAccessibleName("Connection Status")
            conn_layout.addRow("Status:", self.lbl_status)

            layout.addWidget(conn_group)

            # Bank states
            banks_group = QGroupBox("Outlet Banks")
            banks_group.setAccessibleName("Outlet Banks Group")
            banks_layout = QGridLayout(banks_group)

            self.bank_labels: dict[int, QLabel] = {}
            for i in range(1, 5):
                lbl_name = QLabel(f"Bank {i}:")
                lbl_state = QLabel("Unknown")
                lbl_state.setAccessibleName(f"Bank {i} State")
                self.bank_labels[i] = lbl_state
                banks_layout.addWidget(lbl_name, i - 1, 0)
                banks_layout.addWidget(lbl_state, i - 1, 1)

            layout.addWidget(banks_group)

            # Power / battery summary
            power_group = QGroupBox("Power")
            power_group.setAccessibleName("Power Status Group")
            power_layout = QFormLayout(power_group)

            self.lbl_power = QLabel("Unknown")
            self.lbl_power.setAccessibleName("Power Status")
            power_layout.addRow("Mains:", self.lbl_power)

            self.lbl_battery = QLabel("Unknown")
            self.lbl_battery.setAccessibleName("Battery Level")
            power_layout.addRow("Battery:", self.lbl_battery)

            layout.addWidget(power_group)
            layout.addStretch()

        def set_connected(self, port: str) -> None:
            self.lbl_port.setText(port)
            self.lbl_status.setText("Connected")
            self.lbl_status.setStyleSheet("color: green; font-weight: bold;")

        def set_disconnected(self) -> None:
            self.lbl_port.setText("Not connected")
            self.lbl_status.setText("Disconnected")
            self.lbl_status.setStyleSheet("color: red;")

        def set_bank_state(self, bank: int, state: str) -> None:
            if bank in self.bank_labels:
                self.bank_labels[bank].setText(state)

        def set_power_status(self, status: str) -> None:
            self.lbl_power.setText(status)

        def set_battery_level(self, level: int) -> None:
            self.lbl_battery.setText(f"{level}%")

    class SerialSettingsPanel(QWidget):
        """Port selection and connect/disconnect controls."""

        connect_requested = Signal(str)
        disconnect_requested = Signal()

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)

            form_group = QGroupBox("Serial Port Configuration")
            form_group.setAccessibleName("Serial Port Configuration")
            form = QFormLayout(form_group)

            # Port selection
            port_row = QHBoxLayout()
            self.combo_port = QComboBox()
            self.combo_port.setAccessibleName("Serial Port Selection")
            self.combo_port.setEditable(True)
            self.combo_port.setMinimumWidth(150)
            port_row.addWidget(self.combo_port)

            self.btn_refresh = QPushButton("Refresh")
            self.btn_refresh.setAccessibleName("Refresh Ports")
            self.btn_refresh.clicked.connect(self._refresh_ports)
            port_row.addWidget(self.btn_refresh)

            form.addRow("Port:", port_row)

            # Baud rate display (read-only, always 9600)
            self.lbl_baud = QLabel("9600-8-N-1 (fixed)")
            self.lbl_baud.setAccessibleName("Baud Rate Display")
            form.addRow("Settings:", self.lbl_baud)

            layout.addWidget(form_group)

            # Connect / Disconnect buttons
            btn_row = QHBoxLayout()
            self.btn_connect = QPushButton("Connect")
            self.btn_connect.setAccessibleName("Connect to Serial Port")
            self.btn_connect.clicked.connect(self._on_connect)
            btn_row.addWidget(self.btn_connect)

            self.btn_disconnect = QPushButton("Disconnect")
            self.btn_disconnect.setAccessibleName("Disconnect from Serial Port")
            self.btn_disconnect.setEnabled(False)
            self.btn_disconnect.clicked.connect(self._on_disconnect)
            btn_row.addWidget(self.btn_disconnect)

            layout.addLayout(btn_row)

            self._refresh_ports()

        def _refresh_ports(self) -> None:
            current_port = self.current_port()
            self.combo_port.clear()
            try:
                from serial.tools.list_ports import comports

                for port_info in comports():
                    self.combo_port.addItem(
                        f"{port_info.device} — {port_info.description}", port_info.device
                    )
            except ImportError:
                self.combo_port.addItem("COM3", "COM3")
            if current_port and not self.set_current_port(current_port, add_if_missing=False):
                logger.info("Previously selected port is no longer available: %s", current_port)

        def _on_connect(self) -> None:
            port = self.current_port()
            if port:
                self.connect_requested.emit(port)
            else:
                logger.warning("Connect requested without a selected serial port")

        def _on_disconnect(self) -> None:
            self.disconnect_requested.emit()

        def current_port(self) -> str:
            text_port = str(self.combo_port.currentText()).split(" —")[0].strip()
            # Editable combo boxes can have typed text even when no list items exist.
            if self.combo_port.count() == 0:
                return text_port
            idx = self.combo_port.currentIndex()
            if idx < 0:
                return text_port
            port = self.combo_port.itemData(idx) or text_port
            return str(port).strip()

        def available_ports(self) -> set[str]:
            ports: set[str] = set()
            for i in range(self.combo_port.count()):
                data = self.combo_port.itemData(i)
                if data:
                    ports.add(str(data))
            return ports

        def set_current_port(self, port: str, *, add_if_missing: bool = True) -> bool:
            port = port.strip()
            if not port:
                logger.debug("Ignoring empty serial port selection")
                return False
            for i in range(self.combo_port.count()):
                if self.combo_port.itemData(i) == port:
                    self.combo_port.setCurrentIndex(i)
                    return True
            if add_if_missing:
                self.combo_port.setEditText(port)
                return True
            return False

        def set_connected(self, connected: bool) -> None:
            self.btn_connect.setEnabled(not connected)
            self.btn_disconnect.setEnabled(connected)
            self.combo_port.setEnabled(not connected)
            self.btn_refresh.setEnabled(not connected)



    class ManualControlsPanel(QWidget):
        """All-on, all-off, and per-bank switches."""

        command_requested = Signal(str, object)  # command_name, args

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)

            # Global controls
            global_group = QGroupBox("Global Controls")
            global_group.setAccessibleName("Global Outlet Controls")
            global_layout = QHBoxLayout(global_group)

            self.btn_all_on = QPushButton("All ON")
            self.btn_all_on.setAccessibleName("Turn All Banks On")
            self.btn_all_on.clicked.connect(lambda: self.command_requested.emit("all_on", None))
            global_layout.addWidget(self.btn_all_on)

            self.btn_all_off = QPushButton("All OFF")
            self.btn_all_off.setAccessibleName("Turn All Banks Off")
            self.btn_all_off.clicked.connect(lambda: self.command_requested.emit("all_off", None))
            global_layout.addWidget(self.btn_all_off)

            layout.addWidget(global_group)

            # Per-bank controls
            banks_group = QGroupBox("Individual Bank Controls")
            banks_group.setAccessibleName("Individual Bank Controls")
            banks_layout = QGridLayout(banks_group)

            self.bank_buttons: dict[int, dict[str, QPushButton]] = {}
            for i in range(1, 5):
                lbl = QLabel(f"Bank {i}:")
                banks_layout.addWidget(lbl, i - 1, 0)

                btn_on = QPushButton("ON")
                btn_on.setAccessibleName(f"Turn Bank {i} On")
                btn_on.clicked.connect(partial(self._switch_bank, i, "ON"))
                banks_layout.addWidget(btn_on, i - 1, 1)

                btn_off = QPushButton("OFF")
                btn_off.setAccessibleName(f"Turn Bank {i} Off")
                btn_off.clicked.connect(partial(self._switch_bank, i, "OFF"))
                banks_layout.addWidget(btn_off, i - 1, 2)

                self.bank_buttons[i] = {"on": btn_on, "off": btn_off}

            layout.addWidget(banks_group)
            layout.addStretch()

        def _switch_bank(self, bank: int, state: str) -> None:
            self.command_requested.emit("switch", (bank, state))

        def set_enabled(self, enabled: bool) -> None:
            self.btn_all_on.setEnabled(enabled)
            self.btn_all_off.setEnabled(enabled)
            for btns in self.bank_buttons.values():
                btns["on"].setEnabled(enabled)
                btns["off"].setEnabled(enabled)

    class _SequenceEditorPanel(QWidget):
        """Shared editor for boot or shutdown sequence configuration."""

        def __init__(
            self, label: str, sound_label: str, parent: Optional[QWidget] = None
        ) -> None:
            super().__init__(parent)
            self._label = label
            layout = QVBoxLayout(self)

            # Sound file section
            sound_group = QGroupBox(f"{sound_label}")
            sound_group.setAccessibleName(f"{sound_label} Configuration")
            sound_form = QFormLayout(sound_group)
            self.edit_sound = QLineEdit()
            self.edit_sound.setAccessibleName(f"{sound_label} Path")
            sound_browse_row = self._path_row(self.edit_sound, f"Browse {sound_label}")
            sound_form.addRow(f"{sound_label}:", sound_browse_row)
            layout.addWidget(sound_group)

            self.bank_widgets: dict[int, dict[str, Any]] = {}

            for i in range(1, 5):
                group = QGroupBox(f"Bank {i}")
                group.setAccessibleName(f"{label} Bank {i} Configuration")
                row_layout = QHBoxLayout(group)

                # Action combo
                row_layout.addWidget(QLabel("Action:"))
                combo = QComboBox()
                combo.setAccessibleName(f"{label} Bank {i} Action")
                combo.addItem("No Action", None)
                combo.addItem("Turn ON", 1)
                combo.addItem("Turn OFF", 0)
                row_layout.addWidget(combo)

                # Pre-delay
                row_layout.addWidget(QLabel("Delay Before:"))
                pre_spin = QSpinBox()
                pre_spin.setAccessibleName(f"{label} Bank {i} Pre-Delay")
                pre_spin.setRange(0, 2_147_483_647)
                pre_spin.setSuffix(" ms")
                pre_spin.setSingleStep(100)
                row_layout.addWidget(pre_spin)

                # Post-delay
                row_layout.addWidget(QLabel("Delay After:"))
                post_spin = QSpinBox()
                post_spin.setAccessibleName(f"{label} Bank {i} Post-Delay")
                post_spin.setRange(0, 2_147_483_647)
                post_spin.setSuffix(" ms")
                post_spin.setSingleStep(100)
                row_layout.addWidget(post_spin)

                layout.addWidget(group)
                self.bank_widgets[i] = {
                    "action": combo,
                    "pre_delay": pre_spin,
                    "post_delay": post_spin,
                }

            layout.addStretch()

        def _path_row(self, edit: QLineEdit, accessible_name: str) -> QWidget:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(edit)

            btn_browse = QPushButton("Browse…")
            btn_browse.setAccessibleName(accessible_name)
            btn_browse.clicked.connect(lambda: self._browse_sound(edit))
            row_layout.addWidget(btn_browse)
            return row

        def _browse_sound(self, edit: QLineEdit) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select Sound File", "", "WAV Files (*.wav);;All Files (*)"
            )
            if path:
                edit.setText(path)

        def get_sound(self) -> str:
            return str(self.edit_sound.text()).strip()

        def set_sound(self, sound: str) -> None:
            self.edit_sound.setText(sound)

        def get_sequence_config(self) -> SequenceConfig:
            """Read current widget values into a SequenceConfig."""
            seq = SequenceConfig()
            for i in range(1, 5):
                w = self.bank_widgets[i]
                action_data = w["action"].currentData()
                if action_data is not None:
                    action = BankAction(action_data)
                else:
                    action = None
                cfg = BankConfig(
                    action=action,
                    pre_delay_ms=w["pre_delay"].value(),
                    post_delay_ms=w["post_delay"].value(),
                )
                seq.set_bank(i, cfg)
            return seq

        def set_sequence_config(self, seq: SequenceConfig) -> None:
            """Populate widgets from a SequenceConfig."""
            for i in range(1, 5):
                w = self.bank_widgets[i]
                bank_cfg = seq.bank(i)
                if bank_cfg.action is None:
                    w["action"].setCurrentIndex(0)
                elif bank_cfg.action == BankAction.ON:
                    w["action"].setCurrentIndex(1)
                else:
                    w["action"].setCurrentIndex(2)
                w["pre_delay"].setValue(bank_cfg.pre_delay_ms)
                w["post_delay"].setValue(bank_cfg.post_delay_ms)

    class BootSequenceEditor(_SequenceEditorPanel):
        """Editor for the boot sequence (banks 1→4)."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__("Boot", "Event Start Sound", parent)

    class ShutdownSequenceEditor(_SequenceEditorPanel):
        """Editor for the shutdown sequence (banks 4→1)."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__("Shutdown", "Event End Sound", parent)

    class ServicePanel(QWidget):
        """Windows service management controls."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)

            group = QGroupBox("Windows Service Management")
            group.setAccessibleName("Windows Service Management")
            glayout = QVBoxLayout(group)

            # Status display
            status_row = QHBoxLayout()
            status_row.addWidget(QLabel("Service Status:"))
            self.lbl_status = QLabel("Unknown")
            self.lbl_status.setAccessibleName("Service Status Display")
            status_row.addWidget(self.lbl_status)

            self.btn_refresh_status = QPushButton("Refresh")
            self.btn_refresh_status.setAccessibleName("Refresh Service Status")
            self.btn_refresh_status.clicked.connect(self._refresh_status)
            status_row.addWidget(self.btn_refresh_status)
            status_row.addStretch()
            glayout.addLayout(status_row)

            # Action buttons
            btn_layout = QGridLayout()

            self.btn_install = QPushButton("Install Service")
            self.btn_install.setAccessibleName("Install Juicer Service")
            self.btn_install.clicked.connect(self._install)
            btn_layout.addWidget(self.btn_install, 0, 0)

            self.btn_uninstall = QPushButton("Uninstall Service")
            self.btn_uninstall.setAccessibleName("Uninstall Juicer Service")
            self.btn_uninstall.clicked.connect(self._uninstall)
            btn_layout.addWidget(self.btn_uninstall, 0, 1)

            self.btn_start = QPushButton("Start Service")
            self.btn_start.setAccessibleName("Start Juicer Service")
            self.btn_start.clicked.connect(self._start)
            btn_layout.addWidget(self.btn_start, 1, 0)

            self.btn_stop = QPushButton("Stop Service")
            self.btn_stop.setAccessibleName("Stop Juicer Service")
            self.btn_stop.clicked.connect(self._stop)
            btn_layout.addWidget(self.btn_stop, 1, 1)

            glayout.addLayout(btn_layout)
            layout.addWidget(group)

            if platform.system() != "Windows":
                note = QLabel("Note: Service management is only available on Windows.")
                note.setStyleSheet("color: orange;")
                layout.addWidget(note)

            layout.addStretch()

        def _refresh_status(self) -> None:
            try:
                from juicer.service import service_status

                st = service_status()
                self.lbl_status.setText(st)
            except Exception as exc:
                self.lbl_status.setText(f"N/A ({exc})")

        def _install(self) -> None:
            try:
                from juicer.service import install_service

                install_service()
                QMessageBox.information(self, "Service", "Service installed successfully.")
                self._refresh_status()
            except Exception as exc:
                QMessageBox.warning(self, "Error", f"Install failed: {exc}")

        def _uninstall(self) -> None:
            try:
                from juicer.service import uninstall_service

                uninstall_service()
                QMessageBox.information(self, "Service", "Service uninstalled.")
                self._refresh_status()
            except Exception as exc:
                QMessageBox.warning(self, "Error", f"Uninstall failed: {exc}")

        def _start(self) -> None:
            try:
                from juicer.service import start_service

                start_service()
                QMessageBox.information(self, "Service", "Service started.")
                self._refresh_status()
            except Exception as exc:
                QMessageBox.warning(self, "Error", f"Start failed: {exc}")

        def _stop(self) -> None:
            try:
                from juicer.service import stop_service

                stop_service()
                QMessageBox.information(self, "Service", "Service stopped.")
                self._refresh_status()
            except Exception as exc:
                QMessageBox.warning(self, "Error", f"Stop failed: {exc}")

    class ImportExportPanel(QWidget):
        """TOML configuration import and export."""

        config_imported = Signal(object)  # GlobalConfig

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)

            group = QGroupBox("Configuration Import / Export")
            group.setAccessibleName("Configuration Import Export")
            glayout = QVBoxLayout(group)

            # Export
            export_row = QHBoxLayout()
            self.btn_export = QPushButton("Export to TOML…")
            self.btn_export.setAccessibleName("Export Configuration to TOML File")
            self.btn_export.clicked.connect(self._export)
            export_row.addWidget(self.btn_export)
            export_row.addStretch()
            glayout.addLayout(export_row)

            # Import
            import_row = QHBoxLayout()
            self.btn_import = QPushButton("Import from TOML…")
            self.btn_import.setAccessibleName("Import Configuration from TOML File")
            self.btn_import.clicked.connect(self._import)
            import_row.addWidget(self.btn_import)
            import_row.addStretch()
            glayout.addLayout(import_row)

            layout.addWidget(group)
            layout.addStretch()

            self._current_config: GlobalConfig | None = None
            self.current_config_provider: Callable[[], GlobalConfig] | None = None

        def set_config(self, config: GlobalConfig) -> None:
            self._current_config = config

        def _export(self) -> None:
            try:
                config = (
                    self.current_config_provider()
                    if self.current_config_provider is not None
                    else self._current_config
                )
            except Exception as exc:
                QMessageBox.warning(self, "Export", f"Current settings are invalid: {exc}")
                return
            if config is None:
                QMessageBox.warning(self, "Export", "No configuration to export.")
                return
            path, _ = QFileDialog.getSaveFileName(
                self, "Export Configuration", "juicer_config.toml", "TOML Files (*.toml)"
            )
            if path:
                try:
                    store = TomlStore(path)
                    store.save(config)
                    QMessageBox.information(self, "Export", f"Configuration exported to {path}")
                except Exception as exc:
                    QMessageBox.warning(self, "Error", f"Export failed: {exc}")

        def _import(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import Configuration", "", "TOML Files (*.toml);;All Files (*)"
            )
            if path:
                try:
                    store = TomlStore(path)
                    config = store.load()
                    self.config_imported.emit(config)
                    QMessageBox.information(
                        self,
                        "Import",
                        f"Configuration imported from {path}.\nClick Save Settings to persist it.",
                    )
                except Exception as exc:
                    QMessageBox.warning(self, "Error", f"Import failed: {exc}")

    class LogViewer(QWidget):
        """Scrolling log message display."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)

            header = QHBoxLayout()
            header.addWidget(QLabel("Application Log"))
            self.btn_clear = QPushButton("Clear")
            self.btn_clear.setAccessibleName("Clear Log Messages")
            self.btn_clear.clicked.connect(self._clear)
            header.addWidget(self.btn_clear)
            header.addStretch()
            layout.addLayout(header)

            self.text_log = QTextEdit()
            self.text_log.setAccessibleName("Log Messages")
            self.text_log.setReadOnly(True)
            self.text_log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
            layout.addWidget(self.text_log)

        @Slot(str)
        def append_message(self, message: str) -> None:
            self.text_log.append(message)
            # Auto-scroll to bottom
            sb = self.text_log.verticalScrollBar()
            if sb:
                sb.setValue(sb.maximum())

        def _clear(self) -> None:
            self.text_log.clear()

    class DeviceStatusPanel(QWidget):
        """Detailed device status queries."""

        refresh_requested = Signal()

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)

            self.btn_refresh = QPushButton("Refresh Device Status")
            self.btn_refresh.setAccessibleName("Refresh Device Status")
            self.btn_refresh.clicked.connect(self.refresh_requested.emit)
            layout.addWidget(self.btn_refresh)

            group = QGroupBox("Device Status")
            form = QFormLayout(group)
            self.labels: dict[str, QLabel] = {}
            for key, label in (
                ("id", "ID:"),
                ("power", "Power:"),
                ("metrics", "Power Metrics:"),
                ("current", "Current:"),
                ("voltage", "Voltage:"),
                ("load", "Load:"),
                ("battery", "Battery:"),
                ("battery_state", "Battery State:"),
                ("backup_time", "Backup Time:"),
            ):
                value = QLabel("Unknown")
                value.setAccessibleName(f"Device {label.rstrip(':')}")
                self.labels[key] = value
                form.addRow(label, value)
            layout.addWidget(group)
            layout.addStretch()

        def set_enabled(self, enabled: bool) -> None:
            self.btn_refresh.setEnabled(enabled)

        def apply_status(self, status: dict[str, str]) -> None:
            for key, value in status.items():
                if key in self.labels:
                    self.labels[key].setText(value)

    class DeviceConfigPanel(QWidget):
        """Device configuration commands exposed by the serial protocol."""

        load_requested = Signal()
        apply_requested = Signal(object)
        reset_requested = Signal()

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)

            group = QGroupBox("Device Configuration")
            form = QFormLayout(group)

            self.combo_buzzer = self._combo(["ON", "OFF"], "Buzzer Mode")
            form.addRow("Buzzer:", self.combo_buzzer)
            self.combo_avr = self._combo(["OFF", "STANDARD", "SENSITIVE"], "AVR Mode")
            form.addRow("AVR:", self.combo_avr)
            self.combo_feedback = self._combo(["ON", "OFF"], "Feedback Mode")
            form.addRow("Feedback:", self.combo_feedback)
            self.combo_linefeed = self._combo(["ON", "OFF"], "Linefeed Mode")
            form.addRow("Linefeed:", self.combo_linefeed)
            self.combo_brightness = self._combo(["100", "075", "050", "025"], "Brightness")
            form.addRow("Brightness:", self.combo_brightness)
            self.combo_scroll = self._combo(["5SEC", "10SEC", "OFF"], "Scroll Mode")
            form.addRow("Scroll:", self.combo_scroll)
            self.combo_sleep = self._combo(["30SEC", "60SEC", "OFF"], "Sleep Mode")
            form.addRow("Sleep:", self.combo_sleep)
            self.combo_normalvolt = self._combo(["220", "230", "240"], "Normal Voltage")
            form.addRow("Normal Voltage:", self.combo_normalvolt)

            self.spin_bthresh3 = QSpinBox()
            self.spin_bthresh3.setAccessibleName("Bank 3 Battery Threshold")
            self.spin_bthresh3.setRange(20, 100)
            self.spin_bthresh3.setSingleStep(10)
            form.addRow("Bank 3 Battery Threshold:", self.spin_bthresh3)

            self.spin_bthresh4 = QSpinBox()
            self.spin_bthresh4.setAccessibleName("Bank 4 Battery Threshold")
            self.spin_bthresh4.setRange(20, 100)
            self.spin_bthresh4.setSingleStep(10)
            form.addRow("Bank 4 Battery Threshold:", self.spin_bthresh4)

            layout.addWidget(group)

            buttons = QHBoxLayout()
            self.btn_load = QPushButton("Load from Device")
            self.btn_load.setAccessibleName("Load Device Configuration")
            self.btn_load.clicked.connect(self.load_requested.emit)
            buttons.addWidget(self.btn_load)

            self.btn_apply = QPushButton("Apply to Device")
            self.btn_apply.setAccessibleName("Apply Device Configuration")
            self.btn_apply.clicked.connect(self._emit_apply)
            buttons.addWidget(self.btn_apply)

            self.btn_reset = QPushButton("Factory Reset Device")
            self.btn_reset.setAccessibleName("Factory Reset Device")
            self.btn_reset.clicked.connect(self.reset_requested.emit)
            buttons.addWidget(self.btn_reset)
            buttons.addStretch()
            layout.addLayout(buttons)
            layout.addStretch()

        def _combo(self, values: list[str], accessible_name: str) -> QComboBox:
            combo = QComboBox()
            combo.setAccessibleName(accessible_name)
            for value in values:
                combo.addItem(value, value)
            return combo

        def _emit_apply(self) -> None:
            self.apply_requested.emit(self.current_values())

        def set_enabled(self, enabled: bool) -> None:
            for widget in (
                self.combo_buzzer,
                self.combo_avr,
                self.combo_feedback,
                self.combo_linefeed,
                self.combo_brightness,
                self.combo_scroll,
                self.combo_sleep,
                self.combo_normalvolt,
                self.spin_bthresh3,
                self.spin_bthresh4,
                self.btn_load,
                self.btn_apply,
                self.btn_reset,
            ):
                widget.setEnabled(enabled)

        def current_values(self) -> dict[str, object]:
            return {
                "buzzer": self.combo_buzzer.currentData(),
                "avr": self.combo_avr.currentData(),
                "feedback": self.combo_feedback.currentData(),
                "linefeed": self.combo_linefeed.currentData(),
                "brightness": self.combo_brightness.currentData(),
                "scroll_mode": self.combo_scroll.currentData(),
                "sleep_mode": self.combo_sleep.currentData(),
                "normalvolt": self.combo_normalvolt.currentData(),
                "bthresh3": self.spin_bthresh3.value(),
                "bthresh4": self.spin_bthresh4.value(),
            }

        def apply_values(self, values: dict[str, object]) -> None:
            mapping = {
                "buzzer": self.combo_buzzer,
                "avr": self.combo_avr,
                "feedback": self.combo_feedback,
                "linefeed": self.combo_linefeed,
                "brightness": self.combo_brightness,
                "scroll_mode": self.combo_scroll,
                "sleep_mode": self.combo_sleep,
                "normalvolt": self.combo_normalvolt,
            }
            for key, combo in mapping.items():
                value = values.get(key)
                if value is not None:
                    idx = combo.findData(str(value))
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
            if isinstance(values.get("bthresh3"), int):
                self.spin_bthresh3.setValue(cast(int, values["bthresh3"]))
            if isinstance(values.get("bthresh4"), int):
                self.spin_bthresh4.setValue(cast(int, values["bthresh4"]))

    # ──────────────────────────────────────────────────────────────────
    # Main Window
    # ──────────────────────────────────────────────────────────────────

    class MainWindow(QMainWindow):
        """Main application window with tabbed interface."""

        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Juicer — Furman F1500-UPS E Controller")
            self.setMinimumSize(700, 550)

            # State
            self._transport: Any = None
            self._client: Any = None
            self._config = GlobalConfig()
            self._busy = False
            self._workers: list[tuple[Any, Any]] = []

            # Central widget with tabs + bottom button row
            central = QWidget()
            central_layout = QVBoxLayout(central)
            central_layout.setContentsMargins(4, 4, 4, 4)
            central_layout.setSpacing(4)
            self.setCentralWidget(central)

            # Tab widget
            self.tabs = QTabWidget()
            self.tabs.setAccessibleName("Main Tab Navigation")
            central_layout.addWidget(self.tabs)

            # Bottom button row
            btn_row = QHBoxLayout()
            btn_row.setContentsMargins(0, 0, 0, 0)

            self.btn_save = QPushButton("Save Settings")
            self.btn_save.setAccessibleName("Save Settings")
            self.btn_save.clicked.connect(self._on_save_settings)
            btn_row.addWidget(self.btn_save)

            self.btn_reset = QPushButton("Reset to Defaults")
            self.btn_reset.setAccessibleName("Reset to Defaults")
            self.btn_reset.clicked.connect(self._on_reset_defaults)
            btn_row.addWidget(self.btn_reset)

            btn_row.addStretch()

            self.btn_close = QPushButton("Close")
            self.btn_close.setAccessibleName("Close Window")
            self.btn_close.clicked.connect(self.close)
            btn_row.addWidget(self.btn_close)

            central_layout.addLayout(btn_row)

            # Create panels
            self.overview = OverviewPanel()
            self.serial_settings = SerialSettingsPanel()
            self.manual_controls = ManualControlsPanel()
            self.boot_editor = BootSequenceEditor()
            self.shutdown_editor = ShutdownSequenceEditor()
            self.device_status = DeviceStatusPanel()
            self.device_config = DeviceConfigPanel()
            self.service_panel = ServicePanel()
            self.import_export = ImportExportPanel()
            self.log_viewer = LogViewer()

            # Add tabs
            self.overview_tab = QWidget()
            overview_layout = QVBoxLayout(self.overview_tab)
            overview_layout.addWidget(self.serial_settings)
            overview_layout.addWidget(self.overview)
            overview_layout.addStretch()

            self.tabs.addTab(self.overview_tab, "Overview")
            self.tabs.addTab(self.manual_controls, "Manual Control")
            self.tabs.addTab(self.boot_editor, "Boot Sequence")
            self.tabs.addTab(self.shutdown_editor, "Shutdown Sequence")
            self.tabs.addTab(self.device_status, "Device Status")
            self.tabs.addTab(self.device_config, "Device Config")
            self.tabs.addTab(self.service_panel, "Service")
            self.tabs.addTab(self.import_export, "Import/Export")
            self.tabs.addTab(self.log_viewer, "Log")

            # Status bar
            self.status_bar = QStatusBar()
            self.setStatusBar(self.status_bar)
            self.status_bar.showMessage("Ready")

            # Connect signals
            self.serial_settings.connect_requested.connect(self._connect_serial)
            self.serial_settings.disconnect_requested.connect(self._disconnect_serial)
            self.manual_controls.command_requested.connect(self._handle_command)
            self.import_export.config_imported.connect(self._on_config_imported)
            self.import_export.current_config_provider = self._config_from_widgets
            self.device_status.refresh_requested.connect(self._refresh_status)
            self.device_config.load_requested.connect(self._load_device_config)
            self.device_config.apply_requested.connect(self._apply_device_config)
            self.device_config.reset_requested.connect(self._reset_device_config)

            # Initial state
            self.manual_controls.set_enabled(False)
            self.device_status.set_enabled(False)
            self.device_config.set_enabled(False)

            # Set up logging handler
            self._log_handler = QtLogHandler()
            self._log_handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            self._log_handler.message_signal.connect(self.log_viewer.append_message)
            logging.getLogger("juicer").addHandler(self._log_handler)
            logging.getLogger("juicer").setLevel(logging.DEBUG)

            # Load default config
            self._load_config()
            self._connect_to_saved_port_if_available()

        @Slot()
        def _on_save_settings(self) -> None:
            """Save current settings to config store."""
            if self._save_config(show_errors=True):
                self.status_bar.showMessage("Settings saved")
                QMessageBox.information(self, "Settings", "Settings saved.")

        @Slot()
        def _on_reset_defaults(self) -> None:
            """Reset all settings to defaults after user confirmation."""
            reply = QMessageBox.question(
                self,
                "Reset to Defaults",
                "Reset all settings to their default values?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._apply_config(GlobalConfig())
                self.status_bar.showMessage("Settings reset to defaults")

        def _load_config(self) -> None:
            """Try to load configuration from the appropriate store."""
            try:
                store = TomlStore()
                if not store.path.exists():
                    logger.info("No config file found, using defaults")
                    self._apply_config(self._config)
                    return
                self._config = store.load()
                self._apply_config(self._config)
                logger.info("Configuration loaded")
            except Exception as exc:
                logger.warning("Could not load config: %s", exc)
                QMessageBox.warning(self, "Configuration", f"Could not load config: {exc}")
                self._apply_config(self._config)

        def _apply_config(self, config: GlobalConfig) -> None:
            """Push config values into editor widgets."""
            self._config = config
            if config.port:
                self.serial_settings.set_current_port(config.port)
            self.boot_editor.set_sound(config.event_start_sound)
            self.shutdown_editor.set_sound(config.event_stop_sound)
            self.boot_editor.set_sequence_config(config.boot)
            self.shutdown_editor.set_sequence_config(config.shutdown)
            self.import_export.set_config(config)

        def _config_from_widgets(self) -> GlobalConfig:
            """Build and validate config from current editor widgets."""
            return GlobalConfig(
                port=self.serial_settings.current_port(),
                event_start_sound=self.boot_editor.get_sound(),
                event_stop_sound=self.shutdown_editor.get_sound(),
                boot=self.boot_editor.get_sequence_config(),
                shutdown=self.shutdown_editor.get_sequence_config(),
            )

        def _save_config(self, *, show_errors: bool = False) -> bool:
            """Read editor widgets and save config."""
            try:
                self._config = self._config_from_widgets()
                store: ConfigStore = TomlStore()
                store.save(self._config)
                self.import_export.set_config(self._config)
                logger.info("Configuration saved")
                return True
            except Exception as exc:
                logger.error("Failed to save config: %s", exc)
                if show_errors:
                    QMessageBox.warning(self, "Save Failed", f"Failed to save config: {exc}")
                return False

        def _connect_to_saved_port_if_available(self) -> None:
            """Connect to the saved port on startup when it is present."""
            port = self._config.port.strip()
            if not port:
                logger.info("No serial port configured for startup connection")
                return
            if not self.serial_settings.set_current_port(port, add_if_missing=False):
                logger.info(
                    "Configured serial port not available for startup connection: %s",
                    port,
                )
                return
            self._connect_serial(port)

        def _set_busy(self, busy: bool) -> None:
            self._busy = busy
            connected = self._client is not None
            self.serial_settings.setEnabled(not busy)
            self.manual_controls.set_enabled(connected and not busy)
            self.device_status.set_enabled(connected and not busy)
            self.device_config.set_enabled(connected and not busy)

        def _run_worker(
            self,
            func: Callable[[], object],
            success: Callable[[object], None],
            error: Callable[[str], None],
            *,
            busy_message: str,
        ) -> None:
            if self._busy:
                QMessageBox.information(
                    self, "Busy", "Another device operation is already running."
                )
                return
            self._set_busy(True)
            self.status_bar.showMessage(busy_message)
            thread = QThread(self)
            worker = _SerialWorker(func)
            worker.moveToThread(thread)

            def cleanup() -> None:
                self._set_busy(False)
                self._workers = [
                    pair
                    for pair in self._workers
                    if pair[0] is not worker and pair[1] is not thread
                ]
                thread.quit()
                thread.wait()
                worker.deleteLater()
                thread.deleteLater()

            def on_finished(result: object) -> None:
                try:
                    success(result)
                finally:
                    cleanup()

            def on_error(message: str) -> None:
                try:
                    error(message)
                finally:
                    cleanup()

            thread.started.connect(worker.run)
            worker.finished.connect(on_finished)
            worker.error.connect(on_error)
            self._workers.append((worker, thread))
            thread.start()

        def _collect_status(self, client: Any) -> dict[str, str]:
            status: dict[str, str] = {}
            try:
                identity = client.query_id()
                status["id"] = (
                    f"{identity.manufacturer} {identity.model} ({identity.firmware})"
                )
            except Exception as exc:
                status["id"] = f"Unavailable ({exc})"
            try:
                outlets = client.query_outlet_status()
                for bank_num, state in outlets.banks.items():
                    status[f"bank{bank_num}"] = state.value
            except Exception as exc:
                status["outlets_error"] = str(exc)
            try:
                pwr = client.query_power_status()
                status["power"] = pwr.status.value
            except Exception as exc:
                status["power"] = f"Unavailable ({exc})"
            try:
                metrics = client.query_power()
                status["metrics"] = (
                    f"In {metrics.volts_in:g} V, Out {metrics.volts_out:g} V, "
                    f"{metrics.watts:g} W"
                )
            except Exception as exc:
                status["metrics"] = f"Unavailable ({exc})"
            try:
                current = client.query_current()
                status["current"] = f"{current.amps:g} A"
            except Exception as exc:
                status["current"] = f"Unavailable ({exc})"
            try:
                voltage = client.query_voltage()
                status["voltage"] = f"{voltage.volts:g} V"
            except Exception as exc:
                status["voltage"] = f"Unavailable ({exc})"
            try:
                load = client.query_loadstat()
                status["load"] = f"{load.percent:g}%"
            except Exception as exc:
                status["load"] = f"Unavailable ({exc})"
            try:
                bat = client.query_battery_status()
                status["battery"] = f"{bat.level}%"
            except Exception as exc:
                status["battery"] = f"Unavailable ({exc})"
            try:
                bat_state = client.query_battery_state()
                status["battery_state"] = bat_state.state.value
            except Exception as exc:
                status["battery_state"] = f"Unavailable ({exc})"
            try:
                backup = client.query_backup_time()
                status["backup_time"] = f"{backup.minutes} minutes"
            except Exception as exc:
                status["backup_time"] = f"Unavailable ({exc})"
            return status

        def _apply_status_result(self, status: dict[str, str]) -> None:
            for bank in range(1, 5):
                value = status.get(f"bank{bank}")
                if value is not None:
                    self.overview.set_bank_state(bank, value)
            if "outlets_error" in status:
                logger.warning("Could not read outlet status: %s", status["outlets_error"])
                for bank in range(1, 5):
                    self.overview.set_bank_state(bank, "Unavailable")
            self.overview.set_power_status(status.get("power", "Unavailable"))
            battery_text = status.get("battery", "Unavailable")
            self.overview.lbl_battery.setText(battery_text)
            self.device_status.apply_status(status)

        @Slot(str)
        def _connect_serial(self, port: str) -> None:
            """Open the serial transport and create a client."""
            from juicer.protocol import JuicerClient, SerialTransport

            def connect() -> object:
                transport = SerialTransport(port=port)
                transport.open()
                client = JuicerClient(transport)
                status = self._collect_status(client)
                return transport, client, status

            def success(result: object) -> None:
                transport, client, status = cast(tuple[Any, Any, dict[str, str]], result)
                self._transport = transport
                self._client = client
                self.serial_settings.set_connected(True)
                self.manual_controls.set_enabled(True)
                self.device_status.set_enabled(True)
                self.device_config.set_enabled(True)
                self.overview.set_connected(port)
                self._config.port = port
                self.status_bar.showMessage(f"Connected to {port}")
                logger.info("Connected to %s", port)
                self._apply_status_result(status)

            def error(message: str) -> None:
                logger.error("Connection failed: %s", message)
                self.status_bar.showMessage("Connection failed")
                QMessageBox.warning(self, "Connection Error", message)

            self._run_worker(
                connect,
                success,
                error,
                busy_message=f"Connecting to {port}…",
            )

        @Slot()
        def _disconnect_serial(self) -> None:
            """Close the serial transport."""
            if self._transport:
                self._transport.close()
            self._transport = None
            self._client = None
            self.serial_settings.set_connected(False)
            self.manual_controls.set_enabled(False)
            self.device_status.set_enabled(False)
            self.device_config.set_enabled(False)
            self.overview.set_disconnected()
            self.status_bar.showMessage("Disconnected")
            logger.info("Disconnected")

        def _refresh_status(self) -> None:
            """Query the UPS for current bank/power/battery status."""
            if not self._client:
                return

            def query() -> object:
                return self._collect_status(self._client)

            def success(result: object) -> None:
                status = cast(dict[str, str], result)
                self._apply_status_result(status)
                self.status_bar.showMessage("Status refreshed")

            def error(message: str) -> None:
                logger.warning("Could not refresh status: %s", message)
                self.status_bar.showMessage("Status refresh failed")
                QMessageBox.warning(self, "Status Error", message)

            self._run_worker(query, success, error, busy_message="Refreshing status…")

        @Slot(str, object)
        def _handle_command(self, command: str, args: Any) -> None:
            """Execute a manual control command."""
            if not self._client:
                QMessageBox.warning(self, "Error", "Not connected to serial port")
                return

            def execute() -> object:
                if command == "all_on":
                    responses = self._client.all_on()
                    logger.info("ALL ON: %s", responses)
                elif command == "all_off":
                    responses = self._client.all_off()
                    logger.info("ALL OFF: %s", responses)
                elif command == "switch" and args:
                    bank, state = args
                    responses = self._client.switch(bank, state)
                    logger.info("SWITCH %d %s: %s", bank, state, responses)
                else:
                    raise ValueError(f"Unknown command: {command}")
                return self._collect_status(self._client)

            def success(result: object) -> None:
                self._apply_status_result(cast(dict[str, str], result))
                self.status_bar.showMessage(f"Command sent: {command}")

            def error(message: str) -> None:
                logger.error("Command failed: %s", message)
                self.status_bar.showMessage("Command failed")
                QMessageBox.warning(self, "Command Error", message)

            self._run_worker(execute, success, error, busy_message=f"Sending {command}…")

        @Slot()
        def _load_device_config(self) -> None:
            if not self._client:
                QMessageBox.warning(self, "Error", "Not connected to serial port")
                return

            def load() -> object:
                cfg = self._client.query_list_config()
                values: dict[str, object] = {
                    "buzzer": cfg.buzzer.value if cfg.buzzer else None,
                    "avr": cfg.avr.value if cfg.avr else None,
                    "feedback": cfg.feedback.value if cfg.feedback else None,
                    "linefeed": cfg.linefeed.value if cfg.linefeed else None,
                    "brightness": cfg.brightness.value if cfg.brightness else None,
                    "scroll_mode": cfg.scroll_mode.value if cfg.scroll_mode else None,
                    "sleep_mode": cfg.sleep_mode.value if cfg.sleep_mode else None,
                    "normalvolt": cfg.normalvolt.value if cfg.normalvolt else None,
                }
                if cfg.bthresh is not None:
                    values["bthresh3"] = cfg.bthresh
                    values["bthresh4"] = cfg.bthresh
                return values

            def success(result: object) -> None:
                self.device_config.apply_values(cast(dict[str, object], result))
                self.status_bar.showMessage("Device configuration loaded")

            def error(message: str) -> None:
                logger.error("Device config load failed: %s", message)
                QMessageBox.warning(self, "Device Config", f"Load failed: {message}")

            self._run_worker(load, success, error, busy_message="Loading device configuration…")

        @Slot(object)
        def _apply_device_config(self, values_obj: object) -> None:
            if not self._client:
                QMessageBox.warning(self, "Error", "Not connected to serial port")
                return
            values = cast(dict[str, object], values_obj)

            def apply() -> object:
                self._client.set_buzzer(cast(str, values["buzzer"]))
                self._client.set_avr(cast(str, values["avr"]))
                self._client.set_feedback(cast(str, values["feedback"]))
                self._client.set_linefeed(cast(str, values["linefeed"]))
                self._client.set_bright(cast(str, values["brightness"]))
                self._client.set_scrollmode(cast(str, values["scroll_mode"]))
                self._client.set_sleepmode(cast(str, values["sleep_mode"]))
                self._client.set_normalvolt(cast(str, values["normalvolt"]))
                self._client.set_batthresh(3, cast(int, values["bthresh3"]))
                self._client.set_batthresh(4, cast(int, values["bthresh4"]))
                return self._collect_status(self._client)

            def success(result: object) -> None:
                self._apply_status_result(cast(dict[str, str], result))
                self.status_bar.showMessage("Device configuration applied")

            def error(message: str) -> None:
                logger.error("Device config apply failed: %s", message)
                QMessageBox.warning(self, "Device Config", f"Apply failed: {message}")

            self._run_worker(apply, success, error, busy_message="Applying device configuration…")

        @Slot()
        def _reset_device_config(self) -> None:
            if not self._client:
                QMessageBox.warning(self, "Error", "Not connected to serial port")
                return
            reply = QMessageBox.question(
                self,
                "Factory Reset Device",
                "Reset all device configuration settings to factory defaults?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

            def reset() -> object:
                self._client.reset_all()
                return self._collect_status(self._client)

            def success(result: object) -> None:
                self._apply_status_result(cast(dict[str, str], result))
                self.status_bar.showMessage("Device reset complete")

            def error(message: str) -> None:
                logger.error("Device reset failed: %s", message)
                QMessageBox.warning(self, "Device Config", f"Reset failed: {message}")

            self._run_worker(reset, success, error, busy_message="Resetting device…")

        @Slot(object)
        def _on_config_imported(self, config: object) -> None:
            """Handle imported configuration."""
            if isinstance(config, GlobalConfig):
                self._apply_config(config)
                logger.info("Imported configuration applied")

        def closeEvent(self, event: Any) -> None:
            """Clean up on window close."""
            if not self._save_config(show_errors=True):
                reply = QMessageBox.question(
                    self,
                    "Save Failed",
                    "Settings could not be saved. Close anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
            self._disconnect_serial()
            super().closeEvent(event)


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Launch the Juicer GUI application."""
    if not _PYSIDE6_AVAILABLE:
        if platform.system() == "Windows":
            try:
                user32 = getattr(ctypes, "windll").user32
                user32.MessageBoxW(
                    None,
                    "PySide6 is required for the GUI: pip install PySide6",
                    "Juicer GUI",
                    0x10,
                )
            except Exception:
                pass
        print("PySide6 is required for the GUI: pip install PySide6", file=sys.stderr)
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setApplicationName("Juicer")
    app.setApplicationVersion("2.0.0")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
