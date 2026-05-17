"""Juicer serial protocol — commands, responses, transport, and client.

Implements the complete Furman F1500-UPS E RS-232 protocol from manual.txt:
13 commands, 10 queries, all response families, plus transport abstraction.

All command construction and response parsing works without a real serial port.
"""

from __future__ import annotations

import abc
import enum
import functools
import logging
import re
import time
from collections import deque
from typing import Any, Callable, Sequence, TypeVar

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────


class JuicerError(Exception):
    """Base exception for all Juicer errors."""


class ProtocolError(JuicerError):
    """Invalid or unexpected data on the wire."""


class JuicerTimeoutError(JuicerError):
    """No response within deadline."""


class TransportError(JuicerError):
    """Low-level transport failure (port open, read, write)."""


class ValidationError(JuicerError):
    """Command parameter validation failed."""


class PromptReceived(JuicerError):
    """Device sent the ``>`` prompt character, signalling readiness.

    Raised by :meth:`SerialTransport.read_line` (and optionally by
    :meth:`FakeTransport.read_line`) when the ``>`` byte (0x3E) is the
    first character of an incoming line.  Client helpers treat this the
    same as :class:`JuicerTimeoutError` for variable-length reads because
    the shell-style prompt means the device is ready for another command.
    """


# ──────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────


class BankNumber(enum.IntEnum):
    """Outlet bank numbers (1-indexed)."""

    BANK1 = 1
    BANK2 = 2
    BANK3 = 3
    BANK4 = 4


class BankState(str, enum.Enum):
    """Outlet bank on/off state."""

    ON = "ON"
    OFF = "OFF"


class PowerStatus(str, enum.Enum):
    """Mains power status reported by UPS."""

    NORMAL = "NORMAL"
    OVERVOLTAGE = "OVERVOLTAGE"
    UNDERVOLTAGE = "UNDERVOLTAGE"
    LOST_POWER = "LOST POWER"
    TEST = "TEST"
    RECOVERY = "RECOVERY"


class BuzzerMode(str, enum.Enum):
    """Audible alarm enable/disable setting."""

    ON = "ON"
    OFF = "OFF"


class AVRMode(str, enum.Enum):
    """Automatic voltage regulation sensitivity modes."""

    OFF = "OFF"
    STANDARD = "STANDARD"
    SENSITIVE = "SENSITIVE"


class FeedbackMode(str, enum.Enum):
    """Device command feedback verbosity mode."""

    ON = "ON"
    OFF = "OFF"


class LinefeedMode(str, enum.Enum):
    """Whether responses append an LF after the required CR terminator."""

    ON = "ON"
    OFF = "OFF"


class Brightness(str, enum.Enum):
    """Front-panel display brightness levels expressed as protocol strings."""

    B100 = "100"
    B075 = "075"
    B050 = "050"
    B025 = "025"


class ScrollMode(str, enum.Enum):
    """Front-panel information scroll timing."""

    SEC5 = "5SEC"
    SEC10 = "10SEC"
    OFF = "OFF"


class SleepMode(str, enum.Enum):
    """Front-panel display sleep timing."""

    SEC30 = "30SEC"
    SEC60 = "60SEC"
    OFF = "OFF"


class NormalVolt(str, enum.Enum):
    """Nominal mains voltage configuration reported by the UPS."""

    V220 = "220"
    V230 = "230"
    V240 = "240"


class AVRState(str, enum.Enum):
    """Active AVR correction direction when regulation is engaged."""

    BOOST = "BOOST"
    BUCK = "BUCK"


class BatteryChargeState(str, enum.Enum):
    """Current battery charging direction or fully charged state."""

    CHARGE = "CHARGE"
    DISCHARGE = "DISCHARGE"
    FULL = "FULL"


class ButtonState(str, enum.Enum):
    """Front-panel power button enabled/disabled state."""

    ON = "ON"
    OFF = "OFF"


class BatteryThreshold(enum.IntEnum):
    """Valid battery threshold levels (UPS rounds up to nearest 10)."""

    T20 = 20
    T30 = 30
    T40 = 40
    T50 = 50
    T60 = 60
    T70 = 70
    T80 = 80
    T90 = 90
    T100 = 100


# ──────────────────────────────────────────────────────────────────────
# Response Models (Pydantic)
# ──────────────────────────────────────────────────────────────────────


class BankStatusResponse(BaseModel):
    """``$BANK <n> = <ON|OFF>`` or ``$BANK<n> = <ON|OFF>``."""

    bank: BankNumber
    state: BankState


class ButtonResponse(BaseModel):
    """``$BUTTON = <ON|OFF>``."""

    state: ButtonState


class PowerStatusResponse(BaseModel):
    """``$PWR = <status>``."""

    status: PowerStatus


class BatteryLevelResponse(BaseModel):
    """``$BATTERY = <charge%>``."""

    level: int = Field(ge=0, le=100)


class BatteryThresholdResponse(BaseModel):
    """``$BTHRESH <bank> = <level>`` or ``$BTHRESH<bank>=<level>``."""

    bank: BankNumber
    level: int


class BatteryThresholdGlobalResponse(BaseModel):
    """``$BTHRESH = <level>`` from ``?LIST_CONFIG``."""

    level: int


class BuzzerResponse(BaseModel):
    """``$BUZZER = <mode>``."""

    mode: BuzzerMode


class AVRModeResponse(BaseModel):
    """``$AVR = <mode>``."""

    mode: AVRMode


class FeedbackResponse(BaseModel):
    """``$FEEDBACK = <mode>``."""

    mode: FeedbackMode


class LinefeedResponse(BaseModel):
    """``$LINEFEED = <mode>``."""

    mode: LinefeedMode


class BrightnessResponse(BaseModel):
    """``$BRIGHTNESS = <xxx>``."""

    level: Brightness


class ScrollModeResponse(BaseModel):
    """``$SCROLL_MODE = <xxx>``."""

    mode: ScrollMode


class SleepModeResponse(BaseModel):
    """``$SLEEP_MODE = <xxx>``."""

    mode: SleepMode


class NormalVoltResponse(BaseModel):
    """``$NORMALVOLT = <xxx>``."""

    voltage: NormalVolt


class FactoryResetResponse(BaseModel):
    """``$FACTORY SETTINGS RESTORED``."""

    restored: bool = True


class InvalidParameterResponse(BaseModel):
    """``$INVALID_PARAMETER``."""

    pass


class LowBatteryResponse(BaseModel):
    """``$LOWBAT`` (async)."""

    pass


