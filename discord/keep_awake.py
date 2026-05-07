"""Prevent Windows from entering system sleep while bot is running.

Uses SetThreadExecutionState — display can still sleep, only the system
stays awake. The request is per-process: when the bot exits (cleanly or
crashes), Windows automatically reverts to normal power behaviour.

No-op on non-Windows platforms.
"""
from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

# Win32 ExecutionState flags (winbase.h)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """Context manager: prevent system sleep for the lifetime of the `with` block."""

    def __init__(self) -> None:
        self._active = False

    def __enter__(self) -> "KeepAwake":
        if sys.platform != "win32":
            log.info("keep_awake: non-Windows platform, skipping")
            return self
        try:
            prev = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            )
        except OSError as e:
            log.warning("keep_awake: SetThreadExecutionState failed (%s)", e)
            return self
        if prev == 0:
            log.warning("keep_awake: API returned 0 — sleep prevention not active")
            return self
        self._active = True
        log.info("keep_awake: system sleep prevented (display may still sleep)")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._active:
            return
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            log.info("keep_awake: released — normal sleep behaviour restored")
        except OSError as e:
            log.warning("keep_awake: release failed (%s)", e)
