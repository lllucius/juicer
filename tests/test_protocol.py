from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from juicer.protocol import (
    AVRMode,
    AVRModeResponse,
    AVRState,
    AVRStateResponse,
    BackupTimeResponse,
    BankNumber,
    BankState,
    BankStatusResponse,
    BatteryChargeState,
    BatteryLevelResponse,
    BatteryStateResponse,
    BatteryThresholdGlobalResponse,
    BatteryThresholdResponse,
    Brightness,
    BrightnessResponse,
    ButtonResponse,
    ButtonState,
    BuzzerMode,
    BuzzerResponse,
    CurrentResponse,
    FactoryResetResponse,
    FakeTransport,
    FeedbackMode,
    FeedbackResponse,
    IDLineResponse,
    InvalidParameterResponse,
    JuicerClient,
    JuicerTimeoutError,
    LinefeedMode,
    LinefeedResponse,
    LoadResponse,
    LowBatteryResponse,
    NormalVolt,
    NormalVoltResponse,
    PowerStatus,
    PowerStatusResponse,
    PromptReceived,
    ProtocolError,
    RawResponse,
    ScrollMode,
    ScrollModeResponse,
    SerialTransport,
    SleepMode,
    SleepModeResponse,
    TransportError,
    ValidationError,
    VoltageResponse,
    VoltsInResponse,
    VoltsOutResponse,
    WattsResponse,
    cmd_all_off,
    cmd_all_on,
    cmd_reset_all,
    cmd_set_avr,
    cmd_set_batthresh,
    cmd_set_bright,
    cmd_set_buzzer,
    cmd_set_feedback,
    cmd_set_linefeed,
    cmd_set_normalvolt,
    cmd_set_scrollmode,
    cmd_set_sleepmode,
    cmd_switch,
    parse_line,
    query_batterystat,
    query_battstate,
    query_current,
    query_help,
    query_id,
    query_list_config,
    query_loadstat,
    query_outletstat,
    query_power,
    query_powerstat,
    query_time,
    query_voltage,
)

# ──────────────────────────────────────────────────────────────────────
# SerialTransport.read_line  (original tests, preserved)
# ──────────────────────────────────────────────────────────────────────


class _StubSerial:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0
        self.is_open = True

    def read(self, size: int = 1) -> bytes:
        if self._offset >= len(self._payload):
            return b""
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _CountingFakeTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.read_count = 0

    def read_line(self, timeout: float = 2.0) -> str:
        self.read_count += 1
        return super().read_line(timeout)


def test_read_line_preserves_first_byte_after_cr_without_lf() -> None:
    transport = SerialTransport(port="COM1")
    transport._serial = _StubSerial(b"$BANK 1 = OFF\r$BANK 2 = OFF\r")

    first = transport.read_line()
    second = transport.read_line()

    assert first == "$BANK 1 = OFF"
    assert second == "$BANK 2 = OFF"


def test_read_line_discards_optional_lf_after_cr() -> None:
    transport = SerialTransport(port="COM1")
    transport._serial = _StubSerial(b"$PWR = NORMAL\r\n$BATTERY = 85\r\n")

    first = transport.read_line()
    second = transport.read_line()

    assert first == "$PWR = NORMAL"
    assert second == "$BATTERY = 85"


def test_read_line_raises_prompt_received_when_gt_is_first_byte() -> None:
    """Real firmware prints '>' (0x3E) as a ready prompt after command output."""
    transport = SerialTransport(port="COM1")
    # Single-line response followed by the ready prompt, as seen on real device
    transport._serial = _StubSerial(b"$BUZZER=ON\r>")

    line = transport.read_line()
    assert line == "$BUZZER=ON"

    with pytest.raises(PromptReceived):
        transport.read_line()


def test_read_line_raises_prompt_received_for_prompt_only_command() -> None:
    """Commands like !SET_LINEFEED print only '>' with no preceding data line."""
    transport = SerialTransport(port="COM1")
    transport._serial = _StubSerial(b">")

    with pytest.raises(PromptReceived):
        transport.read_line()


def test_read_line_handles_multi_line_output_followed_by_prompt() -> None:
    """Multi-line output: all data lines returned, then PromptReceived on '>'."""
    transport = SerialTransport(port="COM1")
    transport._serial = _StubSerial(b"$BANK1=ON\r$BANK2=ON\r$BANK3=ON\r$BANK4=ON\r>")

    assert transport.read_line() == "$BANK1=ON"
    assert transport.read_line() == "$BANK2=ON"
    assert transport.read_line() == "$BANK3=ON"
    assert transport.read_line() == "$BANK4=ON"
    with pytest.raises(PromptReceived):
        transport.read_line()


def test_fake_transport_enqueue_prompt_raises_prompt_received() -> None:
    t = FakeTransport()
    t.open()
    t.enqueue_response("$BUZZER=ON")
    t.enqueue_prompt()

    assert t.read_line() == "$BUZZER=ON"
    with pytest.raises(PromptReceived):
        t.read_line()


def test_fake_transport_enqueue_prompt_only() -> None:
    """enqueue_prompt() with no data lines simulates a prompt-only command."""
    t = FakeTransport()
    t.open()
    t.enqueue_prompt()

    with pytest.raises(PromptReceived):
        t.read_line()


def test_serial_open_sets_write_timeout() -> None:
    serial_module = Mock()
    serial_module.EIGHTBITS = 8
    serial_module.PARITY_NONE = "N"
    serial_module.STOPBITS_ONE = 1
    serial_module.SerialException = Exception

    with patch.dict(sys.modules, {"serial": serial_module}):
        transport = SerialTransport(port="COM1")
        transport.open()

    serial_module.Serial.assert_called_once_with(
        port="COM1",
        baudrate=9600,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=0.1,
        write_timeout=2.0,
    )


# ──────────────────────────────────────────────────────────────────────
# Action command builders
# ──────────────────────────────────────────────────────────────────────


def test_cmd_all_on() -> None:
    assert cmd_all_on() == "!ALL_ON\r"


