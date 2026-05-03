# Furman F1500-UPS E Emulator — ESP32 Firmware

An ESP32 Arduino sketch that emulates the RS-232 serial protocol of the
**Furman F1500-UPS E** UPS.  Use it to test the Juicer CLI, GUI, and
Windows service without real hardware.

---

## Requirements

| Tool | Notes |
|------|-------|
| ESP32 dev board | Any board based on the ESP32, ESP32-S2, ESP32-S3, or ESP32-C3 |
| [Arduino IDE 2.x](https://www.arduino.cc/en/software) **or** [PlatformIO](https://platformio.org/) | Either toolchain works |
| [arduino-esp32 core](https://github.com/espressif/arduino-esp32) | ≥ 2.0 (Arduino IDE board manager) |

---

## Flashing

### Arduino IDE

1. Open **`furman_emulator.ino`** in Arduino IDE.
2. Select your board under **Tools → Board → esp32 → ESP32 Dev Module**
   (or whichever variant you have).
3. Select the correct **Tools → Port**.
4. Click **Upload** (Ctrl+U).

### PlatformIO (CLI or VS Code extension)

```bash
cd firmware/furman_emulator
pio run --target upload
```

The `platformio.ini` targets `esp32dev`.  Change the `board` value if you
have a different board (e.g. `esp32-s3-devkitc-1`).

---

## Connecting to Juicer

After flashing, the ESP32 appears as a virtual COM port:

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
| Backup time | 30 min |
| Power status | NORMAL |

Sensor values are compile-time constants (`s_*` variables at the top of the
sketch).  Edit them and re-flash to test different conditions.

---

## Notes

* **Baud rate** — Juicer's `SerialTransport` uses 9600 baud / 8-N-1 by
  default.  USB-CDC virtual ports ignore the baud-rate setting from the host,
  so the sketch works at any speed over USB.  If you wire the ESP32's hardware
  UART through an RS-232 level-shifter instead, set both sides to 9600 baud.

* **Hardware UART** — To use `Serial1` or `Serial2` instead of the USB
  serial port, replace `Serial` with `Serial1` (or `Serial2`) throughout the
  sketch and remove the `while (!Serial)` wait in `setup()`.

* **Line ending** — The Furman protocol terminates lines with CR only
  (`\r`, 0x0D).  The emulator honours `!SET_LINEFEED ON` by appending LF as
  well, matching real-device behaviour.