class AVRStateResponse(BaseModel):
    """``$AVRSTATE = <BOOST|BUCK>``."""

    state: AVRState


class BackupTimeResponse(BaseModel):
    """``$TIME = <xxx>`` (minutes of backup remaining)."""

    minutes: int


class BatteryStateResponse(BaseModel):
    """``$BATTSTATE = <CHARGE|DISCHARGE|FULL>``."""

    state: BatteryChargeState


class VoltsInResponse(BaseModel):
    """``$VOLTS_IN = <vvv>``."""

    volts: float


class VoltsOutResponse(BaseModel):
    """``$VOLTS_OUT = <vvv>``."""

    volts: float


class WattsResponse(BaseModel):
    """``$WATTS = <xxxx>``."""

    watts: float


class CurrentResponse(BaseModel):
    """``$CURRENT = <xx.x>``."""

    amps: float


class VoltageResponse(BaseModel):
    """``$VOLTAGE = <xxx>``."""

    volts: float


class LoadResponse(BaseModel):
    """``$LOAD = <xxx>``."""

    percent: float


class IDLineResponse(BaseModel):
    """Plain ``$<text>`` line returned by ``?ID``."""

    text: str


class IDResponse(BaseModel):
    """Aggregate of the three ``?ID`` response lines."""

    manufacturer: str
    model: str
    firmware: str


class OutletStatusResponse(BaseModel):
    """Aggregate of four ``$BANK`` lines from ``?OUTLETSTAT``."""

    banks: dict[int, BankState]


class PowerMetricsResponse(BaseModel):
    """Aggregate of ``?POWER`` response lines."""

    volts_in: float
    volts_out: float
    watts: float
    current: float


class ListConfigResponse(BaseModel):
    """Aggregate of ``?LIST_CONFIG`` response lines."""

    bthresh: int | None = None
    bthresh3: int | None = None
    bthresh4: int | None = None
    buzzer: BuzzerMode | None = None
    avr: AVRMode | None = None
    feedback: FeedbackMode | None = None
    linefeed: LinefeedMode | None = None
    brightness: Brightness | None = None
    scroll_mode: ScrollMode | None = None
    sleep_mode: SleepMode | None = None
    normalvolt: NormalVolt | None = None


class RawResponse(BaseModel):
    """Unrecognised response line kept verbatim."""

    raw: str


# Union type for all parsed single-line responses
ParsedResponse = (
    BankStatusResponse
    | ButtonResponse
    | PowerStatusResponse
    | BatteryLevelResponse
    | BatteryThresholdResponse
    | BatteryThresholdGlobalResponse
    | BuzzerResponse
    | AVRModeResponse
    | FeedbackResponse
    | LinefeedResponse
    | BrightnessResponse
    | ScrollModeResponse
    | SleepModeResponse
    | NormalVoltResponse
    | FactoryResetResponse
    | InvalidParameterResponse
    | LowBatteryResponse
    | AVRStateResponse
    | BackupTimeResponse
    | BatteryStateResponse
    | VoltsInResponse
    | VoltsOutResponse
    | WattsResponse
    | CurrentResponse
    | VoltageResponse
    | LoadResponse
    | IDLineResponse
    | RawResponse
)

T = TypeVar("T", bound=ParsedResponse)


# ──────────────────────────────────────────────────────────────────────
# Command Builders
# ──────────────────────────────────────────────────────────────────────

CR = "\r"


def cmd_all_on() -> str:
    """Build ``!ALL_ON\\r``."""
    return f"!ALL_ON{CR}"


def cmd_all_off() -> str:
    """Build ``!ALL_OFF\\r``."""
    return f"!ALL_OFF{CR}"


def cmd_switch(bank: int | BankNumber, state: str | BankState) -> str:
    """Build ``!SWITCH <bank> <ON|OFF>\\r``."""
    b = BankNumber(bank)
    s = state if isinstance(state, BankState) else BankState(state.upper())
    return f"!SWITCH {b.value} {s.value}{CR}"


def cmd_set_batthresh(bank: int | BankNumber, level: int) -> str:
    """Build ``!SET_BATTHRESH <bank> <level>\\r``. bank must be 3 or 4."""
    b = BankNumber(bank)
    if b not in (BankNumber.BANK3, BankNumber.BANK4):
        raise ValidationError(f"Battery threshold only applies to banks 3 and 4, got {b.value}")
    if not (20 <= level <= 100):
        raise ValidationError(f"Threshold level must be 20–100, got {level}")
    return f"!SET_BATTHRESH {b.value} {level}{CR}"


def cmd_set_buzzer(mode: str | BuzzerMode) -> str:
    """Build ``!SET_BUZZER <ON|OFF>\\r``."""
    m = BuzzerMode(mode.upper() if isinstance(mode, str) else mode.value)
    return f"!SET_BUZZER {m.value}{CR}"


def cmd_set_avr(mode: str | AVRMode) -> str:
    """Build ``!SET_AVR <mode>\\r``."""
    m = AVRMode(mode.upper() if isinstance(mode, str) else mode.value)
    return f"!SET_AVR {m.value}{CR}"


def cmd_set_feedback(mode: str | FeedbackMode) -> str:
    """Build ``!SET_FEEDBACK <ON|OFF>\\r``."""
    m = FeedbackMode(mode.upper() if isinstance(mode, str) else mode.value)
    return f"!SET_FEEDBACK {m.value}{CR}"


def cmd_set_linefeed(mode: str | LinefeedMode) -> str:
    """Build ``!SET_LINEFEED <ON|OFF>\\r``."""
    m = LinefeedMode(mode.upper() if isinstance(mode, str) else mode.value)
    return f"!SET_LINEFEED {m.value}{CR}"


def cmd_set_bright(level: str | Brightness) -> str:
    """Build ``!SET_BRIGHT <xxx>\\r``."""
    b = Brightness(level if isinstance(level, str) else level.value)
    return f"!SET_BRIGHT {b.value}{CR}"


def cmd_set_scrollmode(mode: str | ScrollMode) -> str:
    """Build ``!SET_SCROLLMODE <xxx>\\r``."""
    m = ScrollMode(mode.upper() if isinstance(mode, str) else mode.value)
    return f"!SET_SCROLLMODE {m.value}{CR}"


def cmd_set_sleepmode(mode: str | SleepMode) -> str:
    """Build ``!SET_SLEEPMODE <xxx>\\r``."""
    m = SleepMode(mode.upper() if isinstance(mode, str) else mode.value)
    return f"!SET_SLEEPMODE {m.value}{CR}"