def test_cmd_all_off() -> None:
    assert cmd_all_off() == "!ALL_OFF\r"


def test_cmd_switch_bank_on() -> None:
    assert cmd_switch(1, "ON") == "!SWITCH 1 ON\r"


def test_cmd_switch_bank_off() -> None:
    assert cmd_switch(2, "OFF") == "!SWITCH 2 OFF\r"


def test_cmd_switch_all_banks() -> None:
    for bank in (1, 2, 3, 4):
        for state in ("ON", "OFF"):
            result = cmd_switch(bank, state)
            assert result == f"!SWITCH {bank} {state}\r"


def test_cmd_switch_accepts_enum_values() -> None:
    assert cmd_switch(BankNumber.BANK3, BankState.ON) == "!SWITCH 3 ON\r"
    assert cmd_switch(BankNumber.BANK4, BankState.OFF) == "!SWITCH 4 OFF\r"


def test_cmd_set_batthresh_bank3() -> None:
    assert cmd_set_batthresh(3, 50) == "!SET_BATTHRESH 3 50\r"


def test_cmd_set_batthresh_bank4() -> None:
    assert cmd_set_batthresh(4, 100) == "!SET_BATTHRESH 4 100\r"


def test_cmd_set_batthresh_invalid_bank_raises() -> None:
    with pytest.raises(ValidationError):
        cmd_set_batthresh(1, 50)
    with pytest.raises(ValidationError):
        cmd_set_batthresh(2, 50)


def test_cmd_set_batthresh_invalid_level_raises() -> None:
    with pytest.raises(ValidationError):
        cmd_set_batthresh(3, 10)
    with pytest.raises(ValidationError):
        cmd_set_batthresh(4, 110)


def test_cmd_set_buzzer_on() -> None:
    assert cmd_set_buzzer("ON") == "!SET_BUZZER ON\r"


def test_cmd_set_buzzer_off() -> None:
    assert cmd_set_buzzer(BuzzerMode.OFF) == "!SET_BUZZER OFF\r"


def test_cmd_set_avr_modes() -> None:
    assert cmd_set_avr("OFF") == "!SET_AVR OFF\r"
    assert cmd_set_avr("STANDARD") == "!SET_AVR STANDARD\r"
    assert cmd_set_avr(AVRMode.SENSITIVE) == "!SET_AVR SENSITIVE\r"


def test_cmd_set_feedback() -> None:
    assert cmd_set_feedback("ON") == "!SET_FEEDBACK ON\r"
    assert cmd_set_feedback(FeedbackMode.OFF) == "!SET_FEEDBACK OFF\r"


def test_cmd_set_linefeed() -> None:
    assert cmd_set_linefeed("ON") == "!SET_LINEFEED ON\r"
    assert cmd_set_linefeed(LinefeedMode.OFF) == "!SET_LINEFEED OFF\r"


def test_cmd_set_bright_all_levels() -> None:
    assert cmd_set_bright("100") == "!SET_BRIGHT 100\r"
    assert cmd_set_bright("075") == "!SET_BRIGHT 075\r"
    assert cmd_set_bright("050") == "!SET_BRIGHT 050\r"
    assert cmd_set_bright(Brightness.B025) == "!SET_BRIGHT 025\r"


def test_cmd_set_scrollmode_all_values() -> None:
    assert cmd_set_scrollmode("5SEC") == "!SET_SCROLLMODE 5SEC\r"
    assert cmd_set_scrollmode("10SEC") == "!SET_SCROLLMODE 10SEC\r"
    assert cmd_set_scrollmode(ScrollMode.OFF) == "!SET_SCROLLMODE OFF\r"


def test_cmd_set_sleepmode_all_values() -> None:
    assert cmd_set_sleepmode("30SEC") == "!SET_SLEEPMODE 30SEC\r"
    assert cmd_set_sleepmode("60SEC") == "!SET_SLEEPMODE 60SEC\r"
    assert cmd_set_sleepmode(SleepMode.OFF) == "!SET_SLEEPMODE OFF\r"


def test_cmd_reset_all() -> None:
    assert cmd_reset_all() == "!RESET_ALL\r"


def test_cmd_set_normalvolt_all_values() -> None:
    assert cmd_set_normalvolt("220") == "!SET_NORMALVOLT 220\r"
    assert cmd_set_normalvolt("230") == "!SET_NORMALVOLT 230\r"
    assert cmd_set_normalvolt(NormalVolt.V240) == "!SET_NORMALVOLT 240\r"


# ──────────────────────────────────────────────────────────────────────
# Query builders
# ──────────────────────────────────────────────────────────────────────


def test_query_id() -> None:
    assert query_id() == "?ID\r"


def test_query_outletstat() -> None:
    assert query_outletstat() == "?OUTLETSTAT\r"


def test_query_powerstat() -> None:
    assert query_powerstat() == "?POWERSTAT\r"


def test_query_power() -> None:
    assert query_power() == "?POWER\r"


def test_query_current() -> None:
    assert query_current() == "?CURRENT\r"


def test_query_voltage() -> None:
    assert query_voltage() == "?VOLTAGE\r"


def test_query_loadstat() -> None:
    assert query_loadstat() == "?LOADSTAT\r"


def test_query_batterystat() -> None:
    assert query_batterystat() == "?BATTERYSTAT\r"


def test_query_battstate() -> None:
    assert query_battstate() == "?BATTSTATE\r"


def test_query_time() -> None:
    assert query_time() == "?TIME\r"


def test_query_list_config() -> None:
    assert query_list_config() == "?LIST_CONFIG\r"


def test_query_help() -> None:
    assert query_help() == "?HELP\r"


# ──────────────────────────────────────────────────────────────────────
# parse_line — bank status
# ──────────────────────────────────────────────────────────────────────


def test_parse_bank_on() -> None:
    r = parse_line("$BANK 1 = ON")
    assert isinstance(r, BankStatusResponse)
    assert r.bank == BankNumber.BANK1
    assert r.state == BankState.ON


