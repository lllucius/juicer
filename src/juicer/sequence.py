"""Juicer boot/shutdown sequencer.

Replicates the C++ ``powercycle()`` logic:
  - Boot order: banks 1, 2, 3, 4
  - Shutdown order: banks 4, 3, 2, 1
  - Per bank: pre-delay → action → post-delay
  - Skip banks with no action configured
  - Optional audible cues for startup completion and shutdown start
"""

from __future__ import annotations

import importlib
import logging
import platform
import time
from typing import Callable, Protocol

from juicer.config import BankAction, GlobalConfig, SequenceConfig

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Type protocols for dependency injection
# ──────────────────────────────────────────────────────────────────────

BOOT_ORDER = [1, 2, 3, 4]
SHUTDOWN_ORDER = [4, 3, 2, 1]

Sleeper = Callable[[float], None]
"""Callable that sleeps for *n* seconds.  Default is ``time.sleep``."""

ProgressCallback = Callable[[], None]
"""Callable invoked to report sequence startup progress."""


class SwitchClient(Protocol):
    """Minimal interface needed by the sequencer — just switch a bank."""

    def switch(self, bank: int, state: str) -> object:
        """Apply the requested ON/OFF state to the specified outlet bank."""
        ...


class CancelToken(Protocol):
    """Minimal cancellation interface accepted by the sequencer."""

    def is_set(self) -> bool:
        """Report whether the caller has requested the active sequence to stop."""
        ...


def play_sound(path: str) -> None:
    """Play a WAV file on Windows, logging and continuing on failure."""
    if not path:
        return
    if platform.system() != "Windows":
        logger.info("Sound playback skipped (not Windows): %s", path)
        return
    try:
        winsound = importlib.import_module("winsound")
        winsound.PlaySound(path, winsound.SND_FILENAME)
        logger.info("Played sound: %s", path)
    except Exception as exc:
        logger.warning("Failed to play sound %s: %s", path, exc)


# ──────────────────────────────────────────────────────────────────────
# Sequence runner
# ──────────────────────────────────────────────────────────────────────


def _report_progress(progress_callback: ProgressCallback | None) -> None:
    """Invoke the optional progress callback when one has been provided."""
    if progress_callback is not None:
        progress_callback()


def _sleep_with_progress(
    total_seconds: float,
    sleeper: Sleeper,
    progress_callback: ProgressCallback | None,
    *,
    interval: float = 5.0,
) -> None:
    """Sleep in chunks, reporting progress between chunks."""
    if total_seconds <= 0:
        return
    if interval <= 0:
        raise ValueError("interval must be greater than zero")

    remaining = total_seconds
    while remaining > 0:
        chunk = min(interval, remaining)
        sleeper(chunk)
        remaining -= chunk
        if remaining > 0:
            _report_progress(progress_callback)


def run_sequence(
    seq: SequenceConfig,
    bank_order: list[int],
    client: SwitchClient,
    *,
    event_start_sound: str = "",
    event_stop_sound: str = "",
    play_event_sounds: bool = True,
    sleeper: Sleeper = time.sleep,
    cancel: CancelToken | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Execute a boot or shutdown sequence.

    Args:
        seq: Per-bank configuration (actions and delays).
        bank_order: Order in which to process banks (e.g. ``[1,2,3,4]``).
        client: Protocol client with a ``switch(bank, state)`` method.
        event_start_sound: WAV file path to play before the first bank action.
        event_stop_sound: WAV file path to play after the last bank action.
        play_event_sounds: False for non-interactive callers such as Windows services.
        sleeper: Callable for delays (injected for testing).
        cancel: Optional event-like object; if set, the sequence exits before the next step.
        progress_callback: Optional callback invoked during long-running startup work.
    """
    logger.info("Starting sequence, bank order: %s", bank_order)

    if play_event_sounds and event_start_sound:
        _report_progress(progress_callback)
        play_sound(event_start_sound)
        _report_progress(progress_callback)

    for bank_num in bank_order:
        if cancel is not None and cancel.is_set():
            logger.info("Sequence cancelled before next bank")
            return

        bank_cfg = seq.bank(bank_num)

        if bank_cfg.action is None:
            logger.info("Bank %d: no action configured, skipping", bank_num)
            continue

        state_str = "ON" if bank_cfg.action == BankAction.ON else "OFF"

        # Pre-delay
        if bank_cfg.pre_delay_ms > 0:
            delay_sec = bank_cfg.pre_delay_ms / 1000.0
            logger.info("Bank %d: pre-delay %d ms", bank_num, bank_cfg.pre_delay_ms)
            _report_progress(progress_callback)
            _sleep_with_progress(delay_sec, sleeper, progress_callback)
            if cancel is not None and cancel.is_set():
                logger.info("Sequence cancelled after pre-delay")
                return

        # Execute action
        logger.info("Bank %d: !SWITCH %d %s", bank_num, bank_num, state_str)
        try:
            _report_progress(progress_callback)
            client.switch(bank_num, state_str)
            _report_progress(progress_callback)
        except Exception as exc:
            logger.error("Bank %d: action failed: %s", bank_num, exc)
            raise

        # Post-delay
        if bank_cfg.post_delay_ms > 0:
            delay_sec = bank_cfg.post_delay_ms / 1000.0
            logger.info("Bank %d: post-delay %d ms", bank_num, bank_cfg.post_delay_ms)
            _report_progress(progress_callback)
            _sleep_with_progress(delay_sec, sleeper, progress_callback)
            if cancel is not None and cancel.is_set():
                logger.info("Sequence cancelled after post-delay")
                return

    if play_event_sounds and event_stop_sound:
        _report_progress(progress_callback)
        play_sound(event_stop_sound)
        _report_progress(progress_callback)

    logger.info("Sequence complete")


def run_boot(
    config: GlobalConfig,
    client: SwitchClient,
    *,
    sleeper: Sleeper = time.sleep,
    cancel: CancelToken | None = None,
    progress_callback: ProgressCallback | None = None,
    play_event_sounds: bool = True,
) -> None:
    """Run the boot sequence (banks 1→4) using ``config.boot``."""
    run_sequence(
        config.boot,
        BOOT_ORDER,
        client,
        event_stop_sound=config.boot.event_stop_sound,
        play_event_sounds=play_event_sounds,
        sleeper=sleeper,
        cancel=cancel,
        progress_callback=progress_callback,
    )


def run_shutdown(
    config: GlobalConfig,
    client: SwitchClient,
    *,
    sleeper: Sleeper = time.sleep,
    cancel: CancelToken | None = None,
    progress_callback: ProgressCallback | None = None,
    play_event_sounds: bool = True,
) -> None:
    """Run the shutdown sequence (banks 4→1) using ``config.shutdown``."""
    run_sequence(
        config.shutdown,
        SHUTDOWN_ORDER,
        client,
        event_start_sound=config.shutdown.event_start_sound,
        play_event_sounds=play_event_sounds,
        sleeper=sleeper,
        cancel=cancel,
        progress_callback=progress_callback,
    )
