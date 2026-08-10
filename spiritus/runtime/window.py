"""Application-facing wrapper around a PyWebView window."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


class WindowController:
    """Expose stable window operations without leaking the PyWebView object."""

    def __init__(self, window: Any) -> None:
        self._window = window

    def show(self) -> None:
        self._window.show()

    def hide(self) -> None:
        self._window.hide()

    def minimize(self) -> None:
        self._window.minimize()

    def maximize(self) -> None:
        self._window.maximize()

    def restore(self) -> None:
        self._window.restore()

    def resize(self, width: int, height: int) -> None:
        self._window.resize(width, height)

    def move(self, x: int, y: int) -> None:
        self._window.move(x, y)

    def set_title(self, title: str) -> None:
        self._window.set_title(title)

    def toggle_fullscreen(self) -> None:
        self._window.toggle_fullscreen()

    def load_url(self, url: str) -> None:
        self._window.load_url(url)

    def evaluate_js(self, script: str) -> Any:
        return self._window.evaluate_js(script)

    def destroy(self) -> None:
        self._window.destroy()

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        """Subscribe to a documented PyWebView window event."""
        events = self._window.events
        signal = getattr(events, event)
        signal += callback
        setattr(events, event, signal)


__all__ = ["WindowController"]