def test_parse_bank_off() -> None:
    r = parse_line("$BANK 2 = OFF")
    assert isinstance(r, BankStatusResponse)
    assert r.bank == BankNumber.BANK2
    assert r.state == BankState.OFF


def test_parse_bank_all_numbers() -> None:
    for n in (1, 2, 3, 4):
        r = parse_line(f"$BANK {n} = ON")
        assert isinstance(r, BankStatusResponse)
        assert r.bank == BankNumber(n)


def test_parse_bank_no_space_variant() -> None:
    r = parse_line("$BANK3 = ON")
    assert isinstance(r, BankStatusResponse)
    assert r.bank == BankNumber.BANK3


def test_parse_bank_strips_cr_lf() -> None:
    r = parse_line("$BANK 1 = ON\r\n")
    assert isinstance(r, BankStatusResponse)
    assert r.state == BankState.ON


# ──────────────────────────────────────────────────────────────────────
# parse_line — power / sensor responses
# ──────────────────────────────────────────────────────────────────────


def test_parse_power_status_normal() -> None:
    r = parse_line("$PWR = NORMAL")
    assert isinstance(r, PowerStatusResponse)
    assert r.status == PowerStatus.NORMAL


def test_parse_power_status_all_values() -> None:
    for status in PowerStatus:
        r = parse_line(f"$PWR = {status.value}")
        assert isinstance(r, PowerStatusResponse)
        assert r.status == status


def test_parse_battery_level() -> None:
    r = parse_line("$BATTERY = 85")
    assert isinstance(r, BatteryLevelResponse)
    assert r.level == 85


def test_parse_battery_level_zero() -> None:
    r = parse_line("$BATTERY = 0")
    assert isinstance(r, BatteryLevelResponse)
    assert r.level == 0


def test_parse_volts_in() -> None:
    r = parse_line("$VOLTS_IN = 230.0")
    assert isinstance(r, VoltsInResponse)
    assert r.volts == pytest.approx(230.0)


def test_parse_volts_out() -> None:
    r = parse_line("$VOLTS_OUT = 228.5")
    assert isinstance(r, VoltsOutResponse)
    assert r.volts == pytest.approx(228.5)


def test_parse_watts() -> None:
    r = parse_line("$WATTS = 150.0")
    assert isinstance(r, WattsResponse)
    assert r.watts == pytest.approx(150.0)


def test_parse_current() -> None:
    r = parse_line("$CURRENT = 0.65")
    assert isinstance(r, CurrentResponse)
    assert r.amps == pytest.approx(0.65)


def test_parse_voltage() -> None:
    r = parse_line("$VOLTAGE = 230.0")
    assert isinstance(r, VoltageResponse)
    assert r.volts == pytest.approx(230.0)


def test_parse_load() -> None:
    r = parse_line("$LOAD = 10.0")
    assert isinstance(r, LoadResponse)
    assert r.percent == pytest.approx(10.0)


# ──────────────────────────────────────────────────────────────────────
# parse_line — battery / backup responses
# ──────────────────────────────────────────────────────────────────────


def test_parse_bthresh() -> None:
    r = parse_line("$BTHRESH 3 = 20")
    assert isinstance(r, BatteryThresholdResponse)
    assert r.bank == BankNumber.BANK3
    assert r.level == 20


def test_parse_bthresh_real_device_compact_zero_padded() -> None:
    r = parse_line("$BTHRESH3=060")
    assert isinstance(r, BatteryThresholdResponse)
    assert r.bank == BankNumber.BANK3
    assert r.level == 60


def test_parse_bthresh_bank4() -> None:
    r = parse_line("$BTHRESH 4 = 80")
    assert isinstance(r, BatteryThresholdResponse)
    assert r.bank == BankNumber.BANK4
    assert r.level == 80


def test_parse_global_bthresh() -> None:
    r = parse_line("$BTHRESH = 80")
    assert isinstance(r, BatteryThresholdGlobalResponse)
    assert r.level == 80


def test_parse_low_battery() -> None:
    r = parse_line("$LOWBAT")
    assert isinstance(r, LowBatteryResponse)


def test_parse_battstate_full() -> None:
    r = parse_line("$BATTSTATE = FULL")
    assert isinstance(r, BatteryStateResponse)
    assert r.state == BatteryChargeState.FULL


def test_parse_battstate_charge() -> None:
    r = parse_line("$BATTSTATE = CHARGE")
    assert isinstance(r, BatteryStateResponse)
    assert r.state == BatteryChargeState.CHARGE


def test_parse_battstate_discharge() -> None:
    r = parse_line("$BATTSTATE = DISCHARGE")
    assert isinstance(r, BatteryStateResponse)
    assert r.state == BatteryChargeState.DISCHARGE


def test_parse_backup_time() -> None:
    r = parse_line("$TIME = 60")
    assert isinstance(r, BackupTimeResponse)
    assert r.minutes == 60


# ──────────────────────────────────────────────────────────────────────
# parse_line — configuration responses
# ──────────────────────────────────────────────────────────────────────


def test_parse_buzzer_on() -> None:
    r = parse_line("$BUZZER = ON")
    assert isinstance(r, BuzzerResponse)
    assert r.mode == BuzzerMode.ON


def test_parse_buzzer_off() -> None:
    r = parse_line("$BUZZER = OFF")
    assert isinstance(r, BuzzerResponse)
    assert r.mode == BuzzerMode.OFF


def test_parse_avr_mode_all_values() -> None:
    for mode in AVRMode:
        r = parse_line(f"$AVR = {mode.value}")
        assert isinstance(r, AVRModeResponse)
        assert r.mode == mode


def test_parse_feedback() -> None:
    r = parse_line("$FEEDBACK = ON")
    assert isinstance(r, FeedbackResponse)
    assert r.mode == FeedbackMode.ON


def test_parse_linefeed() -> None:
    r = parse_line("$LINEFEED = OFF")
    assert isinstance(r, LinefeedResponse)
    assert r.mode == LinefeedMode.OFF