def cmd_reset_all() -> str:
    """Build ``!RESET_ALL\\r``."""
    return f"!RESET_ALL{CR}"


def cmd_set_normalvolt(voltage: str | NormalVolt) -> str:
    """Build ``!SET_NORMALVOLT <xxx>\\r``."""
    v = NormalVolt(voltage if isinstance(voltage, str) else voltage.value)
    return f"!SET_NORMALVOLT {v.value}{CR}"


# ── Query builders ────────────────────────────────────────────────────


def query_id() -> str:
    """Build ``?ID\\r``."""
    return f"?ID{CR}"


def query_outletstat() -> str:
    """Build ``?OUTLETSTAT\\r``."""
    return f"?OUTLETSTAT{CR}"


def query_powerstat() -> str:
    """Build ``?POWERSTAT\\r``."""
    return f"?POWERSTAT{CR}"


def query_power() -> str:
    """Build ``?POWER\\r``."""
    return f"?POWER{CR}"


def query_current() -> str:
    """Build ``?CURRENT\\r``."""
    return f"?CURRENT{CR}"


def query_voltage() -> str:
    """Build ``?VOLTAGE\\r``."""
    return f"?VOLTAGE{CR}"


def query_loadstat() -> str:
    """Build ``?LOADSTAT\\r``."""
    return f"?LOADSTAT{CR}"


def query_batterystat() -> str:
    """Build ``?BATTERYSTAT\\r``."""
    return f"?BATTERYSTAT{CR}"


def query_battstate() -> str:
    """Build ``?BATTSTATE\\r``."""
    return f"?BATTSTATE{CR}"


def query_time() -> str:
    """Build ``?TIME\\r``."""
    return f"?TIME{CR}"


def query_list_config() -> str:
    """Build ``?LIST_CONFIG\\r``."""
    return f"?LIST_CONFIG{CR}"


def query_help() -> str:
    """Build ``?HELP\\r``."""
    return f"?HELP{CR}"


# ──────────────────────────────────────────────────────────────────────
# Line Parser
# ──────────────────────────────────────────────────────────────────────

# Regex patterns for response lines
_RE_BANK = re.compile(r"^\$BANK\s*(\d)\s*=\s*(ON|OFF)$")
_RE_BUTTON = re.compile(r"^\$BUTTON\s*=\s*(ON|OFF)$")
_RE_PWR = re.compile(r"^\$PWR\s*=\s*(.+)$")
_RE_BATTERY = re.compile(r"^\$BATTERY\s*=\s*(\d+)$")
_RE_BTHRESH = re.compile(r"^\$BTHRESH\s*(\d)\s*=\s*(\d+)$")
_RE_BTHRESH_GLOBAL = re.compile(r"^\$BTHRESH\s*=\s*(\d+)$")
_RE_BUZZER = re.compile(r"^\$BUZZER\s*=\s*(ON|OFF)$")
_RE_AVR_MODE = re.compile(r"^\$AVR\s*=\s*(OFF|STANDARD|SENSITIVE)$")
_RE_FEEDBACK = re.compile(r"^\$FEEDBACK\s*=\s*(ON|OFF)$")
_RE_LINEFEED = re.compile(r"^\$?LINEFEED\s*=\s*(ON|OFF)$")
_RE_BRIGHTNESS = re.compile(r"^\$BRIGHTNESS\s*=\s*(\d+)$")
_RE_SCROLL = re.compile(r"^\$SCROLL_MODE\s*=\s*(.+)$")
_RE_SLEEP = re.compile(r"^\$SLEEP_MODE\s*=\s*(.+)$")
_RE_NORMALVOLT = re.compile(r"^\$NORMALVOLT\s*=\s*(\d+)$")
_RE_VOLTS_IN = re.compile(r"^\$VOLTS_IN\s*=\s*([\d.]+)$")
_RE_VOLTS_OUT = re.compile(r"^\$VOLTS_OUT\s*=\s*([\d.]+)$")
_RE_WATTS = re.compile(r"^\$WATTS\s*=\s*([\d.]+)$")
_RE_CURRENT = re.compile(r"^\$CURRENT\s*=\s*([\d.]+)$")
_RE_VOLTAGE = re.compile(r"^\$VOLTAGE\s*=\s*([\d.]+)$")
_RE_LOAD = re.compile(r"^\$LOAD\s*=\s*([\d.]+)$")
_RE_AVRSTATE = re.compile(r"^\$AVRSTATE\s*=\s*(BOOST|BUCK)$")
_RE_TIME = re.compile(r"^\$TIME\s*=\s*(\d+)$")
_RE_BATTSTATE = re.compile(r"^\$BATTSTATE\s*=\s*(CHARGE|DISCHARGE|FULL)$")


