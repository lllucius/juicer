from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from juicer.protocol import SerialTransport


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