def test_parse_linefeed_on_real_device_omits_dollar() -> None:
    r = parse_line("LINEFEED=ON")
    assert isinstance(r, LinefeedResponse)
    assert r.mode == LinefeedMode.ON


def test_parse_brightness_all_levels() -> None:
    for level in Brightness:
        r = parse_line(f"$BRIGHTNESS = {level.value}")
        assert isinstance(r, BrightnessResponse)
        assert r.level == level


def test_parse_scroll_mode_all_values() -> None:
    for mode in ScrollMode:
        r = parse_line(f"$SCROLL_MODE = {mode.value}")
        assert isinstance(r, ScrollModeResponse)
        assert r.mode == mode


def test_parse_sleep_mode_all_values() -> None:
    for mode in SleepMode:
        r = parse_line(f"$SLEEP_MODE = {mode.value}")
        assert isinstance(r, SleepModeResponse)
        assert r.mode == mode


def test_parse_normalvolt_all_values() -> None:
    for v in NormalVolt:
        r = parse_line(f"$NORMALVOLT = {v.value}")
        assert isinstance(r, NormalVoltResponse)
        assert r.voltage == v


def test_parse_factory_reset() -> None:
    r = parse_line("$FACTORY SETTINGS RESTORED")
    assert isinstance(r, FactoryResetResponse)
    assert r.restored is True


def test_parse_invalid_parameter() -> None:
    r = parse_line("$INVALID_PARAMETER")
    assert isinstance(r, InvalidParameterResponse)


# ──────────────────────────────────────────────────────────────────────
# parse_line — AVR state and button
# ──────────────────────────────────────────────────────────────────────


def test_parse_avrstate_boost() -> None:
    r = parse_line("$AVRSTATE = BOOST")
    assert isinstance(r, AVRStateResponse)
    assert r.state == AVRState.BOOST


def test_parse_avrstate_buck() -> None:
    r = parse_line("$AVRSTATE = BUCK")
    assert isinstance(r, AVRStateResponse)
    assert r.state == AVRState.BUCK


def test_parse_button_on() -> None:
    r = parse_line("$BUTTON = ON")
    assert isinstance(r, ButtonResponse)
    assert r.state == ButtonState.ON


def test_parse_button_off() -> None:
    r = parse_line("$BUTTON = OFF")
    assert isinstance(r, ButtonResponse)
    assert r.state == ButtonState.OFF


# ──────────────────────────────────────────────────────────────────────
# parse_line — raw / unrecognised responses
# ──────────────────────────────────────────────────────────────────────


def test_parse_id_manufacturer_line() -> None:
    r = parse_line("$Furman")
    assert isinstance(r, IDLineResponse)
    assert r.text == "Furman"


def test_parse_id_model_line() -> None:
    r = parse_line("$F1500-UPS E")
    assert isinstance(r, IDLineResponse)
    assert r.text == "F1500-UPS E"


def test_parse_id_firmware_line() -> None:
    r = parse_line("$FW1.00 (Emulator)")
    assert isinstance(r, IDLineResponse)
    assert r.text == "FW1.00 (Emulator)"


def test_parse_help_command_line() -> None:
    r = parse_line("!ALL_ON")
    assert isinstance(r, RawResponse)
    assert r.raw == "!ALL_ON"


def test_parse_empty_line() -> None:
    r = parse_line("")
    assert isinstance(r, RawResponse)
    assert r.raw == ""


def test_parse_completely_unknown() -> None:
    r = parse_line("SOMETHING_UNKNOWN")
    assert isinstance(r, RawResponse)


# ──────────────────────────────────────────────────────────────────────
# FakeTransport basics
# ──────────────────────────────────────────────────────────────────────


def _open_fake(*responses: str) -> FakeTransport:
    t = FakeTransport()
    t.open()
    t.enqueue_responses(list(responses))
    return t


def test_fake_transport_write_and_read() -> None:
    t = _open_fake("$BANK 1 = ON")
    t.write("!ALL_ON\r")
    assert t.last_command == "!ALL_ON\r"
    assert t.read_line() == "$BANK 1 = ON"


def test_fake_transport_raises_timeout_when_empty() -> None:
    t = _open_fake()
    with pytest.raises(JuicerTimeoutError):
        t.read_line()


def test_fake_transport_raises_when_not_open() -> None:
    t = FakeTransport()
    with pytest.raises(TransportError):
        t.write("!ALL_ON\r")
    with pytest.raises(TransportError):
        t.read_line()


def test_fake_transport_clear_resets_state() -> None:
    t = _open_fake("$BANK 1 = ON")
    t.write("!ALL_ON\r")
    t.clear()
    assert t.written == []
    with pytest.raises(JuicerTimeoutError):
        t.read_line()


def test_fake_transport_context_manager() -> None:
    t = FakeTransport()
    assert not t.is_open
    with t:
        assert t.is_open
    assert not t.is_open


# ──────────────────────────────────────────────────────────────────────
# JuicerClient — action commands
# ──────────────────────────────────────────────────────────────────────


def test_client_all_on() -> None:
    t = _open_fake(
        "$BANK 1 = ON", "$BANK 2 = ON", "$BANK 3 = ON", "$BANK 4 = ON"
    )
    client = JuicerClient(t)
    result = client.all_on()
    assert t.last_command == "!ALL_ON\r"
    assert len(result) == 4
    assert all(isinstance(r, BankStatusResponse) for r in result)
    assert all(r.state == BankState.ON for r in result)


def test_client_all_on_real_firmware_includes_button_and_prompt() -> None:
    """Real firmware !ALL_ON: $BANK1-4=ON then $BUTTON=ON then '>'."""
    t = FakeTransport()
    t.open()
    t.enqueue_responses(["$BANK1=ON", "$BANK2=ON", "$BANK3=ON", "$BANK4=ON", "$BUTTON=ON"])
    t.enqueue_prompt()
    client = JuicerClient(t)
    result = client.all_on()
    bank_results = [r for r in result if isinstance(r, BankStatusResponse)]
    button_results = [r for r in result if isinstance(r, ButtonResponse)]
    assert len(bank_results) == 4
    assert all(r.state == BankState.ON for r in bank_results)
    assert len(button_results) == 1