def parse_line(line: str) -> ParsedResponse:
    """Parse a single CR-stripped response line from the UPS.

    Strips any trailing ``\\r`` or ``\\n`` before matching.
    Returns a typed Pydantic model or ``RawResponse`` for unrecognised lines.
    """
    line = line.strip("\r\n ")

    # Exact-match tokens
    if line == "$INVALID_PARAMETER":
        return InvalidParameterResponse()
    if line == "$LOWBAT":
        return LowBatteryResponse()
    if line == "$FACTORY SETTINGS RESTORED":
        return FactoryResetResponse()

    # Regex-based parsing
    if m := _RE_LINEFEED.match(line):
        return LinefeedResponse(mode=LinefeedMode(m.group(1)))

    if not line.startswith("$"):
        return RawResponse(raw=line)

    if m := _RE_BANK.match(line):
        return BankStatusResponse(bank=BankNumber(int(m.group(1))), state=BankState(m.group(2)))
    if m := _RE_BUTTON.match(line):
        return ButtonResponse(state=ButtonState(m.group(1)))
    if m := _RE_PWR.match(line):
        return PowerStatusResponse(status=PowerStatus(m.group(1).strip()))
    if m := _RE_BATTERY.match(line):
        return BatteryLevelResponse(level=int(m.group(1)))
    if m := _RE_BTHRESH.match(line):
        return BatteryThresholdResponse(bank=BankNumber(int(m.group(1))), level=int(m.group(2)))
    if m := _RE_BTHRESH_GLOBAL.match(line):
        return BatteryThresholdGlobalResponse(level=int(m.group(1)))
    if m := _RE_BUZZER.match(line):
        return BuzzerResponse(mode=BuzzerMode(m.group(1)))
    if m := _RE_AVR_MODE.match(line):
        return AVRModeResponse(mode=AVRMode(m.group(1)))
    if m := _RE_FEEDBACK.match(line):
        return FeedbackResponse(mode=FeedbackMode(m.group(1)))
    if m := _RE_BRIGHTNESS.match(line):
        return BrightnessResponse(level=Brightness(m.group(1)))
    if m := _RE_SCROLL.match(line):
        return ScrollModeResponse(mode=ScrollMode(m.group(1).strip()))
    if m := _RE_SLEEP.match(line):
        return SleepModeResponse(mode=SleepMode(m.group(1).strip()))
    if m := _RE_NORMALVOLT.match(line):
        return NormalVoltResponse(voltage=NormalVolt(m.group(1)))
    if m := _RE_VOLTS_IN.match(line):
        return VoltsInResponse(volts=float(m.group(1)))
    if m := _RE_VOLTS_OUT.match(line):
        return VoltsOutResponse(volts=float(m.group(1)))
    if m := _RE_WATTS.match(line):
        return WattsResponse(watts=float(m.group(1)))
    if m := _RE_CURRENT.match(line):
        return CurrentResponse(amps=float(m.group(1)))
    if m := _RE_VOLTAGE.match(line):
        return VoltageResponse(volts=float(m.group(1)))
    if m := _RE_LOAD.match(line):
        return LoadResponse(percent=float(m.group(1)))
    if m := _RE_AVRSTATE.match(line):
        return AVRStateResponse(state=AVRState(m.group(1)))
    if m := _RE_TIME.match(line):
        return BackupTimeResponse(minutes=int(m.group(1)))
    if m := _RE_BATTSTATE.match(line):
        return BatteryStateResponse(state=BatteryChargeState(m.group(1)))

    # ID response lines (manufacturer, model, firmware) — plain $<text>
    if line.startswith("$"):
        return IDLineResponse(text=line[1:].strip())
    return RawResponse(raw=line)



# Fixed response counts for commands with documented response sizes.
_ID_RESPONSE_LINES = 3
_MAX_ALL_BANK_RESPONSES = 4
_MAX_SWITCH_RESPONSES = 1
_MAX_CONFIG_SET_RESPONSES = 1
_HELP_RESPONSE_LINES = 25

# ──────────────────────────────────────────────────────────────────────
# Transport Abstraction
# ──────────────────────────────────────────────────────────────────────


class Transport(abc.ABC):
    """Abstract transport for sending commands and receiving responses."""

    @abc.abstractmethod
    def open(self) -> None:
        """Open the transport connection."""

    @abc.abstractmethod
    def close(self) -> None:
        """Close the transport connection."""

    @abc.abstractmethod
    def write(self, data: str) -> None:
        """Write a command string (already CR-terminated)."""

    @abc.abstractmethod
    def read_line(self, timeout: float = 2.0) -> str:
        """Read one CR-terminated line. Returns the line *without* the CR.

        Raises ``JuicerTimeoutError`` if no complete line within *timeout* seconds.
        """

    def drain(self) -> None:
        """Discard any bytes already buffered for reading.

        Default implementation is a no-op; transports that talk to a real
        device override this to clear stale input before sending the next
        command.  This makes the client resilient when the previous response
        was not fully consumed (e.g. an exception interrupted the read loop).
        """

    @property
    @abc.abstractmethod
    def is_open(self) -> bool:
        """Whether the connection is currently open."""

    def __enter__(self) -> "Transport":
        """Open the transport and return it for ``with``-statement use."""
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        """Close the transport when leaving a ``with`` block."""
        self.close()


class SerialTransport(Transport):
    """RS-232 transport using pyserial — 9600/8N1, matching C++ ``openport()``."""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        retries: int = 50,
        retry_delay: float = 0.1,
    ) -> None:
        """Store connection parameters and retry policy for a serial port session."""
        self.port = port
        self.baudrate = baudrate
        self.retries = retries
        self.retry_delay = retry_delay
        self._serial: Any = None
        self._pending_byte: bytes = b""

    def open(self) -> None:
        """Open serial port with retry logic matching C++ (50 × 100 ms)."""
        import serial  # pyserial

        last_err: Exception | None = None
        for attempt in range(self.retries):
            try:
                self._serial = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.1,  # per-byte read timeout
                    write_timeout=2.0,
                )
                logger.info("Opened serial port %s (attempt %d)", self.port, attempt + 1)
                # Discard any stale bytes that may have been buffered before
                # we opened the port; the device may have been printing prompts
                # or unsolicited status while we were not listening.
                self.drain()
                return
            except serial.SerialException as exc:
                last_err = exc
                logger.debug("Port open attempt %d failed: %s", attempt + 1, exc)
                time.sleep(self.retry_delay)
        raise TransportError(
            f"Failed to open {self.port} after {self.retries} attempts: {last_err}"
        )

    def close(self) -> None:
        """Close the serial handle and discard any buffered unread byte."""
        if self._serial and self._serial.is_open:
            self._serial.close()
            logger.info("Closed serial port %s", self.port)
        self._serial = None
        self._pending_byte = b""

    def write(self, data: str) -> None:
        """Write an ASCII command to the open serial port."""
        if not self._serial or not self._serial.is_open:
            raise TransportError("Serial port not open")
        self._serial.write(data.encode("ascii"))

    def drain(self) -> None:
        """Discard any bytes already available on the port.

        Used to recover from a previous command whose trailing ``>`` prompt
        was not consumed (e.g. when an exception interrupted reading).  Reads
        non-blockingly until no more bytes arrive.
        """
        self._pending_byte = b""
        if not self._serial or not self._serial.is_open:
            return
        try:
            in_waiting = int(getattr(self._serial, "in_waiting", 0) or 0)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return
        if in_waiting <= 0:
            return
        try:
            discarded = self._serial.read(in_waiting)
        except Exception:  # pragma: no cover - defensive
            return
        try:
            count = len(discarded) if discarded else 0
        except TypeError:  # pragma: no cover - defensive
            return
        if count:
            logger.debug("Drained %d stale bytes from serial input", count)

    def read_line(self, timeout: float = 2.0) -> str:
        """Read bytes until CR (0x0D), stripping optional trailing LF.

        The real Furman F1500-UPS firmware behaves like a simple shell: it
        prints command output, then prints a ``>`` prompt byte (0x3E, no CR)
        to show that it is ready for the next command.  The prompt is not
        part of the response data.  When ``>`` is the first byte of a new read
        (either from a previous peek stored in ``_pending_byte`` or arriving
        fresh from the serial port), :class:`PromptReceived` is raised.
        Callers that iterate over variable-length responses (e.g.
        :meth:`JuicerClient._recv_variable`) catch this exception to stop
        reading, just as they catch :class:`JuicerTimeoutError`.

        Stray bytes that are neither ``$`` (response start) nor ``>`` (ready
        prompt) at the beginning of a new line are discarded with a debug
        log entry, since every valid Furman line begins with ``$``.
        """
        if not self._serial or not self._serial.is_open:
            raise TransportError("Serial port not open")
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._pending_byte:
                b = self._pending_byte
                self._pending_byte = b""
            else:
                b = self._serial.read(1)
            if not b:
                continue
            if b[0] == 0x3E and not buf:  # '>' prompt — ready for next command
                raise PromptReceived("Device '>' prompt received")
            if b[0] == 0x0D:  # CR — end of line
                # Peek for optional LF
                peek = self._serial.read(1)
                if peek and peek[0] != 0x0A:
                    self._pending_byte = peek
                return buf.decode("ascii", errors="replace")
            # All valid response lines start with '$'.  Silently drop any
            # garbage bytes that appear before a line begins, so a single
            # noise byte cannot corrupt the next response.
            if not buf and b[0] != 0x24:
                logger.debug("Discarded stray byte 0x%02X before '$'", b[0])
                continue
            buf.append(b[0])
        raise JuicerTimeoutError(f"No CR-terminated line within {timeout}s")

    @property
    def is_open(self) -> bool:
        """Report whether the underlying pyserial handle is currently open."""
        return self._serial is not None and self._serial.is_open


