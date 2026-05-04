"""Juicer PySide6 GUI — complete graphical interface for the UPS controller.

Panels:
  - Overview: serial settings, connection status, bank states
  - Manual Controls: all-on, all-off, per-bank switches
  - Boot Sequence Editor: per-bank action/delay config
  - Shutdown Sequence Editor: same for shutdown
  - Service Panel: install/uninstall/start/stop with status
  - Import/Export: JSON config import/export
  - Log Viewer: scrolling log messages

All controls have ``setAccessibleName()`` and proper labels.
Keyboard-navigable with correct tab order.
Uses protocol/config/service APIs — never raw serial strings.
"""

from __future__ import annotations

import logging
import platform
import sys
from functools import partial
from pathlib import Path
from typing import Any, Optional

from juicer.config import (
    BankAction,
    BankConfig,
    ConfigStore,
    GlobalConfig,
    JsonStore,
    SequenceConfig,
)

logger = logging.getLogger(__name__)

# Guard PySide6 import for environments where it's not installed
try:
    from PySide6.QtCore import QObject, Signal, Slot
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
            if current_port:
                if not self.set_current_port(current_port, add_if_missing=False):
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

    class SoundSettingsPanel(QWidget):
        """Startup and shutdown sound path settings."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)

            group = QGroupBox("Sound Configuration")
            group.setAccessibleName("Sound Configuration")
            form = QFormLayout(group)

            self.edit_start_sound = QLineEdit()
            self.edit_start_sound.setAccessibleName("Startup Sound Path")
            form.addRow(
                "Startup Sound:",
                self._path_row(self.edit_start_sound, "Browse Startup Sound"),
            )

            self.edit_stop_sound = QLineEdit()
            self.edit_stop_sound.setAccessibleName("Shutdown Sound Path")
            form.addRow(
                "Shutdown Sound:",
                self._path_row(self.edit_stop_sound, "Browse Shutdown Sound"),
            )

            layout.addWidget(group)

        def _path_row(self, edit: QLineEdit, accessible_name: str) -> QWidget:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(edit)

            btn_browse = QPushButton("Browse…")
            btn_browse.setAccessibleName(accessible_name)
            btn_browse.clicked.connect(lambda: self._browse_sound(edit))
            layout.addWidget(btn_browse)
            return row

        def _browse_sound(self, edit: QLineEdit) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select Sound File", "", "WAV Files (*.wav);;All Files (*)"
            )
            if path:
                edit.setText(path)

        def set_sounds(self, start_sound: str, stop_sound: str) -> None:
            self.edit_start_sound.setText(start_sound)
            self.edit_stop_sound.setText(stop_sound)

        def get_sounds(self) -> tuple[str, str]:
            return self.edit_start_sound.text().strip(), self.edit_stop_sound.text().strip()

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

        def __init__(self, label: str, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self._label = label
            layout = QVBoxLayout(self)

            self.bank_widgets: dict[int, dict[str, Any]] = {}

            for i in range(1, 5):
                group = QGroupBox(f"Bank {i}")
                group.setAccessibleName(f"{label} Bank {i} Configuration")
                form = QFormLayout(group)

                # Action combo
                combo = QComboBox()
                combo.setAccessibleName(f"{label} Bank {i} Action")
                combo.addItem("No Action", None)
                combo.addItem("Turn ON", 1)
                combo.addItem("Turn OFF", 0)
                form.addRow("Action:", combo)

                # Pre-delay
                pre_spin = QSpinBox()
                pre_spin.setAccessibleName(f"{label} Bank {i} Pre-Delay")
                pre_spin.setRange(0, 60000)
                pre_spin.setSuffix(" ms")
                pre_spin.setSingleStep(100)
                form.addRow("Delay Before:", pre_spin)

                # Post-delay
                post_spin = QSpinBox()
                post_spin.setAccessibleName(f"{label} Bank {i} Post-Delay")
                post_spin.setRange(0, 60000)
                post_spin.setSuffix(" ms")
                post_spin.setSingleStep(100)
                form.addRow("Delay After:", post_spin)

                layout.addWidget(group)
                self.bank_widgets[i] = {
                    "action": combo,
                    "pre_delay": pre_spin,
                    "post_delay": post_spin,
                }

            layout.addStretch()

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
            super().__init__("Boot", parent)

    class ShutdownSequenceEditor(_SequenceEditorPanel):
        """Editor for the shutdown sequence (banks 4→1)."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__("Shutdown", parent)

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
        """JSON configuration import and export."""

        config_imported = Signal(object)  # GlobalConfig

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)

            group = QGroupBox("Configuration Import / Export")
            group.setAccessibleName("Configuration Import Export")
            glayout = QVBoxLayout(group)

            # Export
            export_row = QHBoxLayout()
            self.btn_export = QPushButton("Export to JSON…")
            self.btn_export.setAccessibleName("Export Configuration to JSON File")
            self.btn_export.clicked.connect(self._export)
            export_row.addWidget(self.btn_export)
            export_row.addStretch()
            glayout.addLayout(export_row)

            # Import
            import_row = QHBoxLayout()
            self.btn_import = QPushButton("Import from JSON…")
            self.btn_import.setAccessibleName("Import Configuration from JSON File")
            self.btn_import.clicked.connect(self._import)
            import_row.addWidget(self.btn_import)
            import_row.addStretch()
            glayout.addLayout(import_row)

            layout.addWidget(group)
            layout.addStretch()

            self._current_config: GlobalConfig | None = None

        def set_config(self, config: GlobalConfig) -> None:
            self._current_config = config

        def _export(self) -> None:
            if self._current_config is None:
                QMessageBox.warning(self, "Export", "No configuration to export.")
                return
            path, _ = QFileDialog.getSaveFileName(
                self, "Export Configuration", "juicer_config.json", "JSON Files (*.json)"
            )
            if path:
                try:
                    store = JsonStore(path)
                    store.save(self._current_config)
                    QMessageBox.information(self, "Export", f"Configuration exported to {path}")
                except Exception as exc:
                    QMessageBox.warning(self, "Error", f"Export failed: {exc}")

        def _import(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import Configuration", "", "JSON Files (*.json);;All Files (*)"
            )
            if path:
                try:
                    store = JsonStore(path)
                    config = store.load()
                    self.config_imported.emit(config)
                    QMessageBox.information(
                        self, "Import", f"Configuration imported from {path}"
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
            self.sound_settings = SoundSettingsPanel()
            self.manual_controls = ManualControlsPanel()
            self.boot_editor = BootSequenceEditor()
            self.shutdown_editor = ShutdownSequenceEditor()
            self.service_panel = ServicePanel()
            self.import_export = ImportExportPanel()
            self.log_viewer = LogViewer()

            # Add tabs
            self.overview_tab = QWidget()
            overview_layout = QVBoxLayout(self.overview_tab)
            overview_layout.addWidget(self.serial_settings)
            overview_layout.addWidget(self.sound_settings)
            overview_layout.addWidget(self.overview)
            overview_layout.addStretch()

            self.tabs.addTab(self.overview_tab, "Overview")
            self.tabs.addTab(self.manual_controls, "Manual Control")
            self.tabs.addTab(self.boot_editor, "Boot Sequence")
            self.tabs.addTab(self.shutdown_editor, "Shutdown Sequence")
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

            # Initial state
            self.manual_controls.set_enabled(False)

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
            self._save_config()
            self.status_bar.showMessage("Settings saved")

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
                store: ConfigStore
                if platform.system() == "Windows":
                    from juicer.config import WindowsRegistryStore

                    store = WindowsRegistryStore()
                else:
                    json_path = Path.home() / ".juicer" / "config.json"
                    if not json_path.exists():
                        logger.info("No config file found, using defaults")
                        self._apply_config(self._config)
                        return
                    store = JsonStore(json_path)
                self._config = store.load()
                self._apply_config(self._config)
                logger.info("Configuration loaded")
            except Exception as exc:
                logger.warning("Could not load config: %s", exc)
                self._apply_config(self._config)

        def _apply_config(self, config: GlobalConfig) -> None:
            """Push config values into editor widgets."""
            self._config = config
            if config.port:
                self.serial_settings.set_current_port(config.port)
            self.sound_settings.set_sounds(config.start_sound, config.stop_sound)
            self.boot_editor.set_sequence_config(config.boot)
            self.shutdown_editor.set_sequence_config(config.shutdown)
            self.import_export.set_config(config)

        def _save_config(self) -> None:
            """Read editor widgets and save config."""
            self._config.port = self.serial_settings.current_port()
            self._config.start_sound, self._config.stop_sound = self.sound_settings.get_sounds()
            self._config.boot = self.boot_editor.get_sequence_config()
            self._config.shutdown = self.shutdown_editor.get_sequence_config()
            try:
                store: ConfigStore
                if platform.system() == "Windows":
                    from juicer.config import WindowsRegistryStore

                    store = WindowsRegistryStore()
                else:
                    json_path = Path.home() / ".juicer" / "config.json"
                    store = JsonStore(json_path)
                store.save(self._config)
                self.import_export.set_config(self._config)
                logger.info("Configuration saved")
            except Exception as exc:
                logger.error("Failed to save config: %s", exc)

        def _connect_to_saved_port_if_available(self) -> None:
            """Connect to the saved port on startup when it is present."""
            port = self._config.port.strip()
            if not port:
                logger.info("No saved serial port configured; skipping startup connection")
                return
            if not self.serial_settings.set_current_port(port, add_if_missing=False):
                logger.info(
                    "Saved serial port is not available; skipping startup connection: %s",
                    port,
                )
                return
            self._connect_serial(port)

        @Slot(str)
        def _connect_serial(self, port: str) -> None:
            """Open the serial transport and create a client."""
            from juicer.protocol import JuicerClient, SerialTransport

            try:
                self._transport = SerialTransport(port=port)
                self._transport.open()
                self._client = JuicerClient(self._transport)
                self.serial_settings.set_connected(True)
                self.manual_controls.set_enabled(True)
                self.overview.set_connected(port)
                self._config.port = port
                self.status_bar.showMessage(f"Connected to {port}")
                logger.info("Connected to %s", port)

                # Attempt to read initial status
                self._refresh_status()
            except Exception as exc:
                logger.error("Connection failed: %s", exc)
                QMessageBox.warning(self, "Connection Error", str(exc))

        @Slot()
        def _disconnect_serial(self) -> None:
            """Close the serial transport."""
            if self._transport:
                self._transport.close()
            self._transport = None
            self._client = None
            self.serial_settings.set_connected(False)
            self.manual_controls.set_enabled(False)
            self.overview.set_disconnected()
            self.status_bar.showMessage("Disconnected")
            logger.info("Disconnected")

        def _refresh_status(self) -> None:
            """Query the UPS for current bank/power/battery status."""
            if not self._client:
                return
            try:
                outlets = self._client.query_outlet_status()
                for bank_num, state in outlets.banks.items():
                    self.overview.set_bank_state(bank_num, state.value)
            except Exception as exc:
                logger.warning("Could not read outlet status: %s", exc)

            try:
                pwr = self._client.query_power_status()
                self.overview.set_power_status(pwr.status.value)
            except Exception:
                pass

            try:
                bat = self._client.query_battery_status()
                self.overview.set_battery_level(bat.level)
            except Exception:
                pass

        @Slot(str, object)
        def _handle_command(self, command: str, args: Any) -> None:
            """Execute a manual control command."""
            if not self._client:
                QMessageBox.warning(self, "Error", "Not connected to serial port")
                return

            try:
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
                    logger.warning("Unknown command: %s", command)
                    return

                # Refresh status after command
                self._refresh_status()
                self.status_bar.showMessage(f"Command sent: {command}")

            except Exception as exc:
                logger.error("Command failed: %s", exc)
                QMessageBox.warning(self, "Command Error", str(exc))

        @Slot(object)
        def _on_config_imported(self, config: object) -> None:
            """Handle imported configuration."""
            if isinstance(config, GlobalConfig):
                self._apply_config(config)
                logger.info("Imported configuration applied")

        def closeEvent(self, event: Any) -> None:
            """Clean up on window close."""
            self._save_config()
            self._disconnect_serial()
            super().closeEvent(event)


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Launch the Juicer GUI application."""
    if not _PYSIDE6_AVAILABLE:
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