def test_client_all_off() -> None:
    t = _open_fake(
        "$BANK 1 = OFF", "$BANK 2 = OFF", "$BANK 3 = OFF", "$BANK 4 = OFF"
    )
    client = JuicerClient(t)
    result = client.all_off()
    assert t.last_command == "!ALL_OFF\r"
    assert all(r.state == BankState.OFF for r in result)


def test_client_all_off_real_firmware_includes_button_and_prompt() -> None:
    """Real firmware !ALL_OFF: $BANK1-4=OFF then $BUTTON=ON then '>'."""
    t = FakeTransport()
    t.open()
    t.enqueue_responses(["$BANK1=OFF", "$BANK2=OFF", "$BANK3=OFF", "$BANK4=OFF", "$BUTTON=ON"])
    t.enqueue_prompt()
    client = JuicerClient(t)
    result = client.all_off()
    bank_results = [r for r in result if isinstance(r, BankStatusResponse)]
    button_results = [r for r in result if isinstance(r, ButtonResponse)]
    assert all(r.state == BankState.OFF for r in bank_results)
    assert len(button_results) == 1


def test_client_switch_on() -> None:
    t = _open_fake("$BANK 2 = ON")
    client = JuicerClient(t)
    result = client.switch(2, "ON")
    assert t.last_command == "!SWITCH 2 ON\r"
    assert isinstance(result[0], BankStatusResponse)
    assert result[0].bank == BankNumber.BANK2
    assert result[0].state == BankState.ON


def test_client_switch_reads_only_expected_bank_response() -> None:
    t = _CountingFakeTransport()
    t.open()
    t.enqueue_responses(["$BANK 2 = ON", "$BANK 3 = ON"])
    client = JuicerClient(t)

    result = client.switch(2, "ON")

    assert t.read_count == 1
    assert isinstance(result[0], BankStatusResponse)
    assert t.read_line() == "$BANK 3 = ON"


def test_client_switch_rejects_wrong_bank_response() -> None:
    t = _open_fake("$BANK 3 = ON")
    client = JuicerClient(t)

    with pytest.raises(ProtocolError, match="expected bank 2"):
        client.switch(2, "ON")


def test_client_switch_invalid_parameter_raises_protocol_error() -> None:
    t = _open_fake("$INVALID_PARAMETER")
    client = JuicerClient(t)

    with pytest.raises(ProtocolError, match=r"\$INVALID_PARAMETER"):
        client.switch(2, "ON")


def test_client_switch_off() -> None:
    t = _open_fake("$BANK 3 = OFF")
    client = JuicerClient(t)
    result = client.switch(BankNumber.BANK3, BankState.OFF)
    assert t.last_command == "!SWITCH 3 OFF\r"
    assert result[0].state == BankState.OFF


def test_client_set_batthresh() -> None:
    t = _open_fake("$BTHRESH 3 = 50")
    client = JuicerClient(t)
    result = client.set_batthresh(3, 50)
    assert t.last_command == "!SET_BATTHRESH 3 50\r"
    assert isinstance(result[0], BatteryThresholdResponse)
    assert result[0].level == 50


def test_client_set_batthresh_rounded() -> None:
    t = _open_fake("$BTHRESH 4 = 60")
    client = JuicerClient(t)
    result = client.set_batthresh(4, 55)
    assert t.last_command == "!SET_BATTHRESH 4 55\r"
    assert isinstance(result[0], BatteryThresholdResponse)
    assert result[0].level == 60  # firmware rounds up to nearest 10


def test_client_set_buzzer_on() -> None:
    t = _open_fake("$BUZZER = ON")
    client = JuicerClient(t)
    result = client.set_buzzer("ON")
    assert t.last_command == "!SET_BUZZER ON\r"
    assert isinstance(result[0], BuzzerResponse)
    assert result[0].mode == BuzzerMode.ON


def test_client_set_buzzer_off() -> None:
    t = _open_fake("$BUZZER = OFF")
    client = JuicerClient(t)
    result = client.set_buzzer(BuzzerMode.OFF)
    assert result[0].mode == BuzzerMode.OFF


def test_client_set_avr_standard() -> None:
    t = _open_fake("$AVR = STANDARD")
    client = JuicerClient(t)
    result = client.set_avr("STANDARD")
    assert t.last_command == "!SET_AVR STANDARD\r"
    assert isinstance(result[0], AVRModeResponse)
    assert result[0].mode == AVRMode.STANDARD


def test_client_set_avr_all_modes() -> None:
    for mode in AVRMode:
        t = _open_fake(f"$AVR = {mode.value}")
        client = JuicerClient(t)
        result = client.set_avr(mode)
        assert t.last_command == f"!SET_AVR {mode.value}\r"
        assert isinstance(result[0], AVRModeResponse)


def test_client_set_feedback_on() -> None:
    t = _open_fake("$FEEDBACK = ON")
    client = JuicerClient(t)
    result = client.set_feedback("ON")
    assert t.last_command == "!SET_FEEDBACK ON\r"
    assert isinstance(result[0], FeedbackResponse)


def test_client_set_feedback_off_accepts_real_device_no_response() -> None:
    t = _open_fake()
    client = JuicerClient(t)
    result = client.set_feedback("OFF")
    assert t.last_command == "!SET_FEEDBACK OFF\r"
    assert result == []


def test_client_set_feedback_off_accepts_prompt_only() -> None:
    """Real firmware: !SET_FEEDBACK OFF → '>' (ready prompt only)."""
    t = FakeTransport()
    t.open()
    t.enqueue_prompt()
    client = JuicerClient(t)
    result = client.set_feedback("OFF")
    assert result == []


