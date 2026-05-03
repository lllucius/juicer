"""Juicer serial protocol — commands, responses, transport, and client.

Implements the complete Furman F1500-UPS E RS-232 protocol from manual.txt:
13 commands, 10 queries, all response families, plus transport abstraction.

All command construction and response parsing works without a real serial port.
"""

from __future__ import annotations

import abc
import enum
import logging
import re
import time
from collections import deque
from typing import Any, Sequence

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────


class JuicerError(Exception):
    """Base exception for all Juicer errors."""


class ProtocolError(JuicerError):
    """Invalid or unexpected data on the wire."""


class TimeoutError(JuicerError):  # noqa: A001
    """No response within deadline."""


class TransportError(JuicerError):
    """Low-level transport failure (port open, read, write)."""


class ValidationError(JuicerError):
    """Command parameter validation failed."""


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
    ON = "ON"
    OFF = "OFF"


class AVRMode(str, enum.Enum):
    OFF = "OFF"
    STANDARD = "STANDARD"
    SENSITIVE = "SENSITIVE"


class FeedbackMode(str, enum.Enum):
    ON = "ON"
    OFF = "OFF"


class LinefeedMode(str, enum.Enum):
    ON = "ON"
    OFF = "OFF"


class Brightness(str, enum.Enum):
    B100 = "100"
    B075 = "075"
    B050 = "050"
    B025 = "025"


class ScrollMode(str, enum.Enum):
    SEC5 = "5SEC"
    SEC10 = "10SEC"
    OFF = "OFF"


class SleepMode(str, enum.Enum):
    SEC30 = "30SEC"
    SEC60 = "60SEC"
    OFF = "OFF"


class NormalVolt(str, enum.Enum):
    V220 = "220"
    V230 = "230"
    V240 = "240"


class AVRState(str, enum.Enum):
    BOOST = "BOOST"
    BUCK = "BUCK"


class BatteryChargeState(str, enum.Enum):
    CHARGE = "CHARGE"
    DISCHARGE = "DISCHARGE"
    FULL = "FULL"


