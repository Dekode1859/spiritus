# Tests

The test suite covers Spiritus's public runtime and SDK boundaries: configuration,
storage, paths, agents, providers, engine provisioning, process lifecycle, the
runtime HTTP API, and the JavaScript-to-Python bridge.

## Run everything

```bash
uv run pytest
```

## Run individually

```bash
uv run pytest tests/test_bridge.py -v
uv run pytest tests/test_spiritus_contract.py -v
uv run ruff check .
uv build
```

`test_bridge.py` requires an environment where `pywebview` can import. The
remaining runtime tests are designed to run headlessly.