def test_client_set_linefeed_off() -> None:
    t = _open_fake("$LINEFEED = OFF")
    client = JuicerClient(t)
    result = client.set_linefeed("OFF")
    assert t.last_command == "!SET_LINEFEED OFF\r"
    assert isinstance(result[0], LinefeedResponse)
    assert result[0].mode == LinefeedMode.OFF


def test_client_set_linefeed_prompt_only() -> None:
    """Real firmware: !SET_LINEFEED ON/OFF → only '>' prompt (no data line)."""
    t = FakeTransport()
    t.open()
    t.enqueue_prompt()
    client = JuicerClient(t)
    result = client.set_linefeed("ON")
    assert t.last_command == "!SET_LINEFEED ON\r"
    assert result == []


def test_client_set_bright_075() -> None:
    t = _open_fake("$BRIGHTNESS = 075")
    client = JuicerClient(t)
    result = client.set_bright("075")
    assert t.last_command == "!SET_BRIGHT 075\r"
    assert isinstance(result[0], BrightnessResponse)
    assert result[0].level == Brightness.B075


def test_client_set_bright_all_levels() -> None:
    for level in Brightness:
        t = _open_fake(f"$BRIGHTNESS = {level.value}")
        client = JuicerClient(t)
        client.set_bright(level)
        assert t.last_command == f"!SET_BRIGHT {level.value}\r"


def test_client_set_bright_prompt_only() -> None:
    """Real firmware: !SET_BRIGHT → only '>' prompt (no data line)."""
    t = FakeTransport()
    t.open()
    t.enqueue_prompt()
    client = JuicerClient(t)
    result = client.set_bright("100")
    assert result == []


def test_client_set_scrollmode_10sec() -> None:
    t = _open_fake("$SCROLL_MODE = 10SEC")
    client = JuicerClient(t)
    result = client.set_scrollmode("10SEC")
    assert t.last_command == "!SET_SCROLLMODE 10SEC\r"
    assert isinstance(result[0], ScrollModeResponse)
    assert result[0].mode == ScrollMode.SEC10


def test_client_set_scrollmode_prompt_only() -> None:
    """Real firmware: !SET_SCROLLMODE → only '>' prompt (no data line)."""
    t = FakeTransport()
    t.open()
    t.enqueue_prompt()
    client = JuicerClient(t)
    result = client.set_scrollmode("OFF")
    assert result == []


def test_client_set_sleepmode_30sec() -> None:
    t = _open_fake("$SLEEP_MODE = 30SEC")
    client = JuicerClient(t)
    result = client.set_sleepmode("30SEC")
    assert t.last_command == "!SET_SLEEPMODE 30SEC\r"
    assert isinstance(result[0], SleepModeResponse)
    assert result[0].mode == SleepMode.SEC30


def test_client_set_sleepmode_prompt_only() -> None:
    """Real firmware: !SET_SLEEPMODE → only '>' prompt (no data line)."""
    t = FakeTransport()
    t.open()
    t.enqueue_prompt()
    client = JuicerClient(t)
    result = client.set_sleepmode("OFF")
    assert result == []



def test_client_reset_all() -> None:
    t = _open_fake("$FACTORY SETTINGS RESTORED")
    client = JuicerClient(t)
    result = client.reset_all()
    assert t.last_command == "!RESET_ALL\r"
    assert isinstance(result, FactoryResetResponse)
    assert result.restored is True


def test_client_reset_all_wrong_response_raises() -> None:
    t = _open_fake("$INVALID_PARAMETER")
    client = JuicerClient(t)
    with pytest.raises(ProtocolError):
        client.reset_all()


def test_client_set_normalvolt_220() -> None:
    t = _open_fake("$NORMALVOLT = 220")
    client = JuicerClient(t)
    result = client.set_normalvolt("220")
    assert t.last_command == "!SET_NORMALVOLT 220\r"
    assert isinstance(result[0], NormalVoltResponse)
    assert result[0].voltage == NormalVolt.V220


def test_client_set_normalvolt_all_values() -> None:
    for v in NormalVolt:
        t = _open_fake(f"$NORMALVOLT = {v.value}")
        client = JuicerClient(t)
        client.set_normalvolt(v)
        assert t.last_command == f"!SET_NORMALVOLT {v.value}\r"


def test_client_invalid_command_returns_invalid_parameter() -> None:
    t = _open_fake("$INVALID_PARAMETER")
    client = JuicerClient(t)
    # Simulate the device rejecting a bad command by reading one response
    resp = client._recv_parsed()
    assert isinstance(resp, InvalidParameterResponse)


# ──────────────────────────────────────────────────────────────────────
# JuicerClient — queries
# ──────────────────────────────────────────────────────────────────────


def test_client_query_id() -> None:
    t = _open_fake("$Furman", "$F1500-UPS E", "$FW1.00 (Emulator)")
    client = JuicerClient(t)
    result = client.query_id()
    assert t.last_command == "?ID\r"
    assert result.manufacturer == "Furman"
    assert result.model == "F1500-UPS E"
    assert result.firmware == "FW1.00 (Emulator)"


def test_client_query_outlet_status() -> None:
    t = _open_fake(
        "$BANK 1 = ON",
        "$BANK 2 = OFF",
        "$BANK 3 = ON",
        "$BANK 4 = OFF",
    )
    client = JuicerClient(t)
    result = client.query_outlet_status()
    assert t.last_command == "?OUTLETSTAT\r"
    assert result.banks[1] == BankState.ON
    assert result.banks[2] == BankState.OFF
    assert result.banks[3] == BankState.ON
    assert result.banks[4] == BankState.OFF


def test_client_query_outlet_status_wrong_response_raises() -> None:
    t = _open_fake(
        "$BANK 1 = ON",
        "$BANK 2 = OFF",
        "$INVALID_PARAMETER",
        "$BANK 4 = OFF",
    )
    client = JuicerClient(t)
    with pytest.raises(ProtocolError, match=r"\?OUTLETSTAT"):
        client.query_outlet_status()


