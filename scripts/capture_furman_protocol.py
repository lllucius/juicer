#!/usr/bin/env python3
"""Capture raw Furman F1500-UPS serial responses for the full command set."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, TextIO

COMMANDS: tuple[str, ...] = (
    "!ALL_OFF",
    "!ALL_ON",
    "!SWITCH 1 ON",
    "!SWITCH 1 OFF",
    "!SWITCH 2 ON",
    "!SWITCH 2 OFF",
    "!SWITCH 3 ON",
    "!SWITCH 3 OFF",
    "!SWITCH 4 ON",
    "!SWITCH 4 OFF",
    "!SET_BATTHRESH 3 60",
    "!SET_BATTHRESH 4 40",
    "!SET_BUZZER ON",
    "!SET_BUZZER OFF",
    "!SET_AVR STANDARD",
    "!SET_AVR SENSITIVE",
    "!SET_AVR OFF",
    "!SET_FEEDBACK ON",
    "!SET_FEEDBACK OFF",
    "!SET_LINEFEED ON",
    "!SET_LINEFEED OFF",
    # Capture both observed brightness forms: unpadded and zero-padded.
    "!SET_BRIGHT 100",
    "!SET_BRIGHT 25",
    "!SET_BRIGHT 75",
    "!SET_BRIGHT 50",
    "!SET_BRIGHT 025",
    "!SET_SCROLLMODE OFF",
    "!SET_SCROLLMODE 5SEC",
    "!SET_SCROLLMODE 10SEC",
    "!SET_SLEEPMODE OFF",
    "!SET_SLEEPMODE 30SEC",
    "!SET_SLEEPMODE 60SEC",
    "!RESET_ALL",
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
)


class ReadablePort(Protocol):
    """Minimal serial-port interface used by the quiet-read loop."""

    def read(self, size: int = 1) -> bytes:
        """Read up to size bytes."""


class Tee:
    """Write capture output to stdout and, optionally, a file."""

    def __init__(self, *streams: TextIO) -> None:
        self._streams = streams

    def write(self, text: str = "") -> None:
        for stream in self._streams:
            print(text, file=stream)
            stream.flush()


def escaped(data: bytes) -> str:
    """Return printable ASCII with control bytes escaped."""
    return data.decode("ascii", errors="backslashreplace").replace("\r", "\\r").replace(
        "\n", "\\n"
    )


def read_until_quiet(serial_port: ReadablePort, quiet_timeout: float, max_wait: float) -> bytes:
    """Read bytes until the line is quiet or max_wait expires."""
    data = bytearray()
    deadline = time.monotonic() + max_wait
    quiet_deadline = time.monotonic() + quiet_timeout

    while time.monotonic() < deadline and time.monotonic() < quiet_deadline:
        chunk = serial_port.read(1)
        if chunk:
            data.extend(chunk)
            quiet_deadline = time.monotonic() + quiet_timeout

    return bytes(data)


def write_block(out: Tee, label: str, data: bytes) -> None:
    """Write one raw byte block in escaped and hexadecimal forms."""
    out.write(f"{label} ascii: {escaped(data)!r}")
    out.write(f"{label} hex:   {data.hex(' ')}")


def load_commands(path: Path | None) -> tuple[str, ...]:
    """Load command lines from a file, ignoring blanks and comments."""
    if path is None:
        return COMMANDS
    commands: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        command = line.strip()
        if command and not command.startswith("#"):
            commands.append(command)
    return tuple(commands)


def run_capture(args: argparse.Namespace, commands: Iterable[str], out: Tee) -> int:
    """Open the serial port, issue commands, and record raw bytes."""
    try:
        import serial
    except ImportError:
        out.write("error: pyserial is required; install with `python -m pip install pyserial`")
        return 1

    with serial.Serial(
        port=args.port,
        baudrate=args.baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=args.read_timeout,
        write_timeout=args.write_timeout,
    ) as serial_port:
        if args.flush_input:
            serial_port.reset_input_buffer()

        prelude = read_until_quiet(serial_port, args.quiet_timeout, args.initial_wait)
        if prelude:
            write_block(out, "RX prelude", prelude)

        for index, command in enumerate(commands, start=1):
            payload = f"{command}\r".encode("ascii")
            out.write()
            out.write(f"## {index:02d} {command}")
            write_block(out, "TX", payload)
            serial_port.write(payload)
            serial_port.flush()
            response = read_until_quiet(serial_port, args.quiet_timeout, args.command_timeout)
            write_block(out, "RX", response)

    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Issue Furman F1500-UPS commands and capture raw serial bytes, including "
            "prompt characters such as '>'."
        )
    )
    parser.add_argument(
        "--port",
        required=True,
        help="Serial port, for example COM3 or /dev/ttyUSB0",
    )
    parser.add_argument("--baudrate", type=int, default=9600, help="Serial baud rate")
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=0.05,
        help="Per-byte read timeout",
    )
    parser.add_argument(
        "--quiet-timeout",
        type=float,
        default=0.5,
        help="Quiet period ending a read",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=5.0,
        help="Max wait after a command",
    )
    parser.add_argument(
        "--initial-wait",
        type=float,
        default=1.0,
        help="Max wait for initial bytes",
    )
    parser.add_argument("--write-timeout", type=float, default=2.0, help="Serial write timeout")
    parser.add_argument("--output", type=Path, help="Optional file to also write the capture log")
    parser.add_argument("--commands", type=Path, help="Optional newline-delimited command file")
    parser.add_argument(
        "--flush-input",
        action="store_true",
        help="Clear queued input before reading the initial prelude",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required because this script switches outlets and resets device settings",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without opening serial",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    commands = load_commands(args.commands)

    streams: list[TextIO] = [sys.stdout]
    output_file: TextIO | None = None
    try:
        if args.output is not None:
            output_file = args.output.open("w", encoding="utf-8")
            streams.append(output_file)
        out = Tee(*streams)

        if args.dry_run:
            for command in commands:
                out.write(command)
            return 0

        if not args.yes:
            out.write(
                "error: this capture switches outlets and resets settings; rerun with --yes "
                "after confirming it is safe"
            )
            return 2

        return run_capture(args, commands, out)
    finally:
        if output_file is not None:
            output_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
