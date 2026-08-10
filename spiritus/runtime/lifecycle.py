"""Reusable, idempotent cleanup coordination for the desktop runtime."""
from __future__ import annotations

import threading
from collections.abc import Callable


class ShutdownCoordinator:
    """Run registered cleanup callbacks once, even across exit paths.

    Spiritus can observe a close through PyWebView, a ``finally`` block, or
    ``atexit``. Keeping those paths on one coordinator prevents duplicate
    teardown and makes adding another runtime-owned service safe.
    """

    def __init__(self) -> None:
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._stopped = False

    def add(self, callback: Callable[[], None]) -> None:
        """Register a callback, or run it immediately if shutdown already ran."""
        with self._lock:
            if not self._stopped:
                self._callbacks.append(callback)
                return
        callback()

    def stop_once(self) -> None:
        """Run callbacks in reverse registration order and swallow cleanup errors."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            callbacks = list(reversed(self._callbacks))
            self._callbacks.clear()

        for callback in callbacks:
            try:
                callback()
            except Exception:
                # One failed service must not prevent the remaining services
                # from being released during application shutdown.
                continue


__all__ = ["ShutdownCoordinator"]
