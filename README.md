# Juicer

**Juicer** is a Python controller for the **Furman F1500-UPS E**, a rack-mounted
UPS with four individually switched outlet banks that are controlled over
RS-232.

The repository contains:

- a **CLI** for one-shot control and scripting,
- a **GUI** for interactive operation and configuration,
- a **Windows service** that runs configured boot and shutdown sequences, and
- a complete **protocol/configuration/test suite** that can be developed without
  a physical UPS attached.

The project is intentionally split so that protocol parsing, command building,
configuration serialization, and sequence execution remain testable in
isolation. Actual hardware access is limited to the serial transport layer.

---

## Table of Contents

1. [What Juicer Does](#what-juicer-does)
2. [Requirements](#requirements)
3. [Repository Tour](#repository-tour)
4. [Installation](#installation)
5. [CLI Reference](#cli-reference)
6. [GUI Reference](#gui-reference)
7. [Configuration](#configuration)
8. [Sequence Execution Model](#sequence-execution-model)
9. [Windows Service](#windows-service)
10. [Protocol and Device Model](#protocol-and-device-model)
11. [Development](#development)
12. [Troubleshooting](#troubleshooting)
13. [License](#license)

---

## What Juicer Does

Juicer is built around the operating model of the Furman unit:

- The UPS exposes **four controllable outlet banks**.
- Commands and queries are sent as **CR-terminated ASCII strings** over a serial
  connection.
- Device state includes outlet status, battery state, mains state, power/load
  measurements, and several user-configurable settings.
- Boot and shutdown behavior is usually a **timed sequence** rather than a
  single command, because downstream equipment often needs controlled delays.

Juicer therefore provides four major workflows:

1. **Manual control** of outlets and device settings.
2. **Status inspection** for outlet, power, and battery information.
3. **Persistent configuration** in TOML for repeatable operation.
4. **Automated startup/shutdown orchestration** through the Windows service.

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Python | >= 3.11 |
| click | >= 8.1 |
| pydantic | >= 2.0 |
| pyserial | >= 3.5 |
| PySide6 *(GUI extra only)* | >= 6.6 |
| pywin32 *(Windows service only)* | >= 306 |

A Furman F1500-UPS E connected with a null-modem RS-232 cable is required for
real device control. The codebase, however, is structured so that protocol
logic, config serialization, and sequence behavior can all be developed and
tested without hardware.

---

## Repository Tour

### Top-level layout

```text
juicer/
├── src/juicer/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── gui.py
│   ├── protocol.py
│   ├── sequence.py
│   └── service.py
├── tests/
├── firmware/
├── scripts/
├── manual.pdf
├── manual.txt
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

### Source modules

#### `src/juicer/protocol.py`

Implements the Furman serial protocol:

- typed enums for protocol values,
- command builder functions,
- line parsing,
- serial and fake transports, and
- `JuicerClient`, the high-level blocking client API.

This is the most protocol-dense module in the repository and is written so the
transport layer can be swapped out in tests.

#### `src/juicer/config.py`

Defines the persistent TOML-backed configuration model:

- `GlobalConfig` for top-level settings,
- `SequenceConfig` and `BankConfig` for boot/shutdown behavior,
- `TomlStore` for load/save operations, and
- serialization helpers that preserve Juicer's explicit TOML layout.

#### `src/juicer/sequence.py`

Runs boot and shutdown sequences using:

- a configured bank order,
- optional start/stop sounds,
- injected sleeper/cancellation/progress callbacks, and
- a minimal switch-capable client protocol.

The sequence runner is independent of the GUI and Windows service so both can
reuse the same orchestration logic.

#### `src/juicer/cli.py`

Defines the `juicer` Click application. It exposes commands for:

- serial port discovery,
- outlet status and switching,
- sequence execution,
- config import/export, and
- Windows service management.

#### `src/juicer/gui.py`

Provides the full PySide6 desktop application. The GUI is intentionally built
on top of the same `config`, `protocol`, `sequence`, and `service` modules used
elsewhere rather than duplicating serial logic inside the interface.

Notable design choices:

- blocking serial work is moved off the main thread through short-lived worker
  threads,
- controls are heavily labeled for accessibility,
- the current configuration is loaded on startup and saved on exit, and
- the GUI reflects device state by re-querying the UPS after commands.

#### `src/juicer/service.py`

Wraps pywin32 service functionality and mirrors the original service lifecycle:

- run the configured **boot** sequence during service start,
- keep startup synchronous until boot completes,
- wait for stop/shutdown events,
- run the configured **shutdown** sequence during stop/shutdown.

The module is importable on non-Windows systems; Windows-only actions fail at
runtime with descriptive errors instead of breaking imports.

### Tests

The tests are grouped by behavior:

- `tests/test_protocol.py` verifies command builders, response parsing, and
  transport/client behavior.
- `tests/test_sequence.py` verifies sequence ordering, delays, and progress
  reporting.
- `tests/test_service.py` verifies service lifecycle behavior and Windows
  service helper logic using mocked pywin32 components.

### Firmware and manual references

- `manual.pdf` / `manual.txt` contain the Furman device manual used as the
  protocol reference.
- `firmware/` contains emulator-related material that helps validate protocol
  assumptions without real hardware.

---

## Installation

Juicer is designed to install cleanly from source.

### Recommended: install with `uv`

```bash
git clone https://github.com/lllucius/juicer.git
cd juicer

# Runtime package
./scripts/install-uv.sh

# Runtime package + GUI dependencies
./scripts/install-uv.sh --gui

# Developer install
./scripts/install-uv.sh --dev

# Developer install with GUI
./scripts/install-uv.sh --dev --gui
```

On Windows PowerShell:

```powershell
pwsh -File scripts/install-uv.ps1 -Windows
pwsh -File scripts/install-uv.ps1 -Dev -Windows
```

If PowerShell blocks the installer after download or clone:

```powershell
Unblock-File scripts/install-uv.ps1
```

### Manual `uv` workflow

```bash
uv venv
uv pip install --python .venv/bin/python .
uv pip install --python .venv/bin/python ".[gui]"
uv pip install --python .venv/bin/python ".[windows]"
uv pip install --python .venv/bin/python ".[dev]"
```

On Windows, use `.venv\Scripts\python.exe` in the `--python` argument.

### Activate the environment

POSIX shells:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### Smoke-test the install

```bash
juicer --help
python -m juicer --help
```

If PySide6 is installed:

```bash
juicer-gui
```

---

## CLI Reference

After installation, the primary command is `juicer`.

### Global usage

```text
juicer [--verbose] <command>
juicer --version
juicer --help
```

`--verbose` enables debug logging to stderr.

### `juicer ports`

Lists available serial ports reported by pyserial.

```bash
juicer ports
```

Typical use:

- confirm the UPS cable is visible to the OS,
- copy the exact port identifier into config or GUI settings,
- distinguish between multiple serial adapters.

### `juicer status --port <PORT>`

Queries the UPS for:

- outlet bank states,
- mains power status, and
- battery percentage.

```bash
juicer status --port COM3
juicer status --port /dev/ttyUSB0
```

This command is a good first validation step when bringing a new machine or
cable online.

### `juicer all-on` / `juicer all-off`

Bulk-controls all outlet banks.

```bash
juicer all-on --port COM3
juicer all-off --port COM3
```

These commands print the parsed responses returned by the device and then a
human-readable summary message.

### `juicer switch --port <PORT> --bank <1-4> --state <on|off>`

Controls a single outlet bank.

```bash
juicer switch --port COM3 --bank 2 --state on
juicer switch --port COM3 --bank 4 --state off
```

Bank-specific switching is particularly useful when sequenced startup is not
required and you simply need to power-cycle one attached component.

### `juicer boot` / `juicer shutdown`

Runs the configured sequence from the persisted TOML file.

```bash
juicer boot
juicer shutdown
```

These commands do **not** ask for a `--port` argument because the serial port is
read from configuration.

### `juicer config`

Manage the persistent TOML configuration.

```bash
juicer config show
juicer config export config.toml
juicer config import config.toml
```

- `show` prints the current persisted config.
- `export` writes the current config to a chosen path.
- `import` validates and stores config from a TOML file.

### `juicer service`

Windows-only service management helpers:

```bash
juicer service install
juicer service uninstall
juicer service start
juicer service stop
juicer service status
```

These operations require Windows and pywin32. Install, uninstall, start, stop,
and restart operations may trigger UAC elevation when needed.

---

## GUI Reference

Launch the GUI with either of the following:

```bash
juicer-gui
python -m juicer.gui
```

If PySide6 is not installed, the GUI entry point exits with a helpful error.

### GUI tab overview

| Tab | Purpose |
|-----|---------|
| Overview | Live connection, outlet, mains, and battery summary |
| Manual Control | All-on, all-off, and per-bank switching |
| Boot Sequence | Edit boot bank actions and delays |
| Shutdown Sequence | Edit shutdown bank actions and delays |
| Device Status | Query extended device measurements and identity |
| Device Config | Read/write device-side buzzer, AVR, feedback, display, and threshold settings |
| Service | Install/start/stop the Windows service |
| Import/Export | Move TOML configuration in and out of the app |
| Log | Review application log output inside the GUI |

### Startup behavior

When the GUI opens it:

1. loads the persisted config if one exists,
2. populates the sequence and serial settings editors,
3. attempts to select the saved serial port if it is currently available, and
4. optionally auto-connects to that saved port.

If the saved port is not currently present, the GUI leaves the user in a safe
disconnected state.

### Connection behavior

The GUI does not perform blocking serial work on the main UI thread. Instead it
uses short-lived worker threads for:

- initial connection,
- status refresh,
- manual commands,
- device config loads,
- device config writes, and
- factory reset.

This matters because serial I/O may block, retry, or time out. Without worker
threads, the interface would freeze while device commands were in progress.

### Accessibility and discoverability

The GUI includes:

- accessible names for controls,
- tooltip/status-tip help text,
- explicit labels and label buddies where relevant, and
- a log view that surfaces internal actions and failures.

This is intentional: the app is meant to be operable and debuggable even when
the user is not watching stdout/stderr.

---

## Configuration

Juicer stores configuration as TOML through `TomlStore`.

### Default config path

- **Windows**: `%PROGRAMDATA%\Juicer\config.toml`
- **Other platforms**: `~/.juicer/config.toml`

### Top-level fields

- `port`: serial port name such as `COM3` or `/dev/ttyUSB0`
- `event_start_sound`: optional WAV file played before a sequence starts
- `event_stop_sound`: optional WAV file played after a sequence ends
- `boot`: boot sequence configuration
- `shutdown`: shutdown sequence configuration

### Per-bank fields

Each sequence has four bank entries:

- `action`
  - `0` = turn the bank OFF
  - `1` = turn the bank ON
  - omitted = skip the bank entirely
- `pre_delay_ms`: milliseconds to wait before the action
- `post_delay_ms`: milliseconds to wait after the action

### Example configuration

```toml
port = "COM3"
event_start_sound = ""
event_stop_sound = ""

[boot.bank1]
action = 1
pre_delay_ms = 0
post_delay_ms = 2000

[boot.bank2]
action = 1
pre_delay_ms = 0
post_delay_ms = 2000

[boot.bank3]
action = 1
pre_delay_ms = 0
post_delay_ms = 2000

[boot.bank4]
action = 1
pre_delay_ms = 0
post_delay_ms = 0

[shutdown.bank4]
action = 0
pre_delay_ms = 0
post_delay_ms = 2000

[shutdown.bank3]
action = 0
pre_delay_ms = 0
post_delay_ms = 2000

[shutdown.bank2]
action = 0
pre_delay_ms = 0
post_delay_ms = 2000

[shutdown.bank1]
action = 0
pre_delay_ms = 0
post_delay_ms = 0
```

### Serialization notes

The config writer is intentionally explicit rather than delegating to a generic
TOML dumping library. That keeps the generated file stable and predictable:

- strings are escaped safely,
- bank sections are always emitted in a fixed order,
- missing actions stay omitted rather than being rewritten ambiguously.

That predictability makes exported configs easier to review and compare.

---

## Sequence Execution Model

The sequence runner in `sequence.py` is shared by the CLI and Windows service.

### Bank order

- **Boot order**: `1 -> 2 -> 3 -> 4`
- **Shutdown order**: `4 -> 3 -> 2 -> 1`

### Per-bank behavior

For each bank:

1. skip the bank if no action is configured,
2. wait `pre_delay_ms`,
3. send the bank switch command,
4. wait `post_delay_ms`.

### Long delays and progress reporting

Long waits are intentionally broken into chunks by `_sleep_with_progress()`.
This is a small but important implementation detail:

- the sequence still waits for the full configured delay,
- but long waits are split into intervals,
- and an optional progress callback runs between intervals.

This allows the Windows service to keep reporting startup progress to the
Service Control Manager during lengthy boot sequences instead of appearing hung.

### Cancellation model

Sequence execution optionally accepts a cancellation token. That lets the
service stop sequence processing cleanly when a stop request arrives during
startup.

### Sound playback

Optional event sounds are only played on Windows through `winsound`. On other
platforms the code logs that sound playback was skipped.

---

## Windows Service

The Windows service exists so outlet sequencing can happen automatically during
machine lifecycle events.

### Service identity

- **Service name**: `Juicer`
- **Display name**: `Juicer UPS Controller`

### Lifecycle

- On service start, Juicer loads config and runs the **boot** sequence.
- Startup remains **synchronous** until boot completes.
- Only then does the service report `SERVICE_RUNNING`.
- During shutdown or stop, Juicer runs the configured **shutdown** sequence.

This behavior is deliberate. Many environments need downstream equipment to be
fully powered before dependent services start.

### Why startup stays synchronous

This is one of the more non-obvious pieces of the codebase.

If the service reported `SERVICE_RUNNING` immediately and only then started the
boot sequence in the background, Windows and other dependent software could
assume the UPS-controlled equipment was ready before the outlet banks had
actually been energized. The current implementation avoids that race by keeping
service startup pending while power-up is still in progress.

### Logging

The service writes per-sequence log files next to the config file:

- `service.log`
- `boot.log`
- `shutdown.log`

That makes post-mortem debugging easier on Windows systems where interactive
stdout/stderr is not available.

### Build and deploy the service executable

The Windows service runs from a self-contained PyInstaller executable instead
of `pythonservice.exe`. This avoids service-start failures caused by an
unactivated virtual environment, a missing Python DLL, or missing site-packages
when the Service Control Manager starts the process.

From a Windows PowerShell prompt:

```powershell
pwsh -File scripts/build-service.ps1 -Clean
```

The build output is:

```text
dist\juicer_service.exe
```

The script installs Juicer's Windows extra plus the PyInstaller build tooling
into the selected Python environment before running the build.

To build and copy the executable to `%PROGRAMDATA%\Juicer`:

```powershell
pwsh -File scripts/build-service.ps1 -Clean -Deploy
```

To build, deploy, install, and start the service in one pass:

```powershell
pwsh -File scripts/build-service.ps1 -Clean -Install -Start
```

Run the deploy/install/start command from an elevated shell if Windows blocks
writing to `%PROGRAMDATA%` or service installation. `-Install` and `-Start`
delegate to the same `juicer service install` / `juicer service start` helpers
used by the GUI and CLI.

### Manual service installation

If you prefer to copy the executable yourself:

1. Build `dist\juicer_service.exe`.
2. Copy it to one of the locations Juicer searches:
   - `%PROGRAMDATA%\Juicer\juicer_service.exe`
   - next to the Python interpreter running `juicer service install`
3. Install and start the service:

```powershell
juicer service install
juicer service start
```

`juicer service install` now requires the bundled executable. If it cannot find
`juicer_service.exe`, it exits with instructions to build and deploy it first.

---

## Protocol and Device Model

`protocol.py` implements a typed wrapper around the UPS serial protocol.

### Command style

Commands are emitted as ASCII with a trailing carriage return:

```text
!ALL_ON\r
!SWITCH 2 OFF\r
?OUTLETSTAT\r
```

### Line termination

Response lines are fundamentally **CR-terminated**. A trailing LF may appear
depending on the device's linefeed setting, so the serial transport strips an
optional LF after each CR.

This subtle behavior is important enough to be called out because the
transport's `read_line()` implementation intentionally preserves the first byte
of the next response if the byte after CR is **not** LF. That prevents line
boundary corruption when the device sends CR-only responses back-to-back.

### Real-device response quirks

The real F1500-UPS firmware differs from the printed manual in several places:

- After command output, the device prints a bare `>` character (0x3E, no CR)
  as a shell-style ready prompt. The prompt is not part of the response; the
  Python client raises `PromptReceived` internally when it sees this byte.
- Many responses omit spaces, such as `$BANK1=ON`, `$BUZZER=OFF`, and
  `$BTHRESH3=060`.
- `!ALL_ON` and `!ALL_OFF` also report `$BUTTON=ON`.
- `!SET_FEEDBACK OFF`, `!SET_LINEFEED`, `!SET_BRIGHT`, `!SET_SCROLLMODE`, and
  `!SET_SLEEPMODE` print only the `>` ready prompt with no data line.
- `!SET_LINEFEED ON` confirms as `LINEFEED=ON` without the leading `$`.
- `?VOLTAGE` reports `$VOLTS_IN=<value>`.
- `?LIST_CONFIG` reports bank 3 and bank 4 thresholds separately.
- The manual lists `!SET_NORMALVOLT`, `?BATTSTATE`, and `?TIME`, but the real
  firmware rejects them with `$INVALID_PARAMETER`. The client and GUI do not
  expose these commands.

### High-level client behavior

`JuicerClient` offers blocking methods that:

1. build the correct protocol string,
2. write it through the configured transport,
3. read the expected number or shape of responses,
4. parse those lines into typed models, and
5. raise protocol-specific errors when the response is missing or invalid.

### Fake transport

The in-memory `FakeTransport` exists so tests can queue expected response lines
without opening a real serial device. Call `enqueue_prompt()` to simulate a
real-device `>` ready prompt; `read_line()` will raise `PromptReceived` just as
`SerialTransport` does when it receives the `>` byte. This is a key reason the
protocol module has high test coverage and remains safe to refactor.

---

## Development

### Local development install

```bash
python -m pip install -r requirements-dev.txt
```

Or use the `uv`-based developer install shown earlier.

### Validation commands

From the repository root:

```bash
python -m ruff check src tests
python -m mypy src
python -m pytest
```

### Running from source

```bash
python -m juicer --help
python -m juicer.gui
```

### Capturing raw Furman serial responses

To collect real-device prompt characters and unparsed response bytes, run:

```bash
python scripts/capture_furman_protocol.py --port COM3 --output furman-capture.txt --yes
```

The capture script issues the full command set, including outlet switching and
`!RESET_ALL`, so only run it when it is safe for attached equipment.

### Design principles worth knowing

#### 1. Keep hardware access isolated

Only the transport layer should know about raw serial I/O details. Higher-level
code should work with typed responses and domain concepts.

#### 2. Prefer typed configuration and typed protocol values

Pydantic models and enums are used heavily so invalid config or unexpected
protocol values fail early and explicitly.

#### 3. Keep sequence logic reusable

The CLI, GUI, and service all rely on the same sequence runner rather than
copying boot/shutdown rules into multiple places.

#### 4. Make non-Windows imports safe

Windows-specific features are guarded so the codebase can still be developed and
tested on Linux or macOS.

---

## Troubleshooting

### `pyserial is required`

Install runtime dependencies:

```bash
python -m pip install -r requirements.txt
```

### `PySide6 is required for the GUI`

Install the GUI extra:

```bash
python -m pip install ".[gui]"
```

### `pywin32 is required for service operations`

Install the Windows extra on a Windows machine:

```bash
python -m pip install ".[windows]"
```

### The saved port does not auto-connect

The GUI only auto-connects when the saved port is currently discoverable. If the
USB adapter name changed or the cable is unplugged, the GUI will stay
disconnected instead of guessing.

### Status queries work partially

Some status queries are performed independently. If one query fails, Juicer
still tries to populate the rest of the available information and reports the
failed field as unavailable rather than failing the whole screen.

### Service starts but dependent equipment is not ready quickly

Remember that startup timing is controlled by the configured sequence delays.
Long boot delays are expected to keep the service in `START_PENDING` until the
configured power-up sequence has finished.

---

## License

[MIT](LICENSE) © 2026 Leland Lucius
