# Desktop runtime controls

Spiritus owns the PyWebView shell and the local UI server. Applications can
configure the shell declaratively while keeping their bridge and UI
application-specific:

```python
from pathlib import Path

from spiritus import AppConfig, WebViewConfig, WindowConfig, run

run(AppConfig(
    app_id="my-app",
    app_title="My App",
    app_root=Path(__file__).resolve().parent,
    window=WindowConfig(
        width=1280,
        height=820,
        min_size=(960, 640),
        resizable=True,
    ),
    webview=WebViewConfig(
        debug=False,
        user_agent="MyApp/1.0",
    ),
))
```

The older `window_size` and `min_size` fields remain supported. If both the
legacy fields and `window` are supplied, `window` is authoritative.

The base `Bridge` exposes a `window` controller after startup. A custom bridge
that inherits from it can use methods such as `show`, `hide`, `minimize`,
`restore`, `resize`, `move`, `set_title`, `toggle_fullscreen`, `load_url`, and
`evaluate_js` without depending on PyWebView's raw window object.

Spiritus also coordinates the OpenCode process, tool server, and UI HTTP server
through one idempotent shutdown path. This keeps close-event, `finally`, and
`atexit` cleanup from racing or repeating, and applies the same hidden-console
policy to Windows engine teardown that is used at engine startup.