def test_client_query_outlet_status_missing_bank_raises() -> None:
    t = _open_fake(
        "$BANK 1 = ON",
        "$BANK 2 = OFF",
        "$BANK 2 = ON",
        "$BANK 4 = OFF",
    )
    client = JuicerClient(t)
    with pytest.raises(ProtocolError, match="3"):
        client.query_outlet_status()


def test_client_query_power_status() -> None:
    t = _open_fake("$PWR = NORMAL")
    client = JuicerClient(t)
    result = client.query_power_status()
    assert t.last_command == "?POWERSTAT\r"
    assert result.status == PowerStatus.NORMAL


def test_client_query_power_status_wrong_response_raises() -> None:
    t = _open_fake("$INVALID_PARAMETER")
    client = JuicerClient(t)
    with pytest.raises(ProtocolError):
        client.query_power_status()


def test_client_query_power() -> None:
    t = _open_fake(
        "$VOLTS_IN = 230.0",
        "$VOLTS_OUT = 230.0",
        "$WATTS = 150.0",
        "$CURRENT = 0.65",
    )
    client = JuicerClient(t)
    result = client.query_power()
    assert t.last_command == "?POWER\r"
    assert result.volts_in == pytest.approx(230.0)
    assert result.volts_out == pytest.approx(230.0)
    assert result.watts == pytest.approx(150.0)
    assert result.current == pytest.approx(0.65)


def test_client_query_power_missing_field_raises() -> None:
    t = _open_fake(
        "$VOLTS_IN = 230.0",
        "$VOLTS_OUT = 230.0",
        "$WATTS = 150.0",
        "$WATTS = 200.0",
    )
    client = JuicerClient(t)
    with pytest.raises(ProtocolError, match="current"):
        client.query_power()


def test_client_query_power_wrong_response_raises() -> None:
    t = _open_fake(
        "$VOLTS_IN = 230.0",
        "$VOLTS_OUT = 230.0",
        "$WATTS = 150.0",
        "$INVALID_PARAMETER",
    )
    client = JuicerClient(t)
    with pytest.raises(ProtocolError, match=r"\?POWER"):
        client.query_power()


def test_client_query_current() -> None:
    t = _open_fake("$CURRENT = 0.65")
    client = JuicerClient(t)
    result = client.query_current()
    assert t.last_command == "?CURRENT\r"
    assert isinstance(result, CurrentResponse)
    assert result.amps == pytest.approx(0.65)


def test_client_query_current_wrong_response_raises() -> None:
    t = _open_fake("$INVALID_PARAMETER")
    client = JuicerClient(t)
    with pytest.raises(ProtocolError):
        client.query_current()


def test_client_query_voltage() -> None:
    t = _open_fake("$VOLTAGE = 230.0")
    client = JuicerClient(t)
    result = client.query_voltage()
    assert t.last_command == "?VOLTAGE\r"
    assert isinstance(result, VoltageResponse)
    assert result.volts == pytest.approx(230.0)


def test_client_query_voltage_accepts_real_device_volts_in_response() -> None:
    t = _open_fake("$VOLTS_IN=120")
    client = JuicerClient(t)
    result = client.query_voltage()
    assert t.last_command == "?VOLTAGE\r"
    assert isinstance(result, VoltageResponse)
    assert result.volts == pytest.approx(120.0)


def test_client_query_voltage_wrong_response_raises() -> None:
    t = _open_fake("$INVALID_PARAMETER")
    client = JuicerClient(t)
    with pytest.raises(ProtocolError):
        client.query_voltage()


def test_client_query_loadstat() -> None:
    t = _open_fake("$LOAD = 10.0")
    client = JuicerClient(t)
    result = client.query_loadstat()
    assert t.last_command == "?LOADSTAT\r"
    assert isinstance(result, LoadResponse)
    assert result.percent == pytest.approx(10.0)


def test_client_query_loadstat_wrong_response_raises() -> None:
    t = _open_fake("$INVALID_PARAMETER")
    client = JuicerClient(t)
    with pytest.raises(ProtocolError):
        client.query_loadstat()


def test_client_query_battery_status() -> None:
    t = _open_fake("$BATTERY = 85")
    client = JuicerClient(t)
    result = client.query_battery_status()
    assert t.last_command == "?BATTERYSTAT\r"
    assert isinstance(result, BatteryLevelResponse)
    assert result.level == 85


def test_client_query_battery_status_wrong_response_raises() -> None:
    t = _open_fake("$INVALID_PARAMETER")
    client = JuicerClient(t)
    with pytest.raises(ProtocolError):
        client.query_battery_status()


def test_client_query_battery_state_full() -> None:
    t = _open_fake("$BATTSTATE = FULL")
    client = JuicerClient(t)
    result = client.query_battery_state()
    assert t.last_command == "?BATTSTATE\r"
    assert isinstance(result, BatteryStateResponse)
    assert result.state == BatteryChargeState.FULL


def test_client_query_battery_state_charge() -> None:
    t = _open_fake("$BATTSTATE = CHARGE")
    client = JuicerClient(t)
    result = client.query_battery_state()
    assert result.state == BatteryChargeState.CHARGE


def test_client_query_battery_state_discharge() -> None:
    t = _open_fake("$BATTSTATE = DISCHARGE")
    client = JuicerClient(t)
    result = client.query_battery_state()
    assert result.state == BatteryChargeState.DISCHARGE


def test_client_query_battery_state_wrong_response_raises() -> None:
    t = _open_fake("$INVALID_PARAMETER")
    client = JuicerClient(t)
    with pytest.raises(ProtocolError):
        client.query_battery_state()


def test_client_query_backup_time() -> None:
    t = _open_fake("$TIME = 60")
    client = JuicerClient(t)
    result = client.query_backup_time()
    assert t.last_command == "?TIME\r"
    assert isinstance(result, BackupTimeResponse)
    assert result.minutes == 60


def test_client_query_backup_time_wrong_response_raises() -> None:
    t = _open_fake("$INVALID_PARAMETER")
    client = JuicerClient(t)
    with pytest.raises(ProtocolError):
        client.query_backup_time()


