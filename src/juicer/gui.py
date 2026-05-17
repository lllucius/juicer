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

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

MAX_QSPINBOX_DELAY_MS = 2_147_483_647
NUM_BANKS = 4

# Guard PySide6 import for environments where it's not installed
try:
    from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
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
            """Create the internal QObject used to forward log records into Qt."""
            super().__init__()
            self._signal = _QtLogSignal()

        @property
        def message_signal(self) -> Signal:
            """Expose the Qt signal consumed by the log viewer widget."""
            return self._signal.message

        def emit(self, record: logging.LogRecord) -> None:
            """Format and forward one logging record to the GUI thread."""
            msg = self.format(record)
            self._signal.message.emit(msg)

    def _set_help(widget: QWidget, text: str) -> None:
        """Set tooltip and status-tip help text for a widget."""
        widget.setToolTip(text)
        widget.setStatusTip(text)

    def _label(text: str, buddy: QWidget | None = None, help_text: str | None = None) -> QLabel:
        """Create a label with an optional accelerator buddy and tooltip."""
        label = QLabel(text)
        if buddy is not None:
            label.setBuddy(buddy)
        if help_text is not None:
            _set_help(label, help_text)
            if buddy is not None:
                _set_help(buddy, help_text)
        return label

    # ──────────────────────────────────────────────────────────────────
    # Worker for background serial operations
    # ──────────────────────────────────────────────────────────────────

    class _SerialWorker(QObject):
        """Run serial operations off the main thread."""

        finished = Signal(object)  # result or exception
        error = Signal(str)

        def __init__(self, func: Any, *args: Any, **kwargs: Any) -> None:
            """Capture the callable and arguments that will run in a worker thread."""
            super().__init__()
            self._func = func
            self._args = args
            self._kwargs = kwargs

        @Slot()
        def run(self) -> None:
            """Execute the queued callable and emit either its result or error text."""
            try:
                result = self._func(*self._args, **self._kwargs)
                self.finished.emit(result)
            except Exception as exc:
                self.error.emit(str(exc))

    class _WorkerCallbacks(QObject):
        """Dispatch worker results in the GUI thread."""

        def __init__(
            self,
            success: Callable[[object], None],
            error: Callable[[str], None],
            cleanup: Callable[[], None],
            parent: QObject,
        ) -> None:
            """Store main-thread callbacks for worker completion, failure, and teardown."""
            super().__init__(parent)
            self._success = success
            self._error = error
            self._cleanup = cleanup

        @Slot(object)
        def on_finished(self, result: object) -> None:
            """Handle a successful worker result and always release thread resources."""
            try:
                self._success(result)
            finally:
                self._cleanup()

        @Slot(str)
        def on_error(self, message: str) -> None:
            """Handle a worker failure message and always release thread resources."""
            try:
                self._error(message)
            finally:
                self._cleanup()

    # ──────────────────────────────────────────────────────────────────
    # Panels
    # ──────────────────────────────────────────────────────────────────

    class OverviewPanel(QWidget):
        """Connection status and bank states at a glance."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            """Build the read-only overview widgets shown on the first tab."""
            super().__init__(parent)
            layout = QVBoxLayout(self)

            # Connection status
            conn_group = QGroupBox("Connection")
            conn_group.setAccessibleName("Connection Status Group")
            conn_layout = QFormLayout(conn_group)

            self.lbl_port = QLabel("Not connected")
            self.lbl_port.setAccessibleName("Connected Port")
            _set_help(self.lbl_port, "The serial port currently connected to the UPS.")
            conn_layout.addRow("Port:", self.lbl_port)

            self.lbl_status = QLabel("Disconnected")
            self.lbl_status.setAccessibleName("Connection Status")
            _set_help(self.lbl_status, "The current serial connection state.")
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
                _set_help(lbl_state, f"The current on/off state reported for outlet bank {i}.")
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
            _set_help(self.lbl_power, "The current mains power status reported by the UPS.")
            power_layout.addRow("Mains:", self.lbl_power)

            self.lbl_battery = QLabel("Unknown")
            self.lbl_battery.setAccessibleName("Battery Level")
            _set_help(self.lbl_battery, "The current battery charge level reported by the UPS.")
            power_layout.addRow("Battery:", self.lbl_battery)

            layout.addWidget(power_group)
            layout.addStretch()

        def set_connected(self, port: str) -> None:
            """Show that the GUI is connected to the supplied serial port."""
            self.lbl_port.setText(port)
            self.lbl_status.setText("Connected")
            self.lbl_status.setStyleSheet("color: green; font-weight: bold;")

        def set_disconnected(self) -> None:
            """Reset the overview to its disconnected visual state."""
            self.lbl_port.setText("Not connected")
            self.lbl_status.setText("Disconnected")
            self.lbl_status.setStyleSheet("color: red;")

        def set_bank_state(self, bank: int, state: str) -> None:
            """Update one bank label when a fresh outlet state is available."""
            if bank in self.bank_labels:
                self.bank_labels[bank].setText(state)

        def set_power_status(self, status: str) -> None:
            """Display the latest reported mains power status."""
            self.lbl_power.setText(status)

        def set_battery_level(self, level: int) -> None:
            """Display the latest reported battery percentage."""
            self.lbl_battery.setText(f"{level}%")

    class SerialSettingsPanel(QWidget):
        """Port selection and connect/disconnect controls."""

        connect_requested = Signal(str)
        disconnect_requested = Signal()

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            """Build the serial-port selector and connection buttons."""
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
            _set_help(self.combo_port, "Select or type the serial port connected to the UPS.")
            port_row.addWidget(self.combo_port)

            self.btn_refresh = QPushButton("&Refresh")
            self.btn_refresh.setAccessibleName("Refresh Ports")
            _set_help(self.btn_refresh, "Refresh the list of available serial ports.")
            self.btn_refresh.clicked.connect(self._refresh_ports)
            port_row.addWidget(self.btn_refresh)

            form.addRow(_label("&Port:", self.combo_port), port_row)

            # Baud rate display (read-only, always 9600)
            self.lbl_baud = QLabel("9600-8-N-1 (fixed)")
            self.lbl_baud.setAccessibleName("Baud Rate Display")
            _set_help(self.lbl_baud, "The fixed serial settings used by the UPS protocol.")
            form.addRow("Settings:", self.lbl_baud)

            layout.addWidget(form_group)

            # Connect / Disconnect buttons
            btn_row = QHBoxLayout()
            self.btn_connect = QPushButton("&Connect")
            self.btn_connect.setAccessibleName("Connect to Serial Port")
            _set_help(self.btn_connect, "Open the selected serial port and connect to the UPS.")
            self.btn_connect.clicked.connect(self._on_connect)
            btn_row.addWidget(self.btn_connect)

            self.btn_disconnect = QPushButton("&Disconnect")
            self.btn_disconnect.setAccessibleName("Disconnect from Serial Port")
            _set_help(self.btn_disconnect, "Close the active serial connection.")
            self.btn_disconnect.setEnabled(False)
            self.btn_disconnect.clicked.connect(self._on_disconnect)
            btn_row.addWidget(self.btn_disconnect)

            layout.addLayout(btn_row)

            self._refresh_ports()

        def _refresh_ports(self) -> None:
            """Reload the port list while preserving the current typed selection."""
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
            """Emit a connect request for the currently selected serial port."""
            port = self.current_port()
            if port:
                self.connect_requested.emit(port)
            else:
                logger.warning("Connect requested without a selected serial port")

        def _on_disconnect(self) -> None:
            """Emit a request to close the active serial connection."""
            self.disconnect_requested.emit()

        def current_port(self) -> str:
            """Return the effective port value from the editable combo box."""
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
            """Return the set of concrete port identifiers listed in the combo box."""
            ports: set[str] = set()
            for i in range(self.combo_port.count()):
                data = self.combo_port.itemData(i)
                if data:
                    ports.add(str(data))
            return ports

        def set_current_port(self, port: str, *, add_if_missing: bool = True) -> bool:
            """Select a known port or keep free-form text for a manually typed one."""
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
            """Toggle widgets based on whether the serial connection is active."""
            self.btn_connect.setEnabled(not connected)
            self.btn_disconnect.setEnabled(connected)
            self.combo_port.setEnabled(not connected)
            self.btn_refresh.setEnabled(not connected)



    class ManualControlsPanel(QWidget):
        """All-on, all-off, and per-bank switches."""

        command_requested = Signal(str, object)  # command_name, args

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            """Build manual outlet control buttons for all supported operations."""
            super().__init__(parent)
            layout = QVBoxLayout(self)

            # Global controls
            global_group = QGroupBox("Global Controls")
            global_group.setAccessibleName("Global Outlet Controls")
            global_layout = QHBoxLayout(global_group)

            self.btn_all_on = QPushButton("&All ON")
            self.btn_all_on.setAccessibleName("Turn All Banks On")
            _set_help(self.btn_all_on, "Turn all UPS outlet banks on.")
            self.btn_all_on.clicked.connect(lambda: self.command_requested.emit("all_on", None))
            global_layout.addWidget(self.btn_all_on)

            self.btn_all_off = QPushButton("All O&FF")
            self.btn_all_off.setAccessibleName("Turn All Banks Off")
            _set_help(self.btn_all_off, "Turn all UPS outlet banks off.")
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
                _set_help(btn_on, f"Turn outlet bank {i} on.")
                btn_on.clicked.connect(partial(self._switch_bank, i, "ON"))
                banks_layout.addWidget(btn_on, i - 1, 1)

                btn_off = QPushButton("OFF")
                btn_off.setAccessibleName(f"Turn Bank {i} Off")
                _set_help(btn_off, f"Turn outlet bank {i} off.")
                btn_off.clicked.connect(partial(self._switch_bank, i, "OFF"))
                banks_layout.addWidget(btn_off, i - 1, 2)

                self.bank_buttons[i] = {"on": btn_on, "off": btn_off}

            layout.addWidget(banks_group)
            layout.addStretch()

        def _switch_bank(self, bank: int, state: str) -> None:
            """Emit a single-bank switch request for the selected outlet bank."""
            self.command_requested.emit("switch", (bank, state))

        def set_enabled(self, enabled: bool) -> None:
            """Enable or disable all manual-control buttons together."""
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
            """Build shared sound and bank-delay editors for one named sequence."""
            super().__init__(parent)
            self._label = label
            layout = QVBoxLayout(self)

            # Sound file section
            sound_group = QGroupBox(f"{sound_label}")
            sound_group.setAccessibleName(f"{sound_label} Configuration")
            sound_form = QFormLayout(sound_group)
            self.edit_sound = QLineEdit()
            self.edit_sound.setAccessibleName(f"{sound_label} Path")
            _set_help(
                self.edit_sound,
                f"Path to the WAV file to play for the {sound_label.lower()}.",
            )
            sound_browse_row = self._path_row(self.edit_sound, f"Browse {sound_label}")
            sound_form.addRow(_label(f"{sound_label} Path:", self.edit_sound), sound_browse_row)
            layout.addWidget(sound_group)

            self.bank_widgets: dict[int, dict[str, Any]] = {}

            for i in range(1, 5):
                group = QGroupBox(f"Bank {i}")
                group.setAccessibleName(f"{label} Bank {i} Configuration")
                row_layout = QHBoxLayout(group)

                # Action combo
                combo = QComboBox()
                combo.setAccessibleName(f"{label} Bank {i} Action")
                combo.addItem("No Action", None)
                combo.addItem("Turn ON", 1)
                combo.addItem("Turn OFF", 0)
                _set_help(combo, f"Choose what the {label.lower()} sequence does to bank {i}.")
                row_layout.addWidget(_label("Action:", combo))
                row_layout.addWidget(combo)

                # Pre-delay
                pre_spin = QSpinBox()
                pre_spin.setAccessibleName(f"{label} Bank {i} Pre-Delay")
                pre_spin.setRange(0, MAX_QSPINBOX_DELAY_MS)
                pre_spin.setSuffix(" ms")
                pre_spin.setSingleStep(100)
                _set_help(pre_spin, f"Delay before the {label.lower()} action for bank {i}.")
                row_layout.addWidget(_label("Delay Before:", pre_spin))
                row_layout.addWidget(pre_spin)

                # Post-delay
                post_spin = QSpinBox()
                post_spin.setAccessibleName(f"{label} Bank {i} Post-Delay")
                post_spin.setRange(0, MAX_QSPINBOX_DELAY_MS)
                post_spin.setSuffix(" ms")
                post_spin.setSingleStep(100)
                _set_help(post_spin, f"Delay after the {label.lower()} action for bank {i}.")
                row_layout.addWidget(_label("Delay After:", post_spin))
                row_layout.addWidget(post_spin)

                layout.addWidget(group)
                self.bank_widgets[i] = {
                    "action": combo,
                    "pre_delay": pre_spin,
                    "post_delay": post_spin,
                }

            layout.addStretch()

        def _path_row(self, edit: QLineEdit, accessible_name: str) -> QWidget:
            """Create a file-path row with an attached browse button."""
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(edit)

            btn_browse = QPushButton("Browse…")
            btn_browse.setAccessibleName(accessible_name)
            _set_help(btn_browse, "Browse for a WAV sound file.")
            btn_browse.clicked.connect(lambda: self._browse_sound(edit))
            row_layout.addWidget(btn_browse)
            return row

        def _browse_sound(self, edit: QLineEdit) -> None:
            """Prompt for a WAV file and copy the chosen path into the target field."""
            path, _ = QFileDialog.getOpenFileName(
                self, "Select Sound File", "", "WAV Files (*.wav);;All Files (*)"
            )
            if path:
                edit.setText(path)

        def get_sound(self) -> str:
            """Return the trimmed sound path currently shown in the editor."""
            return str(self.edit_sound.text()).strip()

        def set_sound(self, sound: str) -> None:
            """Populate the sound-path field from configuration data."""
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
            """Configure the shared editor with boot-sequence labels."""
            super().__init__("Boot", "Event Start Sound", parent)

    class ShutdownSequenceEditor(_SequenceEditorPanel):
        """Editor for the shutdown sequence (banks 4→1)."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            """Configure the shared editor with shutdown-sequence labels."""
            super().__init__("Shutdown", "Event End Sound", parent)

    class ServicePanel(QWidget):
        """Windows service management controls."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            """Build service status and management buttons for Windows deployments."""
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
            _set_help(self.lbl_status, "The current Windows service status.")
            status_row.addWidget(self.lbl_status)

            self.btn_refresh_status = QPushButton("Refresh")
            self.btn_refresh_status.setAccessibleName("Refresh Service Status")
            _set_help(self.btn_refresh_status, "Refresh the Juicer Windows service status.")
            self.btn_refresh_status.clicked.connect(self._refresh_status)
            status_row.addWidget(self.btn_refresh_status)
            status_row.addStretch()
            glayout.addLayout(status_row)

            # Action buttons
            btn_layout = QGridLayout()

            self.btn_install = QPushButton("&Install Service")
            self.btn_install.setAccessibleName("Install Juicer Service")
            _set_help(self.btn_install, "Install the Juicer Windows service.")
            self.btn_install.clicked.connect(self._install)
            btn_layout.addWidget(self.btn_install, 0, 0)

            self.btn_uninstall = QPushButton("&Uninstall Service")
            self.btn_uninstall.setAccessibleName("Uninstall Juicer Service")
            _set_help(self.btn_uninstall, "Uninstall the Juicer Windows service.")
            self.btn_uninstall.clicked.connect(self._uninstall)
            btn_layout.addWidget(self.btn_uninstall, 0, 1)

            self.btn_start = QPushButton("Start Service")
            self.btn_start.setAccessibleName("Start Juicer Service")
            _set_help(self.btn_start, "Start the Juicer Windows service.")
            self.btn_start.clicked.connect(self._start)
            btn_layout.addWidget(self.btn_start, 1, 0)

            self.btn_stop = QPushButton("S&top Service")
            self.btn_stop.setAccessibleName("Stop Juicer Service")
            _set_help(self.btn_stop, "Stop the Juicer Windows service.")
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
            """Refresh the displayed Windows service status string."""
            try:
                from juicer.service import service_status

                st = service_status()
                self.lbl_status.setText(st)
            except Exception as exc:
                self.lbl_status.setText(f"N/A ({exc})")

        def _install(self) -> None:
            """Install the Windows service and refresh the on-screen status."""
            try:
                from juicer.service import install_service

                install_service()
                QMessageBox.information(self, "Service", "Service installed successfully.")
                self._refresh_status()
            except Exception as exc:
                QMessageBox.warning(self, "Error", f"Install failed: {exc}")

        def _uninstall(self) -> None:
            """Uninstall the Windows service and refresh the on-screen status."""
            try:
                from juicer.service import uninstall_service

                uninstall_service()
                QMessageBox.information(self, "Service", "Service uninstalled.")
                self._refresh_status()
            except Exception as exc:
                QMessageBox.warning(self, "Error", f"Uninstall failed: {exc}")

        def _start(self) -> None:
            """Start the Windows service and refresh the on-screen status."""
            try:
                from juicer.service import start_service

                start_service()
                QMessageBox.information(self, "Service", "Service started.")
                self._refresh_status()
            except Exception as exc:
                QMessageBox.warning(self, "Error", f"Start failed: {exc}")

        def _stop(self) -> None:
            """Stop the Windows service and refresh the on-screen status."""
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
            """Build file-picking controls for exporting and importing settings."""
            super().__init__(parent)
            layout = QVBoxLayout(self)

            group = QGroupBox("Configuration Import / Export")
            group.setAccessibleName("Configuration Import Export")
            glayout = QVBoxLayout(group)

            # Export
            export_row = QHBoxLayout()
            self.btn_export = QPushButton("E&xport to TOML…")
            self.btn_export.setAccessibleName("Export Configuration to TOML File")
            _set_help(self.btn_export, "Export the current configuration to a TOML file.")
            self.btn_export.clicked.connect(self._export)
            export_row.addWidget(self.btn_export)
            export_row.addStretch()
            glayout.addLayout(export_row)

            # Import
            import_row = QHBoxLayout()
            self.btn_import = QPushButton("I&mport from TOML…")
            self.btn_import.setAccessibleName("Import Configuration from TOML File")
            _set_help(self.btn_import, "Import configuration settings from a TOML file.")
            self.btn_import.clicked.connect(self._import)
            import_row.addWidget(self.btn_import)
            import_row.addStretch()
            glayout.addLayout(import_row)

            layout.addWidget(group)
            layout.addStretch()

            self._current_config: GlobalConfig | None = None
            self.current_config_provider: Callable[[], GlobalConfig] | None = None

        def set_config(self, config: GlobalConfig) -> None:
            """Remember the most recently applied configuration for later export."""
            self._current_config = config

        def _export(self) -> None:
            """Write the current GUI configuration to a user-chosen TOML file."""
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
            """Load configuration from a TOML file and emit it for application."""
            path, _ = QFileDialog.getOpenFileName(
                self, "Import Configuration", "", "TOML Files (*.toml);;All Files (*)"
            )
            if path:
                try:
                    store = TomlStore(path)
                    config = store.load()
                    self.config_imported.emit(config)
                    msg = QMessageBox(self)
                    msg.setIcon(QMessageBox.Icon.Information)
                    msg.setWindowTitle("Import")
                    msg.setText(f"Configuration imported from {path}.")
                    msg.setInformativeText("Click Save Settings to persist it.")
                    msg.exec()
                except Exception as exc:
                    QMessageBox.warning(self, "Error", f"Import failed: {exc}")

    class LogViewer(QWidget):
        """Scrolling log message display."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            """Build the read-only log pane and its clear button."""
            super().__init__(parent)
            layout = QVBoxLayout(self)

            header = QHBoxLayout()
            header.addWidget(QLabel("Application Log"))
            self.btn_clear = QPushButton("C&lear")
            self.btn_clear.setAccessibleName("Clear Log Messages")
            _set_help(self.btn_clear, "Clear the GUI log viewer.")
            self.btn_clear.clicked.connect(self._clear)
            header.addWidget(self.btn_clear)
            header.addStretch()
            layout.addLayout(header)

            self.text_log = QTextEdit()
            self.text_log.setAccessibleName("Log Messages")
            self.text_log.setReadOnly(True)
            self.text_log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
            _set_help(self.text_log, "Application log messages for this GUI session.")
            layout.addWidget(self.text_log)

        @Slot(str)
        def append_message(self, message: str) -> None:
            """Append one log line and keep the newest message visible."""
            self.text_log.append(message)
            # Auto-scroll to bottom
            sb = self.text_log.verticalScrollBar()
            if sb:
                sb.setValue(sb.maximum())

        def _clear(self) -> None:
            """Remove all currently displayed log lines."""
            self.text_log.clear()

    class DeviceStatusPanel(QWidget):
        """Detailed device status queries."""

        refresh_requested = Signal()

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            """Build labels for the extended device status queries."""
            super().__init__(parent)
            layout = QVBoxLayout(self)

            self.btn_refresh = QPushButton("Refresh Device Status")
            self.btn_refresh.setAccessibleName("Refresh Device Status")
            _set_help(self.btn_refresh, "Query the connected UPS for its latest status.")
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
                _set_help(value, f"The current device {label.rstrip(':').lower()} value.")
                self.labels[key] = value
                form.addRow(label, value)
            layout.addWidget(group)
            layout.addStretch()

        def set_enabled(self, enabled: bool) -> None:
            """Enable or disable the refresh button based on connection state."""
            self.btn_refresh.setEnabled(enabled)

        def apply_status(self, status: dict[str, str]) -> None:
            """Copy collected status strings into the visible label set."""
            for key, value in status.items():
                if key in self.labels:
                    self.labels[key].setText(value)

    class DeviceConfigPanel(QWidget):
        """Device configuration commands exposed by the serial protocol."""

        load_requested = Signal()
        apply_requested = Signal(object)
        reset_requested = Signal()

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            """Build editable widgets for every protocol-backed device setting."""
            super().__init__(parent)
            layout = QVBoxLayout(self)

            group = QGroupBox("Device Configuration")
            form = QFormLayout(group)

            self.combo_buzzer = self._combo(["ON", "OFF"], "Buzzer Mode")
            form.addRow(_label("&Buzzer:", self.combo_buzzer), self.combo_buzzer)
            self.combo_avr = self._combo(["OFF", "STANDARD", "SENSITIVE"], "AVR Mode")
            form.addRow(_label("AVR:", self.combo_avr), self.combo_avr)
            self.combo_feedback = self._combo(["ON", "OFF"], "Feedback Mode")
            form.addRow(_label("Feedbac&k:", self.combo_feedback), self.combo_feedback)
            self.combo_linefeed = self._combo(["ON", "OFF"], "Linefeed Mode")
            form.addRow(_label("Linefeed:", self.combo_linefeed), self.combo_linefeed)
            self.combo_brightness = self._combo(["100", "075", "050", "025"], "Brightness")
            form.addRow(_label("Bright&ness:", self.combo_brightness), self.combo_brightness)
            self.combo_scroll = self._combo(["5SEC", "10SEC", "OFF"], "Scroll Mode")
            form.addRow(_label("&Scroll:", self.combo_scroll), self.combo_scroll)
            self.combo_sleep = self._combo(["30SEC", "60SEC", "OFF"], "Sleep Mode")
            form.addRow(_label("Sleep:", self.combo_sleep), self.combo_sleep)
            self.combo_normalvolt = self._combo(["220", "230", "240"], "Normal Voltage")
            form.addRow(_label("Normal Volta&ge:", self.combo_normalvolt), self.combo_normalvolt)

            self.spin_bthresh3 = QSpinBox()
            self.spin_bthresh3.setAccessibleName("Bank 3 Battery Threshold")
            self.spin_bthresh3.setRange(20, 100)
            self.spin_bthresh3.setSingleStep(10)
            _set_help(self.spin_bthresh3, "Battery threshold percentage for outlet bank 3.")
            form.addRow(
                _label("Bank &3 Battery Threshold:", self.spin_bthresh3),
                self.spin_bthresh3,
            )

            self.spin_bthresh4 = QSpinBox()
            self.spin_bthresh4.setAccessibleName("Bank 4 Battery Threshold")
            self.spin_bthresh4.setRange(20, 100)
            self.spin_bthresh4.setSingleStep(10)
            _set_help(self.spin_bthresh4, "Battery threshold percentage for outlet bank 4.")
            form.addRow(
                _label("Bank &4 Battery Threshold:", self.spin_bthresh4),
                self.spin_bthresh4,
            )

            layout.addWidget(group)

            buttons = QHBoxLayout()
            self.btn_load = QPushButton("L&oad from Device")
            self.btn_load.setAccessibleName("Load Device Configuration")
            _set_help(self.btn_load, "Load device configuration values from the connected UPS.")
            self.btn_load.clicked.connect(self.load_requested.emit)
            buttons.addWidget(self.btn_load)

            self.btn_apply = QPushButton("Appl&y to Device")
            self.btn_apply.setAccessibleName("Apply Device Configuration")
            _set_help(self.btn_apply, "Apply these configuration values to the connected UPS.")
            self.btn_apply.clicked.connect(self._emit_apply)
            buttons.addWidget(self.btn_apply)

            self.btn_reset = QPushButton("Factory Reset Device")
            self.btn_reset.setAccessibleName("Factory Reset Device")
            _set_help(self.btn_reset, "Reset the connected UPS configuration to factory defaults.")
            self.btn_reset.clicked.connect(self.reset_requested.emit)
            buttons.addWidget(self.btn_reset)
            buttons.addStretch()
            layout.addLayout(buttons)
            layout.addStretch()

        def _combo(self, values: list[str], accessible_name: str) -> QComboBox:
            """Create a populated combo box for a device configuration field."""
            combo = QComboBox()
            combo.setAccessibleName(accessible_name)
            _set_help(combo, f"Select the {accessible_name.lower()} setting.")
            for value in values:
                combo.addItem(value, value)
            return combo

        def _emit_apply(self) -> None:
            """Emit the current widget values for application to the UPS."""
            self.apply_requested.emit(self.current_values())

        def set_enabled(self, enabled: bool) -> None:
            """Enable or disable all configuration widgets together."""
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
            """Collect current widget values into a transport-friendly mapping."""
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
            """Populate widgets from device-derived configuration values."""
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
            """Create panels, wire their signals, and load persisted configuration."""
            super().__init__()
            self.setWindowTitle("Juicer — Furman F1500-UPS E Controller")
            self._resize_to_content_on_show = True

            # State
            self._transport: Any = None
            self._client: Any = None
            self._config = GlobalConfig()
            self._busy = False
            self._workers: list[tuple[_SerialWorker, QThread, _WorkerCallbacks]] = []

            # Central widget with tabs + bottom button row
            central = QWidget()
            central_layout = QVBoxLayout(central)
            central_layout.setContentsMargins(4, 4, 4, 4)
            central_layout.setSpacing(4)
            self.setCentralWidget(central)

            # Tab widget
            self.tabs = QTabWidget()
            self.tabs.setAccessibleName("Main Tab Navigation")
            _set_help(self.tabs, "Choose which Juicer controls to display.")
            central_layout.addWidget(self.tabs)

            # Bottom button row
            btn_row = QHBoxLayout()
            btn_row.setContentsMargins(0, 0, 0, 0)

            self.btn_save = QPushButton("Sa&ve Settings")
            self.btn_save.setAccessibleName("Save Settings")
            _set_help(self.btn_save, "Save the current configuration settings.")
            self.btn_save.clicked.connect(self._on_save_settings)
            btn_row.addWidget(self.btn_save)

            self.btn_reset = QPushButton("Reset to Defaults")
            self.btn_reset.setAccessibleName("Reset to Defaults")
            _set_help(self.btn_reset, "Reset the configuration fields to their default values.")
            self.btn_reset.clicked.connect(self._on_reset_defaults)
            btn_row.addWidget(self.btn_reset)

            btn_row.addStretch()

            self.btn_close = QPushButton("Clos&e")
            self.btn_close.setAccessibleName("Close Window")
            _set_help(self.btn_close, "Close the Juicer GUI.")
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
            _set_help(self.status_bar, "Shows the latest Juicer GUI status message.")
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

            self._fill_missing_tooltips()

            # Load default config
            self._load_config()
            self._connect_to_saved_port_if_available()

        def _fill_missing_tooltips(self) -> None:
            """Use accessible names as fallback tooltip help for any remaining controls."""
            for widget in self.findChildren(QWidget):
                if not widget.toolTip():
                    accessible_name = widget.accessibleName()
                    if accessible_name:
                        _set_help(widget, accessible_name)

        def showEvent(self, event: Any) -> None:
            """Resize the main window to its content the first time it is shown."""
            super().showEvent(event)
            if not self._resize_to_content_on_show:
                return
            self._resize_to_content_on_show = False
            self.adjustSize()
            screen = self.screen() or QApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                self.resize(
                    min(self.width(), available.width()),
                    min(self.height(), available.height()),
                )

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
            """Synchronize busy-state enablement across connection-dependent widgets."""
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
            """Run a blocking device operation in a short-lived Qt worker thread.

            The callable must not touch widgets. Results and errors are delivered
            back to the main thread through Qt signals, where the supplied
            callbacks update UI state. The worker and thread are cleaned up after
            either signal is handled.
            """
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
                """Tear down worker objects after either completion callback fires."""
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
                callbacks.deleteLater()

            callbacks = _WorkerCallbacks(success, error, cleanup, self)
            thread.started.connect(worker.run)
            worker.finished.connect(callbacks.on_finished, Qt.ConnectionType.QueuedConnection)
            worker.error.connect(callbacks.on_error, Qt.ConnectionType.QueuedConnection)
            self._workers.append((worker, thread, callbacks))
            thread.start()

        def _collect_status(self, client: Any) -> dict[str, str]:
            """Gather best-effort status text for every overview and status-panel field."""
            from juicer.protocol import UnsupportedCommandError

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
            except UnsupportedCommandError:
                status["battery_state"] = "Unsupported by device"
            except Exception as exc:
                status["battery_state"] = f"Unavailable ({exc})"
            try:
                backup = client.query_backup_time()
                status["backup_time"] = f"{backup.minutes} minutes"
            except UnsupportedCommandError:
                status["backup_time"] = "Unsupported by device"
            except Exception as exc:
                status["backup_time"] = f"Unavailable ({exc})"
            return status

        def _apply_status_result(self, status: dict[str, str]) -> None:
            """Apply a collected status snapshot to the overview and detail panels."""
            for bank in range(1, NUM_BANKS + 1):
                value = status.get(f"bank{bank}")
                if value is not None:
                    self.overview.set_bank_state(bank, value)
            if "outlets_error" in status:
                logger.warning("Could not read outlet status: %s", status["outlets_error"])
                for bank in range(1, NUM_BANKS + 1):
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
                """Perform blocking serial connection setup in a worker thread."""
                transport = SerialTransport(port=port)
                transport.open()
                client = JuicerClient(transport)
                # Establish known feedback/line-feed environment so subsequent
                # status queries parse deterministically; failures here are
                # logged but do not prevent connection.
                client.initialize()
                status = self._collect_status(client)
                return transport, client, status

            def success(result: object) -> None:
                """Persist the live client objects and update the UI after connection."""
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
                """Report a connection failure raised by the worker thread."""
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
                """Fetch a fresh status snapshot using the active client."""
                return self._collect_status(self._client)

            def success(result: object) -> None:
                """Update status widgets after a successful refresh."""
                status = cast(dict[str, str], result)
                self._apply_status_result(status)
                self.status_bar.showMessage("Status refreshed")

            def error(message: str) -> None:
                """Surface refresh failures without dropping the active connection."""
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
                """Send the requested manual command and then reload device status."""
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
                """Refresh visible state after a manual command succeeds."""
                self._apply_status_result(cast(dict[str, str], result))
                self.status_bar.showMessage(f"Command sent: {command}")

            def error(message: str) -> None:
                """Display a manual command failure raised by the worker thread."""
                logger.error("Command failed: %s", message)
                self.status_bar.showMessage("Command failed")
                QMessageBox.warning(self, "Command Error", message)

            self._run_worker(execute, success, error, busy_message=f"Sending {command}…")

        @Slot()
        def _load_device_config(self) -> None:
            """Load configurable device settings from the connected UPS."""
            if not self._client:
                QMessageBox.warning(self, "Error", "Not connected to serial port")
                return

            def load() -> object:
                """Query the UPS and normalize the response for the config panel."""
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
                if cfg.bthresh3 is not None:
                    values["bthresh3"] = cfg.bthresh3
                elif cfg.bthresh is not None:
                    values["bthresh3"] = cfg.bthresh
                if cfg.bthresh4 is not None:
                    values["bthresh4"] = cfg.bthresh4
                elif cfg.bthresh is not None:
                    values["bthresh4"] = cfg.bthresh
                return values

            def success(result: object) -> None:
                """Populate device config widgets after a successful query."""
                self.device_config.apply_values(cast(dict[str, object], result))
                self.status_bar.showMessage("Device configuration loaded")

            def error(message: str) -> None:
                """Report a device configuration load failure."""
                logger.error("Device config load failed: %s", message)
                QMessageBox.warning(self, "Device Config", f"Load failed: {message}")

            self._run_worker(load, success, error, busy_message="Loading device configuration…")

        @Slot(object)
        def _apply_device_config(self, values_obj: object) -> None:
            """Push edited device configuration values to the connected UPS."""
            if not self._client:
                QMessageBox.warning(self, "Error", "Not connected to serial port")
                return
            values = cast(dict[str, object], values_obj)

            def apply() -> object:
                """Send each config-setting command and then reload status text."""
                from juicer.protocol import UnsupportedCommandError

                normalvolt_skipped = False
                self._client.set_buzzer(cast(str, values["buzzer"]))
                self._client.set_avr(cast(str, values["avr"]))
                self._client.set_feedback(cast(str, values["feedback"]))
                self._client.set_linefeed(cast(str, values["linefeed"]))
                self._client.set_bright(cast(str, values["brightness"]))
                self._client.set_scrollmode(cast(str, values["scroll_mode"]))
                self._client.set_sleepmode(cast(str, values["sleep_mode"]))
                try:
                    self._client.set_normalvolt(cast(str, values["normalvolt"]))
                except UnsupportedCommandError:
                    normalvolt_skipped = True
                    logger.info("Skipping unsupported !SET_NORMALVOLT")
                self._client.set_batthresh(3, cast(int, values["bthresh3"]))
                self._client.set_batthresh(4, cast(int, values["bthresh4"]))
                notice = (
                    "Normal voltage unsupported by device; skipped"
                    if normalvolt_skipped
                    else None
                )
                return self._collect_status(self._client), notice

            def success(result: object) -> None:
                """Refresh status panels after device settings are applied."""
                status, notice = cast(tuple[dict[str, str], str | None], result)
                self._apply_status_result(status)
                self.status_bar.showMessage(notice or "Device configuration applied")

            def error(message: str) -> None:
                """Report a failure while applying configuration to the UPS."""
                logger.error("Device config apply failed: %s", message)
                QMessageBox.warning(self, "Device Config", f"Apply failed: {message}")

            self._run_worker(apply, success, error, busy_message="Applying device configuration…")

        @Slot()
        def _reset_device_config(self) -> None:
            """Request a factory reset of device-side configuration after confirmation."""
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
                """Issue the factory reset command and then collect fresh status."""
                self._client.reset_all()
                return self._collect_status(self._client)

            def success(result: object) -> None:
                """Refresh status panels after a successful factory reset."""
                self._apply_status_result(cast(dict[str, str], result))
                self.status_bar.showMessage("Device reset complete")

            def error(message: str) -> None:
                """Report a factory reset failure raised by the worker thread."""
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
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
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
