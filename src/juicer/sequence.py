"""Juicer boot/shutdown sequencer.

Replicates the C++ ``powercycle()`` logic:
  - Boot order: banks 1, 2, 3, 4
  - Shutdown order: banks 4, 3, 2, 1
  - Per bank: pre-delay → action → post-delay
  - Skip banks with no action configured
  - Optional sound at start/end of sequence
"""

from __future__ import annotations

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

    def switch(self, bank: int, state: str) -> object: ...


class CancelToken(Protocol):
    """Minimal cancellation interface accepted by the sequencer."""

    def is_set(self) -> bool: ...


# ──────────────────────────────────────────────────────────────────────
# Sound helper
# ──────────────────────────────────────────────────────────────────────


def _play_sound(path: str) -> None:
    """Play a WAV file.  Windows: ``PlaySound``.  Others: log a warning."""
    if not path:
        return
    if platform.system() == "Windows":
        try:
            import winsound

            winsound.PlaySound(path, winsound.SND_FILENAME)
            logger.info("Played sound: %s", path)
        except Exception as exc:
            logger.warning("Failed to play sound %s: %s", path, exc)
    else:
        logger.info("Sound playback skipped (not Windows): %s", path)


# ──────────────────────────────────────────────────────────────────────
# Sequence runner
# ──────────────────────────────────────────────────────────────────────


def _report_progress(progress_callback: ProgressCallback | None) -> None:
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
    start_sound: str = "",
    stop_sound: str = "",
    sleeper: Sleeper = time.sleep,
    cancel: CancelToken | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Execute a boot or shutdown sequence.

    Args:
        seq: Per-bank configuration (actions and delays).
        bank_order: Order in which to process banks (e.g. ``[1,2,3,4]``).
        client: Protocol client with a ``switch(bank, state)`` method.
        start_sound: WAV file path to play before the first bank action.
        stop_sound: WAV file path to play after the last bank action.
        sleeper: Callable for delays (injected for testing).
        cancel: Optional event-like object; if set, the sequence exits before the next step.
        progress_callback: Optional callback invoked during long-running startup work.
    """
    logger.info("Starting sequence, bank order: %s", bank_order)

    # Play start sound
    if start_sound:
        _report_progress(progress_callback)
        _play_sound(start_sound)
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
            # Do not play stop_sound here; callers own error cleanup.
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

    # Play stop sound
    if stop_sound:
        _report_progress(progress_callback)
        _play_sound(stop_sound)
        _report_progress(progress_callback)

    logger.info("Sequence complete")


def run_boot(
    config: GlobalConfig,
    client: SwitchClient,
    *,
    sleeper: Sleeper = time.sleep,
    cancel: CancelToken | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Run the boot sequence (banks 1→4) using ``config.boot``."""
    run_sequence(
        config.boot,
        BOOT_ORDER,
        client,
        start_sound=config.start_sound,
        stop_sound=config.stop_sound,
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
) -> None:
    """Run the shutdown sequence (banks 4→1) using ``config.shutdown``."""
    run_sequence(
        config.shutdown,
        SHUTDOWN_ORDER,
        client,
        start_sound=config.start_sound,
        stop_sound=config.stop_sound,
        sleeper=sleeper,
        cancel=cancel,
        progress_callback=progress_callback,
    )