def test_client_query_list_config_defaults() -> None:
    t = _open_fake(
        "$BUZZER = ON",
        "$AVR = OFF",
        "$FEEDBACK = ON",
        "$LINEFEED = OFF",
        "$BRIGHTNESS = 100",
        "$SCROLL_MODE = 5SEC",
        "$SLEEP_MODE = OFF",
        "$NORMALVOLT = 230",
        "$BTHRESH 3 = 20",
        "$BTHRESH 4 = 20",
    )
    client = JuicerClient(t)
    result = client.query_list_config()
    assert t.last_command == "?LIST_CONFIG\r"
    assert result.buzzer == BuzzerMode.ON
    assert result.avr == AVRMode.OFF
    assert result.feedback == FeedbackMode.ON
    assert result.linefeed == LinefeedMode.OFF
    assert result.brightness == Brightness.B100
    assert result.scroll_mode == ScrollMode.SEC5
    assert result.sleep_mode == SleepMode.OFF
    assert result.normalvolt == NormalVolt.V230
    assert result.bthresh == 20  # last BTHRESH seen (bank 4)
    assert result.bthresh3 == 20
    assert result.bthresh4 == 20


def test_client_query_list_config_real_device_format() -> None:
    t = _open_fake(
        "$BTHRESH3=060",
        "$BTHRESH4=040",
        "$BUZZER=OFF",
        "$AVR=STANDARD",
        "$FEEDBACK=ON",
        "$LINEFEED=OFF",
        "$BRIGHTNESS=100",
        "$SCROLL_MODE=OFF",
        "$SLEEP_MODE=OFF",
    )
    client = JuicerClient(t)
    result = client.query_list_config()
    assert t.last_command == "?LIST_CONFIG\r"
    assert result.bthresh == 40
    assert result.bthresh3 == 60
    assert result.bthresh4 == 40
    assert result.buzzer == BuzzerMode.OFF
    assert result.avr == AVRMode.STANDARD
    assert result.feedback == FeedbackMode.ON
    assert result.linefeed == LinefeedMode.OFF
    assert result.brightness == Brightness.B100
    assert result.scroll_mode == ScrollMode.OFF
    assert result.sleep_mode == SleepMode.OFF


def test_client_query_help_returns_command_list() -> None:
    help_lines = [
        "!ALL_ON",
        "!ALL_OFF",
        "!SWITCH",
        "!SET_BATTHRESH",
        "!SET_BUZZER",
        "!SET_AVR",
        "!SET_FEEDBACK",
        "!SET_LINEFEED",
        "!RESET_ALL",
        "!SET_BRIGHT",
        "!SET_SCROLLMODE",
        "!SET_SLEEPMODE",
        "?ID",
        "?OUTLETSTAT",
        "?POWERSTAT",
        "?POWER",
        "?CURRENT",
        "?VOLTAGE",
        "?LOADSTAT",
        "?BATTERYSTAT",
        "?LIST_CONFIG",
        "?HELP",
    ]
    t = _open_fake(*help_lines)
    client = JuicerClient(t)
    result = client.query_help()
    assert t.last_command == "?HELP\r"
    assert "!ALL_ON" in result
    assert "?ID" in result
    assert "?BATTSTATE" not in result
    assert "?TIME" not in result
    assert len(result) == len(help_lines)


# ──────────────────────────────────────────────────────────────────────
# End-to-end stateful emulation: sequence of commands via FakeTransport
# ──────────────────────────────────────────────────────────────────────


def test_stateful_all_on_then_switch_off() -> None:
    """Simulate all-on followed by switching a single bank off."""
    t = FakeTransport()
    t.open()
    client = JuicerClient(t)

    # Enqueue responses for all_on; _recv_until_timeout drains them then stops.
    t.enqueue_responses(["$BANK 1 = ON", "$BANK 2 = ON", "$BANK 3 = ON", "$BANK 4 = ON"])
    on_result = client.all_on()
    assert all(r.state == BankState.ON for r in on_result)

    # Enqueue the switch response after all_on has consumed its responses.
    t.enqueue_response("$BANK 2 = OFF")
    off_result = client.switch(2, "OFF")
    assert off_result[0].bank == BankNumber.BANK2
    assert off_result[0].state == BankState.OFF

    assert t.written == ["!ALL_ON\r", "!SWITCH 2 OFF\r"]


def test_stateful_configure_then_reset() -> None:
    """Set several config options then reset to factory defaults."""
    t = FakeTransport()
    t.open()
    client = JuicerClient(t)

    t.enqueue_response("$BUZZER = OFF")
    buz = client.set_buzzer("OFF")
    assert isinstance(buz[0], BuzzerResponse)

    t.enqueue_response("$AVR = STANDARD")
    avr = client.set_avr("STANDARD")
    assert isinstance(avr[0], AVRModeResponse)

    t.enqueue_response("$BRIGHTNESS = 075")
    bri = client.set_bright("075")
    assert isinstance(bri[0], BrightnessResponse)

    t.enqueue_response("$FACTORY SETTINGS RESTORED")
    reset = client.reset_all()
    assert isinstance(reset, FactoryResetResponse)

    assert t.written == [
        "!SET_BUZZER OFF\r",
        "!SET_AVR STANDARD\r",
        "!SET_BRIGHT 075\r",
        "!RESET_ALL\r",
    ]


def test_stateful_battery_queries() -> None:
    """Query battery level, state, and backup time in sequence."""
    t = _open_fake(
        "$BATTERY = 85",
        "$BATTSTATE = FULL",
        "$TIME = 60",
    )
    client = JuicerClient(t)

    lvl = client.query_battery_status()
    state = client.query_battery_state()
    btime = client.query_backup_time()

    assert lvl.level == 85
    assert state.state == BatteryChargeState.FULL
    assert btime.minutes == 60
    assert t.written == ["?BATTERYSTAT\r", "?BATTSTATE\r", "?TIME\r"]
