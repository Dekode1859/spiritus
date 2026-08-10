from __future__ import annotations

from types import SimpleNamespace

from spiritus import WebViewConfig, WindowConfig
from spiritus.runtime.window import WindowController


class Event:
    def __init__(self):
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self


class FakeWindow:
    def __init__(self):
        self.events = SimpleNamespace(closed=Event())
        self.calls = []

    def __getattr__(self, name):
        def operation(*args):
            self.calls.append((name, args))
            return "result" if name == "evaluate_js" else None

        return operation


def test_window_config_maps_runtime_options():
    config = WindowConfig(
        width=1200,
        height=800,
        x=20,
        y=30,
        frameless=True,
        transparent=True,
    )

    assert config.create_window_kwargs() == {
        "width": 1200,
        "height": 800,
        "x": 20,
        "y": 30,
        "resizable": True,
        "fullscreen": False,
        "min_size": (900, 600),
        "hidden": False,
        "frameless": True,
        "easy_drag": True,
        "minimized": False,
        "maximized": False,
        "on_top": False,
        "confirm_close": False,
        "background_color": "#FFFFFF",
        "transparent": True,
        "text_select": False,
    }


def test_webview_config_keeps_spiritus_http_server_authoritative():
    assert WebViewConfig(debug=True, user_agent="SpiritusTest").start_kwargs() == {
        "gui": None,
        "debug": True,
        "user_agent": "SpiritusTest",
        "localization": {},
        "http_server": False,
    }


def test_window_controller_delegates_without_exposing_raw_window():
    raw = FakeWindow()
    controller = WindowController(raw)

    controller.show()
    controller.resize(800, 600)
    controller.move(10, 20)
    controller.set_title("Updated")
    assert controller.evaluate_js("1 + 1") == "result"
    controller.on("closed", lambda: None)

    assert ("show", ()) in raw.calls
    assert ("resize", (800, 600)) in raw.calls
    assert ("move", (10, 20)) in raw.calls
    assert ("set_title", ("Updated",)) in raw.calls
    assert raw.events.closed.callbacks
    assert not hasattr(controller, "_raw")