class _PromptSentinel:
    """Internal marker type used by :class:`FakeTransport` to simulate the ``>`` prompt.

    A singleton instance (:attr:`FakeTransport._PROMPT`) is queued by
    :meth:`FakeTransport.enqueue_prompt` so that :meth:`FakeTransport.read_line`
    can raise :class:`PromptReceived` exactly as :class:`SerialTransport` does
    when it receives the 0x3E byte from the real device.
    """


class FakeTransport(Transport):
    """In-memory transport for testing — no serial port needed.

    Usage::

        fake = FakeTransport()
        fake.enqueue_response("$BANK 1 = ON")
        fake.enqueue_response("$BANK 2 = ON")
        client = JuicerClient(fake)
        # client.all_on() will read those queued responses
    """

    def __init__(self) -> None:
        """Initialize an empty, closed in-memory transport."""
        self._responses: deque[str | _PromptSentinel] = deque()
        self._written: list[str] = []
        self._open = False

    # Singleton sentinel queued by enqueue_prompt() to simulate the '>' prompt.
    _PROMPT: _PromptSentinel = _PromptSentinel()

    def open(self) -> None:
        """Mark the fake transport as open for subsequent reads and writes."""
        self._open = True

    def close(self) -> None:
        """Mark the fake transport as closed without discarding queued data."""
        self._open = False

    def write(self, data: str) -> None:
        """Record one outbound command for later assertions in tests."""
        if not self._open:
            raise TransportError("FakeTransport not open")
        self._written.append(data)

    def read_line(self, timeout: float = 2.0) -> str:
        """Return the next queued response line, or raise when none are available.

        If the next queued item is the internal prompt sentinel (enqueued via
        :meth:`enqueue_prompt`), raises :class:`PromptReceived` to mirror the
        behaviour of :meth:`SerialTransport.read_line` when the real device
        prints its ``>`` ready prompt.
        """
        if not self._open:
            raise TransportError("FakeTransport not open")
        if not self._responses:
            raise JuicerTimeoutError("No more queued responses in FakeTransport")
        item = self._responses.popleft()
        if item is FakeTransport._PROMPT:
            raise PromptReceived("Simulated '>' prompt from FakeTransport")
        return str(item)

    @property
    def is_open(self) -> bool:
        """Report whether callers may currently interact with the fake transport."""
        return self._open

    # ── Test helpers ──────────────────────────────────────────────────

    def enqueue_response(self, line: str) -> None:
        """Queue a response line (without CR — simulates stripped transport)."""
        self._responses.append(line)

    def enqueue_responses(self, lines: Sequence[str]) -> None:
        """Queue multiple response lines."""
        self._responses.extend(lines)

    def enqueue_prompt(self) -> None:
        """Queue a simulated ``>`` ready prompt.

        Use this to reproduce real-firmware command cycles that print no data
        lines before the ``>`` prompt (e.g. ``!SET_LINEFEED ON``).  The next
        call to :meth:`read_line` will raise :class:`PromptReceived`.
        """
        self._responses.append(FakeTransport._PROMPT)

    @property
    def written(self) -> list[str]:
        """All raw strings written via ``write()``."""
        return self._written

    @property
    def last_command(self) -> str | None:
        """Last command string sent, or ``None`` if no command has been written."""
        return self._written[-1] if self._written else None

    def clear(self) -> None:
        """Reset all state."""
        self._responses.clear()
        self._written.clear()


# ──────────────────────────────────────────────────────────────────────
# High-Level Client
# ──────────────────────────────────────────────────────────────────────


_F = TypeVar("_F", bound=Callable[..., Any])


def _with_prompt_drain(func: _F) -> _F:
    """Decorator: consume the trailing ``>`` prompt after a command method.

    Every Furman command response ends with a ``>`` byte that the device
    prints to signal it is ready for the next command.  Wrapping each
    public client method ensures that byte is always consumed — on the
    success path *and* on the error path — so the next command starts
    with a clean input buffer instead of inheriting a stale prompt that
    would otherwise raise :class:`PromptReceived` on its first read.
    """

    @functools.wraps(func)
    def wrapper(self: "JuicerClient", *args: Any, **kwargs: Any) -> Any:
        try:
            return func(self, *args, **kwargs)
        finally:
            try:
                self._drain_prompt()
            except Exception as drain_exc:  # pragma: no cover - defensive
                logger.debug("Prompt drain after %s raised %r", func.__name__, drain_exc)

    return wrapper  # type: ignore[return-value]