class ButtonState(str, enum.Enum):
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
    """``$BTHRESH <bank> = <level>``."""

    bank: BankNumber
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
    | RawResponse
)


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
    s = BankState(state.upper() if isinstance(state, str) else state.value)
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
_RE_BTHRESH = re.compile(r"^\$BTHRESH\s+(\d)\s*=\s*(\d+)$")
_RE_BUZZER = re.compile(r"^\$BUZZER\s*=\s*(ON|OFF)$")
_RE_AVR_MODE = re.compile(r"^\$AVR\s*=\s*(OFF|STANDARD|SENSITIVE)$")
_RE_FEEDBACK = re.compile(r"^\$FEEDBACK\s*=\s*(ON|OFF)$")
_RE_LINEFEED = re.compile(r"^\$LINEFEED\s*=\s*(ON|OFF)$")
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

    if not line.startswith("$"):
        return RawResponse(raw=line)

    # Exact-match tokens
    if line == "$INVALID_PARAMETER":
        return InvalidParameterResponse()
    if line == "$LOWBAT":
        return LowBatteryResponse()
    if line == "$FACTORY SETTINGS RESTORED":
        return FactoryResetResponse()

    # Regex-based parsing
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
    if m := _RE_BUZZER.match(line):
        return BuzzerResponse(mode=BuzzerMode(m.group(1)))
    if m := _RE_AVR_MODE.match(line):
        return AVRModeResponse(mode=AVRMode(m.group(1)))
    if m := _RE_FEEDBACK.match(line):
        return FeedbackResponse(mode=FeedbackMode(m.group(1)))
    if m := _RE_LINEFEED.match(line):
        return LinefeedResponse(mode=LinefeedMode(m.group(1)))
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
    return RawResponse(raw=line)


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

        Raises ``TimeoutError`` if no complete line within *timeout* seconds.
        """

    @property
    @abc.abstractmethod
    def is_open(self) -> bool:
        """Whether the connection is currently open."""

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
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
                )
                logger.info("Opened serial port %s (attempt %d)", self.port, attempt + 1)
                return
            except serial.SerialException as exc:
                last_err = exc
                logger.debug("Port open attempt %d failed: %s", attempt + 1, exc)
                time.sleep(self.retry_delay)
        raise TransportError(
            f"Failed to open {self.port} after {self.retries} attempts: {last_err}"
        )

    def close(self) -> None:
        if self._serial and self._serial.is_open:
            self._serial.close()
            logger.info("Closed serial port %s", self.port)
        self._serial = None
        self._pending_byte = b""

    def write(self, data: str) -> None:
        if not self._serial or not self._serial.is_open:
            raise TransportError("Serial port not open")
        self._serial.write(data.encode("ascii"))

    def read_line(self, timeout: float = 2.0) -> str:
        """Read bytes until CR (0x0D), stripping optional trailing LF."""
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
            if b[0] == 0x0D:  # CR — end of line
                # Peek for optional LF
                peek = self._serial.read(1)
                if peek and peek[0] != 0x0A:
                    self._pending_byte = peek
                return buf.decode("ascii", errors="replace")
            buf.append(b[0])
        raise TimeoutError(f"No CR-terminated line within {timeout}s")

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open


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
        self._responses: deque[str] = deque()
        self._written: list[str] = []
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: str) -> None:
        if not self._open:
            raise TransportError("FakeTransport not open")
        self._written.append(data)

    def read_line(self, timeout: float = 2.0) -> str:
        if not self._open:
            raise TransportError("FakeTransport not open")
        if not self._responses:
            raise TimeoutError("No more queued responses in FakeTransport")
        return self._responses.popleft()

    @property
    def is_open(self) -> bool:
        return self._open

    # ── Test helpers ──────────────────────────────────────────────────

    def enqueue_response(self, line: str) -> None:
        """Queue a response line (without CR — simulates stripped transport)."""
        self._responses.append(line)

    def enqueue_responses(self, lines: Sequence[str]) -> None:
        """Queue multiple response lines."""
        self._responses.extend(lines)

    @property
    def written(self) -> list[str]:
        """All raw strings written via ``write()``."""
        return self._written

    @property
    def last_command(self) -> str | None:
        """Last command string sent."""
        return self._written[-1] if self._written else None

    def clear(self) -> None:
        """Reset all state."""
        self._responses.clear()
        self._written.clear()


# ──────────────────────────────────────────────────────────────────────
# High-Level Client
# ──────────────────────────────────────────────────────────────────────


class JuicerClient:
    """High-level client combining transport + command builders + response parsing.

    All public methods are blocking: they send a command and read expected responses.
    """

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    # ── Helpers ───────────────────────────────────────────────────────

    def _send(self, cmd: str) -> None:
        """Send a pre-built command string."""
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

    def _recv_n(self, n: int, timeout: float = 2.0) -> list[ParsedResponse]:
        """Receive and parse *n* response lines."""
        return [self._recv_parsed(timeout) for _ in range(n)]

    def _recv_until_timeout(
        self, timeout: float = 0.5, max_lines: int = 20
    ) -> list[ParsedResponse]:
        """Read lines until timeout (for variable-length responses)."""
        results: list[ParsedResponse] = []
        for _ in range(max_lines):
            try:
                results.append(self._recv_parsed(timeout))
            except TimeoutError:
                break
        return results

    # ── Commands ──────────────────────────────────────────────────────

    def all_on(self) -> list[ParsedResponse]:
        """Send ``!ALL_ON`` and collect responses (bank statuses + button)."""
        self._send(cmd_all_on())
        return self._recv_until_timeout(timeout=2.0, max_lines=10)

    def all_off(self) -> list[ParsedResponse]:
        """Send ``!ALL_OFF`` and collect responses."""
        self._send(cmd_all_off())
        return self._recv_until_timeout(timeout=2.0, max_lines=10)

    def switch(self, bank: int | BankNumber, state: str | BankState) -> list[ParsedResponse]:
        """Send ``!SWITCH`` and collect response(s)."""
        self._send(cmd_switch(bank, state))
        return self._recv_until_timeout(timeout=2.0, max_lines=5)

    def set_batthresh(self, bank: int | BankNumber, level: int) -> list[ParsedResponse]:
        """Send ``!SET_BATTHRESH`` and read response."""
        self._send(cmd_set_batthresh(bank, level))
        return self._recv_until_timeout(timeout=2.0, max_lines=3)

    def set_buzzer(self, mode: str | BuzzerMode) -> list[ParsedResponse]:
        """Send ``!SET_BUZZER``."""
        self._send(cmd_set_buzzer(mode))
        return self._recv_until_timeout(timeout=2.0, max_lines=3)

    def set_avr(self, mode: str | AVRMode) -> list[ParsedResponse]:
        """Send ``!SET_AVR``."""
        self._send(cmd_set_avr(mode))
        return self._recv_until_timeout(timeout=2.0, max_lines=3)

    def set_feedback(self, mode: str | FeedbackMode) -> list[ParsedResponse]:
        """Send ``!SET_FEEDBACK``."""
        self._send(cmd_set_feedback(mode))
        return self._recv_until_timeout(timeout=2.0, max_lines=3)

    def set_linefeed(self, mode: str | LinefeedMode) -> list[ParsedResponse]:
        """Send ``!SET_LINEFEED``."""
        self._send(cmd_set_linefeed(mode))
        return self._recv_until_timeout(timeout=2.0, max_lines=3)

    def set_bright(self, level: str | Brightness) -> list[ParsedResponse]:
        """Send ``!SET_BRIGHT``."""
        self._send(cmd_set_bright(level))
        return self._recv_until_timeout(timeout=2.0, max_lines=3)

    def set_scrollmode(self, mode: str | ScrollMode) -> list[ParsedResponse]:
        """Send ``!SET_SCROLLMODE``."""
        self._send(cmd_set_scrollmode(mode))
        return self._recv_until_timeout(timeout=2.0, max_lines=3)

    def set_sleepmode(self, mode: str | SleepMode) -> list[ParsedResponse]:
        """Send ``!SET_SLEEPMODE``."""
        self._send(cmd_set_sleepmode(mode))
        return self._recv_until_timeout(timeout=2.0, max_lines=3)

    def reset_all(self) -> FactoryResetResponse:
        """Send ``!RESET_ALL`` and expect factory-reset confirmation."""
        self._send(cmd_reset_all())
        resp = self._recv_parsed(timeout=5.0)
        if isinstance(resp, FactoryResetResponse):
            return resp
        raise ProtocolError(f"Expected FactoryResetResponse, got {resp!r}")

    def set_normalvolt(self, voltage: str | NormalVolt) -> list[ParsedResponse]:
        """Send ``!SET_NORMALVOLT``."""
        self._send(cmd_set_normalvolt(voltage))
        return self._recv_until_timeout(timeout=2.0, max_lines=3)

    # ── Queries ───────────────────────────────────────────────────────

    def query_id(self) -> IDResponse:
        """Send ``?ID`` and parse the three-line response."""
        self._send(query_id())
        lines = [self._recv() for _ in range(3)]
        return IDResponse(
            manufacturer=lines[0].lstrip("$").strip(),
            model=lines[1].lstrip("$").strip(),
            firmware=lines[2].lstrip("$").strip(),
        )

    def query_outlet_status(self) -> OutletStatusResponse:
        """Send ``?OUTLETSTAT`` and parse four bank-status lines."""
        self._send(query_outletstat())
        banks: dict[int, BankState] = {}
        for _ in range(4):
            resp = self._recv_parsed()
            if isinstance(resp, BankStatusResponse):
                banks[resp.bank.value] = resp.state
            else:
                logger.warning("Unexpected response in OUTLETSTAT: %r", resp)
        return OutletStatusResponse(banks=banks)

    def query_power_status(self) -> PowerStatusResponse:
        """Send ``?POWERSTAT`` and parse the single-line response."""
        self._send(query_powerstat())
        resp = self._recv_parsed()
        if isinstance(resp, PowerStatusResponse):
            return resp
        raise ProtocolError(f"Expected PowerStatusResponse, got {resp!r}")

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
                logger.warning("Unexpected in POWER: %r", resp)
        return PowerMetricsResponse(
            volts_in=data.get("volts_in", 0),
            volts_out=data.get("volts_out", 0),
            watts=data.get("watts", 0),
            current=data.get("current", 0),
        )

    def query_current(self) -> CurrentResponse:
        """Send ``?CURRENT``."""
        self._send(query_current())
        resp = self._recv_parsed()
        if isinstance(resp, CurrentResponse):
            return resp
        raise ProtocolError(f"Expected CurrentResponse, got {resp!r}")

    def query_voltage(self) -> VoltageResponse:
        """Send ``?VOLTAGE``."""
        self._send(query_voltage())
        resp = self._recv_parsed()
        if isinstance(resp, VoltageResponse):
            return resp
        raise ProtocolError(f"Expected VoltageResponse, got {resp!r}")

    def query_loadstat(self) -> LoadResponse:
        """Send ``?LOADSTAT``."""
        self._send(query_loadstat())
        resp = self._recv_parsed()
        if isinstance(resp, LoadResponse):
            return resp
        raise ProtocolError(f"Expected LoadResponse, got {resp!r}")

    def query_battery_status(self) -> BatteryLevelResponse:
        """Send ``?BATTERYSTAT``."""
        self._send(query_batterystat())
        resp = self._recv_parsed()
        if isinstance(resp, BatteryLevelResponse):
            return resp
        raise ProtocolError(f"Expected BatteryLevelResponse, got {resp!r}")

    def query_list_config(self) -> ListConfigResponse:
        """Send ``?LIST_CONFIG`` and aggregate response lines."""
        self._send(query_list_config())
        cfg = ListConfigResponse()
        responses = self._recv_until_timeout(timeout=2.0, max_lines=15)
        for resp in responses:
            if isinstance(resp, BatteryThresholdResponse):
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
            # $BTHRESH without bank number in LIST_CONFIG
            elif isinstance(resp, RawResponse) and resp.raw.startswith("$BTHRESH"):
                m = re.match(r"\$BTHRESH\s*=\s*(\d+)", resp.raw)
                if m:
                    cfg.bthresh = int(m.group(1))
        return cfg

    def query_help(self) -> list[str]:
        """Send ``?HELP`` and return the list of command/query names."""
        self._send(query_help())
        lines: list[str] = []
        responses = self._recv_until_timeout(timeout=2.0, max_lines=30)
        for resp in responses:
            if isinstance(resp, RawResponse):
                lines.append(resp.raw)
            else:
                lines.append(str(resp))
        return lines
