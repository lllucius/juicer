# Furman F1500-UPS E Emulator — ESP32 Firmware (ESP-IDF v6.0)

An ESP-IDF v6.0 application that emulates the RS-232 serial protocol of the
**Furman F1500-UPS E** UPS.  Use it to test the Juicer CLI, GUI, and
Windows service without real hardware.

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| ESP32 dev board | Any board with a USB-UART bridge (CP2102, CH340, …) and an ESP32, ESP32-S2, ESP32-S3, or ESP32-C3 SoC |
| [ESP-IDF **v6.0**](https://github.com/espressif/esp-idf) | Follow the [official installation guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/index.html) |

---

## Project layout

```
firmware/furman_emulator/
├── CMakeLists.txt        — top-level ESP-IDF project file
├── sdkconfig.defaults    — disables IDF console so UART0 stays clean
├── main/
│   ├── CMakeLists.txt    — component registration
│   └── main.c            — complete protocol emulator
└── README.md
```

---

## Build and flash

```bash
# 1. Activate the ESP-IDF v6.0 environment (adjust path to your install)
. $HOME/esp/esp-idf/export.sh

# 2. Enter the project directory
cd firmware/furman_emulator

# 3. (Optional) choose a non-default target, e.g. for ESP32-S3:
#    idf.py set-target esp32s3
#    The default target is esp32.

# 4. Build
idf.py build

# 5. Flash  (replace /dev/ttyUSB0 with your port)
idf.py -p /dev/ttyUSB0 flash
```

> **Note:** `idf.py monitor` will show no output because
> `sdkconfig.defaults` sets `CONFIG_ESP_CONSOLE_NONE=y`, which keeps UART0
> free for the Furman protocol.  Use a separate terminal / serial monitor
> at 9600 baud if you want to observe the raw traffic.

### Windows

```bat
:: Activate ESP-IDF (adjust to your installation path)
%USERPROFILE%\esp\esp-idf\export.bat

cd firmware\furman_emulator
idf.py build
idf.py -p COM3 flash
```

---

## Connecting to Juicer

After flashing, the ESP32 appears as a virtual COM port through its
on-board USB-UART bridge:

| OS | Device name |
|----|------------|
| Windows | `COMx` (check Device Manager) |
| Linux | `/dev/ttyUSB0` or `/dev/ttyACM0` |
| macOS | `/dev/cu.usbserial-*` or `/dev/cu.usbmodem*` |

Point Juicer at that port:

```bash
# Check status
juicer status --port /dev/ttyUSB0

# Turn all banks on
juicer all-on --port /dev/ttyUSB0

# Switch bank 2 off
juicer switch --port /dev/ttyUSB0 --bank 2 --state off

# Open the GUI
juicer-gui
# then select the port in the Serial Settings tab
```

---

## Supported commands

### Action commands (`!`)

| Command | Description |
|---------|-------------|
| `!ALL_ON` | Turn all four outlet banks ON |
| `!ALL_OFF` | Turn all four outlet banks OFF |
| `!SWITCH <bank> <ON\|OFF>` | Switch a single bank (1–4) |
| `!SET_BATTHRESH <bank> <level>` | Set battery threshold for bank 3 or 4 (20–100, rounded to nearest 10) |
| `!SET_BUZZER <ON\|OFF>` | Enable / disable buzzer |
| `!SET_AVR <OFF\|STANDARD\|SENSITIVE>` | Set AVR mode |
| `!SET_FEEDBACK <ON\|OFF>` | Enable / disable command echo |
| `!SET_LINEFEED <ON\|OFF>` | Append LF after each CR in responses |
| `!SET_BRIGHT <100\|075\|050\|025>` | Set display brightness |
| `!SET_SCROLLMODE <5SEC\|10SEC\|OFF>` | Set display scroll mode |
| `!SET_SLEEPMODE <30SEC\|60SEC\|OFF>` | Set display sleep mode |
| `!RESET_ALL` | Restore factory defaults |
| `!SET_NORMALVOLT <220\|230\|240>` | Set nominal mains voltage |

### Query commands (`?`)

| Command | Response |
|---------|----------|
| `?ID` | Three lines: manufacturer, model, firmware |
| `?OUTLETSTAT` | Four `$BANK n = ON\|OFF` lines |
| `?POWERSTAT` | `$PWR = NORMAL` |
| `?POWER` | `$VOLTS_IN`, `$VOLTS_OUT`, `$WATTS`, `$CURRENT` |
| `?CURRENT` | `$CURRENT = <amps>` |
| `?VOLTAGE` | `$VOLTAGE = <volts>` |
| `?LOADSTAT` | `$LOAD = <percent>` |
| `?BATTERYSTAT` | `$BATTERY = <percent>` |
| `?LIST_CONFIG` | All configuration settings |
| `?HELP` | List of all supported commands |

Any unrecognised command returns `$INVALID_PARAMETER`.

---

## Simulated sensor values

The emulator returns fixed, plausible readings for sensor queries:

| Metric | Value |
|--------|-------|
| Volts in | 230.0 V |
| Volts out | 230.0 V |
| Load watts | 150.0 W |
| Current | 0.65 A |
| Voltage | 230.0 V |
| Load | 10.0 % |
| Battery | 85 % |
| Power status | NORMAL |

Sensor values are compile-time constants (`s_*` variables near the top of
`main/main.c`).  Edit them and re-flash to test different conditions.

---

## Notes

* **UART0 / USB-UART bridge** — `main.c` uses UART0 (GPIO1/TX, GPIO3/RX),
  the UART wired to the on-board USB-UART bridge on all standard dev boards.
  `sdkconfig.defaults` sets `CONFIG_ESP_CONSOLE_NONE=y` so the IDF boot
  messages and log output do not appear on UART0 and corrupt the protocol.

* **Using a different UART** — Change `#define UART_PORT UART_NUM_0` to
  `UART_NUM_1` (or `UART_NUM_2`) and update `uart_set_pin()` with the
  desired TX/RX GPIO numbers.  Remove `CONFIG_ESP_CONSOLE_NONE=y` from
  `sdkconfig.defaults` if you want the IDF console back on UART0.

* **Using a different board target** — Run `idf.py set-target <target>`
  (e.g. `esp32s3`, `esp32c3`) before `idf.py build`.  The emulator logic is
  target-agnostic; only `LED_GPIO` may need adjusting for your board.

* **LED pin** — The ready-blink uses GPIO2 (`LED_GPIO` in `main.c`), which
  is the built-in LED on most ESP32-DevKitC boards.  Change it if your board
  uses a different pin (e.g. GPIO8 on ESP32-C3-DevKitM-1).

* **Baud rate** — Juicer's `SerialTransport` uses 9600 baud / 8-N-1 by
  default, matching `UART_BAUD` in `main.c`.  The USB-UART bridge presents a
  virtual COM port to the host; the host-side baud rate setting is passed
  through transparently.

* **Line ending** — The Furman protocol terminates lines with CR only
  (`\r`, 0x0D).  The emulator honours `!SET_LINEFEED ON` by appending LF as
  well, matching real-device behaviour.