class JuicerClient:
    """High-level client combining transport + command builders + response parsing.

    All public methods are blocking: they send a command and read expected responses.
    """

    def __init__(self, transport: Transport) -> None:
        """Bind the client to a transport implementation."""
        self.transport = transport

    # ── Helpers ───────────────────────────────────────────────────────

    def _send(self, cmd: str) -> None:
        """Send a pre-built command string.

        Before writing, any bytes still buffered on the transport are
        discarded.  In normal operation each command's response ends with
        ``>`` which is consumed by :meth:`_drain_prompt`; the proactive
        drain here protects against the case where a previous command
        aborted before its prompt was consumed.
        """
        try:
            self.transport.drain()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Transport drain before send raised %r", exc)
        logger.debug("TX: %r", cmd)
        self.transport.write(cmd)

    def _recv(self, timeout: float = 2.0) -> str:
        """Receive one response line."""
        line = self.transport.read_line(timeout=timeout)
        logger.debug("RX: %r", line)
        return line

    def _recv_parsed(self, timeout: float = 2.0) -> ParsedResponse:
        """Receive and parse one line."""
        return parse_line(self._recv(timeout))

    def _drain_prompt(self, timeout: float = 0.5) -> list[ParsedResponse]:
        """Consume bytes up to and including the trailing ``>`` ready prompt.

        Every real-firmware command response ends with the ``>`` byte that
        the device prints to indicate it is ready for the next command.  This
        helper is called at the end of every command so the next command
        starts with a clean buffer (otherwise the leftover ``>`` would be
        read by the next command's first ``read_line`` and raise
        :class:`PromptReceived`, surfacing as a spurious "Unavailable" error).

        Any unexpected response lines that arrive before the prompt are
        returned to the caller so they can be incorporated into the result
        or surfaced as protocol errors.  If neither a prompt nor any data
        arrives within *timeout* seconds, returns silently — the FakeTransport
        and older emulator builds do not always emit a prompt.
        """
        extras: list[ParsedResponse] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                extras.append(self._recv_parsed(max(0.01, deadline - time.monotonic())))
            except PromptReceived:
                return extras
            except JuicerTimeoutError:
                return extras
        return extras

    def _recv_n(self, n: int, timeout: float = 2.0) -> list[ParsedResponse]:
        """Receive and parse *n* response lines."""
        return [self._recv_parsed(timeout) for _ in range(n)]

    def _recv_until_timeout(
        self, timeout: float = 0.5, max_lines: int = 20
    ) -> list[ParsedResponse]:
        """Read variable-length responses, stopping after quiet timeout or prompt."""
        results: list[ParsedResponse] = []
        for _ in range(max_lines):
            try:
                results.append(self._recv_parsed(timeout))
            except (JuicerTimeoutError, PromptReceived):
                break
        return results

    def _recv_variable(
        self,
        *,
        min_lines: int,
        max_lines: int,
        quiet_timeout: float = 0.25,
        context: str,
    ) -> list[ParsedResponse]:
        """Read a response with required initial lines and optional trailing lines."""
        results: list[ParsedResponse] = []
        for _ in range(min_lines):
            resp = self._recv_parsed()
            if isinstance(resp, InvalidParameterResponse):
                raise ProtocolError(f"{context}: device returned $INVALID_PARAMETER")
            results.append(resp)
        for _ in range(max_lines - min_lines):
            try:
                resp = self._recv_parsed(quiet_timeout)
            except (JuicerTimeoutError, PromptReceived):
                break
            if isinstance(resp, InvalidParameterResponse):
                raise ProtocolError(f"{context}: device returned $INVALID_PARAMETER")
            results.append(resp)
        return results

    def _expect_one(
        self,
        expected_type: type[T],
        *,
        timeout: float = 2.0,
        context: str = "",
    ) -> T:
        """Read one protocol-defined response and validate its type."""
        resp = self._recv_parsed(timeout)
        label = context or expected_type.__name__
        if isinstance(resp, InvalidParameterResponse):
            raise ProtocolError(f"{label}: device returned $INVALID_PARAMETER")
        if isinstance(resp, expected_type):
            return resp
        raise ProtocolError(f"{label}: expected {expected_type.__name__}, got {resp!r}")

    def _expect_bank_status(
        self,
        bank: int | BankNumber,
        *,
        state: str | BankState | None = None,
        timeout: float = 2.0,
        context: str = "!SWITCH",
    ) -> BankStatusResponse:
        """Read and validate one bank status response."""
        expected_bank = BankNumber(bank)
        expected_state = (
            None
            if state is None
            else state
            if isinstance(state, BankState)
            else BankState(state.upper())
        )
        resp = self._expect_one(BankStatusResponse, timeout=timeout, context=context)
        if resp.bank != expected_bank:
            raise ProtocolError(
                f"{context}: expected bank {expected_bank.value}, got bank {resp.bank.value}"
            )
        if expected_state is not None and resp.state != expected_state:
            raise ProtocolError(
                f"{context}: expected bank {expected_bank.value} {expected_state.value}, "
                f"got {resp.state.value}"
            )
        return resp

    def _expect_all_bank_statuses(
        self,
        state: BankState,
        *,
        context: str,
    ) -> list[ParsedResponse]:
        """Read one bank-status response per outlet bank and validate the state."""
        responses: list[ParsedResponse] = []
        for bank in BankNumber:
            responses.append(self._expect_bank_status(bank, state=state, context=context))
        return responses

    # ── Commands ──────────────────────────────────────────────────────

    @_with_prompt_drain
    def all_on(self) -> list[ParsedResponse]:
        """Send ``!ALL_ON`` and collect documented status responses."""
        self._send(cmd_all_on())
        return self._recv_variable(min_lines=1, max_lines=6, context="!ALL_ON")

    @_with_prompt_drain
    def all_off(self) -> list[ParsedResponse]:
        """Send ``!ALL_OFF`` and collect documented status responses."""
        self._send(cmd_all_off())
        return self._recv_variable(min_lines=4, max_lines=6, context="!ALL_OFF")

    @_with_prompt_drain
    def switch(self, bank: int | BankNumber, state: str | BankState) -> list[ParsedResponse]:
        """Send ``!SWITCH`` and collect protocol status lines.

        Banks 1 and 2 report one bank status line. Banks 3 and 4 may also
        report related bank/battery status lines when battery threshold rules
        affect the requested action.
        """
        self._send(cmd_switch(bank, state))
        first = self._expect_bank_status(bank, context="!SWITCH")
        responses: list[ParsedResponse] = [first]
        if BankNumber(bank) in (BankNumber.BANK3, BankNumber.BANK4):
            responses.extend(self._recv_until_timeout(timeout=0.25, max_lines=3))
        return responses

    @_with_prompt_drain
    def set_batthresh(self, bank: int | BankNumber, level: int) -> list[ParsedResponse]:
        """Send ``!SET_BATTHRESH`` and read response."""
        self._send(cmd_set_batthresh(bank, level))
        resp = self._expect_one(
            BatteryThresholdResponse,
            context="!SET_BATTHRESH",
        )
        expected_bank = BankNumber(bank)
        if resp.bank != expected_bank:
            raise ProtocolError(
                f"!SET_BATTHRESH: expected bank {expected_bank.value}, got bank {resp.bank.value}"
            )
        return [resp]

    @_with_prompt_drain
    def set_buzzer(self, mode: str | BuzzerMode) -> list[ParsedResponse]:
        """Send ``!SET_BUZZER``."""
        self._send(cmd_set_buzzer(mode))
        return [self._expect_one(BuzzerResponse, context="!SET_BUZZER")]

    @_with_prompt_drain
    def set_avr(self, mode: str | AVRMode) -> list[ParsedResponse]:
        """Send ``!SET_AVR``."""
        self._send(cmd_set_avr(mode))
        return [self._expect_one(AVRModeResponse, context="!SET_AVR")]

    @_with_prompt_drain
    def set_feedback(self, mode: str | FeedbackMode) -> list[ParsedResponse]:
        """Send ``!SET_FEEDBACK``.

        Real firmware behaviour (observed on F1500-UPS):

        * ``!SET_FEEDBACK ON``  → ``$FEEDBACK=ON\\r>``
        * ``!SET_FEEDBACK OFF`` → ``>`` (ready prompt only, no data line)
        """
        if isinstance(mode, FeedbackMode):
            feedback_mode = mode
        else:
            feedback_mode = FeedbackMode(mode.upper())
        self._send(cmd_set_feedback(feedback_mode))
        try:
            return [self._expect_one(FeedbackResponse, timeout=0.5, context="!SET_FEEDBACK")]
        except PromptReceived:
            # A bare '>' ready prompt with no data line is valid only for OFF.
            if feedback_mode == FeedbackMode.OFF:
                return []
            raise
        except JuicerTimeoutError:
            if feedback_mode == FeedbackMode.OFF:
                return []
            raise

    @_with_prompt_drain
    def set_linefeed(self, mode: str | LinefeedMode) -> list[ParsedResponse]:
        """Send ``!SET_LINEFEED``.

        Real firmware prints only the ``>`` prompt for this command (no data
        line), so an empty list is a valid successful result.
        """
        self._send(cmd_set_linefeed(mode))
        try:
            return [self._expect_one(LinefeedResponse, context="!SET_LINEFEED")]
        except (JuicerTimeoutError, PromptReceived):
            return []

    @_with_prompt_drain
    def set_bright(self, level: str | Brightness) -> list[ParsedResponse]:
        """Send ``!SET_BRIGHT``.

        Real firmware prints only the ``>`` prompt for this command (no data
        line), so an empty list is a valid successful result.
        """
        self._send(cmd_set_bright(level))
        try:
            return [self._expect_one(BrightnessResponse, context="!SET_BRIGHT")]
        except (JuicerTimeoutError, PromptReceived):
            return []

    @_with_prompt_drain
    def set_scrollmode(self, mode: str | ScrollMode) -> list[ParsedResponse]:
        """Send ``!SET_SCROLLMODE``.

        Real firmware prints only the ``>`` prompt for this command (no data
        line), so an empty list is a valid successful result.
        """
        self._send(cmd_set_scrollmode(mode))
        try:
            return [self._expect_one(ScrollModeResponse, context="!SET_SCROLLMODE")]
        except (JuicerTimeoutError, PromptReceived):
            return []

    @_with_prompt_drain
    def set_sleepmode(self, mode: str | SleepMode) -> list[ParsedResponse]:
        """Send ``!SET_SLEEPMODE``.

        Real firmware prints only the ``>`` prompt for this command (no data
        line), so an empty list is a valid successful result.
        """
        self._send(cmd_set_sleepmode(mode))
        try:
            return [self._expect_one(SleepModeResponse, context="!SET_SLEEPMODE")]
        except (JuicerTimeoutError, PromptReceived):
            return []

    @_with_prompt_drain
    def reset_all(self) -> FactoryResetResponse:
        """Send ``!RESET_ALL`` and expect factory-reset confirmation."""
        self._send(cmd_reset_all())
        resp = self._recv_parsed(timeout=5.0)
        if isinstance(resp, FactoryResetResponse):
            return resp
        raise ProtocolError(f"Expected FactoryResetResponse, got {resp!r}")

    @_with_prompt_drain
    def set_normalvolt(self, voltage: str | NormalVolt) -> list[ParsedResponse]:
        """Send ``!SET_NORMALVOLT``."""
        self._send(cmd_set_normalvolt(voltage))
        return [self._expect_one(NormalVoltResponse, context="!SET_NORMALVOLT")]

    # ── Queries ───────────────────────────────────────────────────────

    @_with_prompt_drain
    def query_id(self) -> IDResponse:
        """Send ``?ID`` and parse the three-line response."""
        self._send(query_id())
        responses = [self._recv_parsed() for _ in range(_ID_RESPONSE_LINES)]
        if not all(isinstance(resp, IDLineResponse) for resp in responses):
            raise ProtocolError(f"Unexpected ?ID response lines: {responses!r}")
        id_lines = [resp.text for resp in responses if isinstance(resp, IDLineResponse)]
        return IDResponse(
            manufacturer=id_lines[0],
            model=id_lines[1],
            firmware=id_lines[2],
        )

    @_with_prompt_drain
    def query_outlet_status(self) -> OutletStatusResponse:
        """Send ``?OUTLETSTAT`` and parse four bank-status lines."""
        self._send(query_outletstat())
        banks: dict[int, BankState] = {}
        for _ in range(4):
            resp = self._recv_parsed()
            if isinstance(resp, BankStatusResponse):
                banks[resp.bank.value] = resp.state
            else:
                raise ProtocolError(f"Unexpected ?OUTLETSTAT response: {resp!r}")
        missing = set(range(1, 5)) - banks.keys()
        if missing:
            missing_banks = ", ".join(str(bank) for bank in sorted(missing))
            raise ProtocolError(f"?OUTLETSTAT response missing banks: {missing_banks}")
        return OutletStatusResponse(banks=banks)

    @_with_prompt_drain
    def query_power_status(self) -> PowerStatusResponse:
        """Send ``?POWERSTAT`` and parse the single-line response."""
        self._send(query_powerstat())
        resp = self._recv_parsed()
        if isinstance(resp, PowerStatusResponse):
            return resp
        raise ProtocolError(f"Expected PowerStatusResponse, got {resp!r}")

    @_with_prompt_drain
    def query_power(self) -> PowerMetricsResponse:
        """Send ``?POWER`` and parse the four metrics lines."""
        self._send(query_power())
        data: dict[str, float] = {}
        for _ in range(4):
            resp = self._recv_parsed()
            if isinstance(resp, VoltsInResponse):
                data["volts_in"] = resp.volts
            elif isinstance(resp, VoltsOutResponse):
                data["volts_out"] = resp.volts
            elif isinstance(resp, WattsResponse):
                data["watts"] = resp.watts
            elif isinstance(resp, CurrentResponse):
                data["current"] = resp.amps
            else:
                raise ProtocolError(f"Unexpected ?POWER response: {resp!r}")
        required = {"volts_in", "volts_out", "watts", "current"}
        missing = required - data.keys()
        if missing:
            missing_fields = ", ".join(sorted(missing))
            raise ProtocolError(f"?POWER response missing fields: {missing_fields}")
        return PowerMetricsResponse(
            volts_in=data["volts_in"],
            volts_out=data["volts_out"],
            watts=data["watts"],
            current=data["current"],
        )

    @_with_prompt_drain
    def query_current(self) -> CurrentResponse:
        """Send ``?CURRENT``."""
        self._send(query_current())
        resp = self._recv_parsed()
        if isinstance(resp, CurrentResponse):
            return resp
        raise ProtocolError(f"Expected CurrentResponse, got {resp!r}")

    @_with_prompt_drain
    def query_voltage(self) -> VoltageResponse:
        """Send ``?VOLTAGE``."""
        self._send(query_voltage())
        resp = self._recv_parsed()
        if isinstance(resp, VoltageResponse):
            return resp
        if isinstance(resp, VoltsInResponse):
            return VoltageResponse(volts=resp.volts)
        raise ProtocolError(f"Expected VoltageResponse, got {resp!r}")

    @_with_prompt_drain
    def query_loadstat(self) -> LoadResponse:
        """Send ``?LOADSTAT``."""
        self._send(query_loadstat())
        resp = self._recv_parsed()
        if isinstance(resp, LoadResponse):
            return resp
        raise ProtocolError(f"Expected LoadResponse, got {resp!r}")

    @_with_prompt_drain
    def query_battery_status(self) -> BatteryLevelResponse:
        """Send ``?BATTERYSTAT``."""
        self._send(query_batterystat())
        resp = self._recv_parsed()
        if isinstance(resp, BatteryLevelResponse):
            return resp
        raise ProtocolError(f"Expected BatteryLevelResponse, got {resp!r}")

    @_with_prompt_drain
    def query_battery_state(self) -> BatteryStateResponse:
        """Send ``?BATTSTATE``."""
        self._send(query_battstate())
        resp = self._recv_parsed()
        if isinstance(resp, BatteryStateResponse):
            return resp
        raise ProtocolError(f"Expected BatteryStateResponse, got {resp!r}")

    @_with_prompt_drain
    def query_backup_time(self) -> BackupTimeResponse:
        """Send ``?TIME``."""
        self._send(query_time())
        resp = self._recv_parsed()
        if isinstance(resp, BackupTimeResponse):
            return resp
        raise ProtocolError(f"Expected BackupTimeResponse, got {resp!r}")

    @_with_prompt_drain
    def query_list_config(self) -> ListConfigResponse:
        """Send ``?LIST_CONFIG`` and aggregate response lines."""
        self._send(query_list_config())
        cfg = ListConfigResponse()
        responses = self._recv_variable(min_lines=9, max_lines=10, context="?LIST_CONFIG")
        for resp in responses:
            if isinstance(resp, BatteryThresholdResponse):
                cfg.bthresh = resp.level
                if resp.bank == BankNumber.BANK3:
                    cfg.bthresh3 = resp.level
                elif resp.bank == BankNumber.BANK4:
                    cfg.bthresh4 = resp.level
            elif isinstance(resp, BatteryThresholdGlobalResponse):
                cfg.bthresh = resp.level
            elif isinstance(resp, BuzzerResponse):
                cfg.buzzer = resp.mode
            elif isinstance(resp, AVRModeResponse):
                cfg.avr = resp.mode
            elif isinstance(resp, FeedbackResponse):
                cfg.feedback = resp.mode
            elif isinstance(resp, LinefeedResponse):
                cfg.linefeed = resp.mode
            elif isinstance(resp, BrightnessResponse):
                cfg.brightness = resp.level
            elif isinstance(resp, ScrollModeResponse):
                cfg.scroll_mode = resp.mode
            elif isinstance(resp, SleepModeResponse):
                cfg.sleep_mode = resp.mode
            elif isinstance(resp, NormalVoltResponse):
                cfg.normalvolt = resp.voltage
            elif isinstance(resp, InvalidParameterResponse):
                raise ProtocolError("?LIST_CONFIG: device returned $INVALID_PARAMETER")
            else:
                raise ProtocolError(f"?LIST_CONFIG: unexpected response {resp!r}")
        return cfg

    @_with_prompt_drain
    def query_help(self) -> list[str]:
        """Send ``?HELP`` and return the list of command/query names."""
        self._send(query_help())
        lines: list[str] = []
        responses = self._recv_variable(min_lines=1, max_lines=40, context="?HELP")
        for resp in responses:
            if isinstance(resp, RawResponse):
                lines.append(resp.raw)
            elif isinstance(resp, InvalidParameterResponse):
                raise ProtocolError("?HELP: device returned $INVALID_PARAMETER")
            else:
                raise ProtocolError(f"?HELP: unexpected response {resp!r}")
        return lines

    # ── Connection lifecycle ──────────────────────────────────────────

    def initialize(self) -> None:
        """Establish a known protocol environment on the device.

        Drains any leftover bytes on the transport, then sets feedback and
        line-feed modes to documented defaults so subsequent reads parse
        deterministically.  Failures are swallowed and logged because some
        emulator builds (and FakeTransport-based tests) do not implement
        the configuration commands; the client must still function.
        """
        try:
            self.transport.drain()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Transport drain during initialize raised %r", exc)
        for setter, value in (
            (self.set_feedback, FeedbackMode.ON),
            (self.set_linefeed, LinefeedMode.OFF),
        ):
            try:
                setter(value)
            except Exception as exc:
                logger.debug(
                    "initialize: %s(%s) failed (%s); continuing", setter.__name__, value, exc
                )

