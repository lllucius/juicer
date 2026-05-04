# Juicer

**Python controller for the Furman F1500-UPS E** — a rack-mounted UPS with four
individually-switched outlet banks controlled over RS-232.

Juicer provides:

- A **CLI** (`juicer`) for scripting and one-shot commands.
- A **GUI** (`juicer-gui`) built with PySide6 for interactive control.
- A **Windows service** that runs the configured boot/shutdown sequences
  automatically on system start and stop.
- A **configuration system** that stores settings in the Windows registry
  (matching the original C++ layout) or in a portable JSON file.

---

## Table of Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [CLI Usage](#cli-usage)
4. [GUI Usage](#gui-usage)
5. [Configuration](#configuration)
6. [Windows Service](#windows-service)
7. [Building Standalone Executables](#building-standalone-executables)
8. [Development](#development)
9. [License](#license)

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Python      | ≥ 3.11  |
| click       | ≥ 8.1   |
| pydantic    | ≥ 2.0   |
| pyserial    | ≥ 3.5   |
| PySide6 *(GUI extra only)* | ≥ 6.6 |
| pywin32 *(Windows service/registry only)* | ≥ 306 |

A Furman F1500-UPS E connected via a null-modem RS-232 cable is required for
actual device control. All protocol logic works without hardware — you can
build and test the software on any OS.

---

## Installation

### From source

```bash
# Clone the repository
git clone https://github.com/lllucius/juicer.git
cd juicer

# Install core dependencies
pip install -r requirements.txt

# Optional GUI support
pip install ".[gui]"

# Windows: also install Windows-specific dependencies
pip install -r requirements-windows.txt

# Or install the package directly (includes all core deps)
pip install .
```

### Editable / developer install

```bash
pip install -e ".[dev]"
# With GUI support:
pip install -e ".[dev,gui]"
# Windows:
pip install -e ".[dev,windows]"
```

---

## CLI Usage

After installation the `juicer` command is available on `PATH`.
You can also invoke it with `python -m juicer`.

### Global options

```
juicer [--verbose] <command>
juicer --version
juicer --help
```

### Commands

#### `ports` — list available serial ports

```bash
juicer ports
```

#### `status` — query device status

```bash
juicer status --port COM3
```

Prints outlet bank states, mains power status, and battery level.

#### `all-on` / `all-off` — bulk power control

```bash
juicer all-on  --port COM3
juicer all-off --port COM3
```

#### `switch` — control a single bank

```bash
juicer switch --port COM3 --bank 2 --state on
juicer switch --port COM3 --bank 2 --state off
```

Bank numbers are **1–4**.

#### `boot` / `shutdown` — run configured sequences

```bash
juicer boot
juicer shutdown
```

Reads configuration from the Windows registry (Windows) or
`~/.juicer/config.json` (other platforms) and executes the stored
boot or shutdown sequence.

#### `config` — manage configuration

```bash
juicer config show                   # Display current config as JSON
juicer config export config.json     # Export to a file
juicer config import config.json     # Import from a file
```

#### `service` — manage the Windows service *(Windows only)*

```bash
juicer service install    # Install as auto-start service
juicer service uninstall  # Remove the service
juicer service start      # Start the service
juicer service stop       # Stop the service
juicer service status     # Query current status
```

---

## GUI Usage

Launch with:

```bash
juicer-gui
# or
python -m juicer.gui
```

The GUI is organized into tabs:

| Tab | Purpose |
|-----|---------|
| **Overview** | Live bank states and connection status |
| **Serial Settings** | Port selection, connect/disconnect |
| **Manual Controls** | All-on, all-off, per-bank switches |
| **Boot Sequence** | Per-bank action and delay configuration |
| **Shutdown Sequence** | Same for the shutdown sequence |
| **Service** | Install/uninstall/start/stop the Windows service |
| **Import/Export** | Save and load JSON configuration |
| **Log** | Scrolling log of all events |

Configuration is automatically loaded from the Windows registry (Windows) or
`~/.juicer/config.json` on launch, and saved when the window is closed.

---

## Configuration

The Juicer configuration describes:

- **`port`** — serial port name (e.g. `COM3`, `/dev/ttyS0`).
- **`verbose`** — enable debug logging.
- **`start_sound`** / **`stop_sound`** — paths to WAV files played at the
  start and end of a sequence (Windows only).
- **`boot`** / **`shutdown`** — per-sequence, per-bank settings:
  - `action`: `0` = OFF, `1` = ON, absent = skip.
  - `pre_delay_ms`: milliseconds to wait *before* the action.
  - `post_delay_ms`: milliseconds to wait *after* the action.

### JSON example

```json
{
  "port": "COM3",
  "verbose": false,
  "start_sound": "",
  "stop_sound": "",
  "boot": {
    "bank1": {"action": 1, "pre_delay_ms": 0,    "post_delay_ms": 2000},
    "bank2": {"action": 1, "pre_delay_ms": 0,    "post_delay_ms": 2000},
    "bank3": {"action": 1, "pre_delay_ms": 0,    "post_delay_ms": 2000},
    "bank4": {"action": 1, "pre_delay_ms": 0,    "post_delay_ms": 0}
  },
  "shutdown": {
    "bank4": {"action": 0, "pre_delay_ms": 0,    "post_delay_ms": 2000},
    "bank3": {"action": 0, "pre_delay_ms": 0,    "post_delay_ms": 2000},
    "bank2": {"action": 0, "pre_delay_ms": 0,    "post_delay_ms": 2000},
    "bank1": {"action": 0, "pre_delay_ms": 0,    "post_delay_ms": 0}
  }
}
```

### Windows registry layout

Settings are stored under:

```
HKLM\System\CurrentControlSet\Services\juicer\boot\
HKLM\System\CurrentControlSet\Services\juicer\shutdown\
```

Use `juicer config export config.json` to back up the registry settings to a
portable JSON file, and `juicer config import config.json` to restore them.

---

## Windows Service

The Juicer Windows service (`Juicer` / *Juicer UPS Controller*) runs
automatically at system start:

- **On start** — executes the boot sequence (banks 1 → 4).
- **On stop / system shutdown** — executes the shutdown sequence (banks 4 → 1).

Startup remains synchronous by design: the service reports `SERVICE_RUNNING`
only after the configured boot sequence has finished, so dependent services do
not start before outlet power is ready. During long boot work, Juicer
periodically refreshes `SERVICE_START_PENDING` with the Windows Service Control
Manager.

### Install and start

```bat
juicer service install
juicer service start
```

> **Note**: service installation requires an **elevated (Administrator)**
> command prompt.

---

## Building Standalone Executables

Standalone `.exe` files are built with [Nuitka](https://nuitka.net).

### Prerequisites

Install the development dependencies (includes Nuitka) and the package itself:

```bat
pip install -r requirements-dev.txt -r requirements-windows.txt
pip install -e .
```

### Build all executables at once

```bat
packaging\build.bat
```

### Build individually

```bat
set PYTHONPATH=src
python -m nuitka ^
    --onefile ^
    --output-dir=dist ^
    --output-filename=juicer.exe ^
    --include-package=juicer ^
    --nofollow-import-to=PySide6 ^
    --nofollow-import-to=tkinter ^
    --assume-yes-for-downloads ^
    src\juicer\cli.py
python -m nuitka ^
    --onefile ^
    --output-dir=dist ^
    --output-filename=juicer-gui.exe ^
    --enable-plugin=pyside6 ^
    --include-package=juicer ^
    --nofollow-import-to=tkinter ^
    --windows-console-mode=disable ^
    --assume-yes-for-downloads ^
    src\juicer\gui.py
python -m nuitka ^
    --onefile ^
    --output-dir=dist ^
    --output-filename=juicer-svc.exe ^
    --include-package=juicer ^
    --nofollow-import-to=PySide6 ^
    --nofollow-import-to=tkinter ^
    --windows-console-mode=disable ^
    --assume-yes-for-downloads ^
    src\juicer\service.py
```

### Output

| File | Description |
|------|-------------|
| `dist\juicer.exe` | CLI — run in a Command Prompt |
| `dist\juicer-gui.exe` | GUI — double-click to launch |
| `dist\juicer-svc.exe` | Windows service executable |

> **Tip**: All executables are self-contained — no Python
> installation is needed on the target machine.

---

## Development

### Project layout

```
juicer/
├── src/
│   └── juicer/
│       ├── __init__.py     # Package version
│       ├── __main__.py     # python -m juicer entry point
│       ├── cli.py          # Click CLI
│       ├── config.py       # Pydantic models + registry/JSON stores
│       ├── gui.py          # PySide6 GUI
│       ├── protocol.py     # Serial protocol (commands, responses, transport)
│       ├── sequence.py     # Boot/shutdown sequencer
│       └── service.py      # Windows service wrapper
├── packaging/
│   └── build.bat           # Windows build script
├── pyproject.toml
├── requirements.txt
├── requirements-windows.txt
├── requirements-dev.txt
├── LICENSE
└── README.md
```

### Running from source

```bash
# CLI
python -m juicer --help

# GUI
python -m juicer.gui
```

### Linting

```bash
ruff check src/
```

### Type checking

```bash
mypy src/
```

### Tests

```bash
pytest
```

---

## License

[MIT](LICENSE) © 2026 Leland Lucius
