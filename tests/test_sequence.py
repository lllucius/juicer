from __future__ import annotations

from juicer.config import BankAction, BankConfig, SequenceConfig
from juicer.sequence import _sleep_with_progress, run_sequence


class _SwitchRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def switch(self, bank: int, state: str) -> object:
        self.events.append(f"switch:{bank}:{state}")
        return object()


def test_run_sequence_reports_progress_around_bank_switch() -> None:
    events: list[str] = []
    seq = SequenceConfig(
        bank1=BankConfig(action=BankAction.ON),
    )

    run_sequence(
        seq,
        [1],
        _SwitchRecorder(events),
        sleeper=lambda seconds: events.append(f"sleep:{seconds}"),
        progress_callback=lambda: events.append("progress"),
    )

    assert events == ["progress", "switch:1:ON", "progress"]


def test_run_sequence_splits_long_delays_with_progress() -> None:
    events: list[str] = []
    seq = SequenceConfig(
        bank1=BankConfig(action=BankAction.ON, pre_delay_ms=12_000),
    )

    run_sequence(
        seq,
        [1],
        _SwitchRecorder(events),
        sleeper=lambda seconds: events.append(f"sleep:{seconds:g}"),
        progress_callback=lambda: events.append("progress"),
    )

    assert events == [
        "progress",
        "sleep:5",
        "progress",
        "sleep:5",
        "progress",
        "sleep:2",
        "progress",
        "switch:1:ON",
        "progress",
    ]


def test_sleep_with_progress_uses_injected_sleeper_in_chunks() -> None:
    sleeps: list[float] = []
    progress_count = 0

    def progress() -> None:
        nonlocal progress_count
        progress_count += 1

    _sleep_with_progress(
        11.0,
        sleeps.append,
        progress,
        interval=4.0,
    )

    assert sleeps == [4.0, 4.0, 3.0]
    assert progress_count == 2
