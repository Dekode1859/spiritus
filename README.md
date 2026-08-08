# Spiritus

[![CI](https://github.com/Dekode1859/Spiritus/actions/workflows/ci.yml/badge.svg)](https://github.com/Dekode1859/Spiritus/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/Dekode1859/Spiritus)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A runtime for AI desktop apps, built around one rule: **the engine knows nothing
about the app**.

Replace the Core under any Spiritus app with the Core from another and both still
run, unmodified. Delete an app and Core is untouched. It is not a framework, a
library of helpers, or a starter template — it is a runtime with a hard edge.

Core owns *how* execution happens: the window, the agent runtime's process
lifecycle, sessions, streaming, storage primitives, provider credentials, the
JS↔Python bridge. Apps own *what* the system does: agents, prompts, schemas,
what a folder means, branding.

## Install

The distribution and import package are both named **`spiritus`**.

There is no PyPI release. Install from git, pinned to a tag:

```bash
uv add "spiritus @ git+https://github.com/Dekode1859/Spiritus@v0.3.0"
```

```bash
pip install "spiritus @ git+https://github.com/Dekode1859/Spiritus@v0.3.0"
```

Either way, applications import the Spiritus runtime with:

```python
from spiritus import run, AppConfig, WorkspaceFolder
```

For local development against a checkout beside your app:

```toml
# your-app/pyproject.toml
[project]
dependencies = ["spiritus"]

[tool.uv.sources]
spiritus = { path = "../Spiritus", editable = true }
```

The `spiritus` console script provisions the OpenCode execution engine; it does
not run applications itself.

### The execution engine

Core hosts [OpenCode](https://opencode.ai) as its execution engine — a
self-contained ~60 MB native binary. A pure-Python wheel cannot carry it, so
`spiritus` ships a command to fetch it:

```bash
spiritus install-engine     # downloads the pinned build into a per-user cache
spiritus engine-info        # where it resolved from, which version, is it supported
```

**Nothing is ever downloaded implicitly.** `run()` resolves an engine but never
fetches one; an app that wants a one-time install calls `spiritus.engine.ensure()`
from its own bootstrap, where the cost is visible. Resolution order:

1. `SPIRITUS_OPENCODE_BIN` — an explicit path, always wins
2. `opencode` on PATH — npm, Homebrew, or the official install script
3. The per-user cache — a build `install-engine` fetched earlier

If none is found, startup fails with a message naming the command to run. Core
declares the engine range it was tested against and warns at launch when the
running engine falls outside it, rather than refusing to start — an engine a
patch ahead almost always works, and being blocked by someone else's release is
worse than being told.

## Write an app

An app's entry point is one object and one call. That object is the *only* place
an app injects identity into Core.

```python
from pathlib import Path
from spiritus import run, AppConfig, WorkspaceFolder

run(AppConfig(
    app_id="recipe-box",                  # data-isolation id
    app_title="Recipe Box",               # window title + UI header
    app_root=Path(__file__).resolve().parent,
    workspace_dirname="workspace",
    workspace_folders=(
        WorkspaceFolder("inbox",   "inbox", "Inbox"),
        WorkspaceFolder("recipes", "book",  "Recipes"),
    ),
    default_capture_folder="inbox",
    default_agent="recipe-writer",
))
```

Agents live in the app's `opencode.json`, with their own prompts. Core reads
them so the UI can offer them, and never defines one — adding a capability is
writing a prompt, not touching the runtime.

By default the app gets the shared chat UI. Set `ui_dir="ui"` and it serves your
own front-end against the same bridge instead. To add methods to that bridge,
subclass `Bridge` and pass `bridge_cls` — which is how an app adds behavior
without Core learning anything about it.

## The contract

| Field | Meaning |
|-------|---------|
| `app_id` | Data-isolation id; names the app-data dir in a packaged build. |
| `app_title` | Window title and UI header. |
| `app_root` | Directory holding `opencode.json` and the workspace. |
| `ui_dir` | Serve your own front-end; unset means the shared chat UI. |
| `bridge_cls` | `Bridge` subclass adding app-specific JS↔Python methods. |
| `workspace_dirname` | Name of the data root under `app_root`. |
| `workspace_folders` | The app's folder taxonomy — Core treats these as opaque. |
| `default_capture_folder` | Where ad-hoc notes are written. |
| `default_agent` | Agent selected on launch. |
| `window_size` / `min_size` | Window geometry. |

Everything else is Core's business. `spiritus.__all__` is exactly
`run`, `AppConfig`, `WorkspaceFolder`.

## Keeping the boundary honest

The rule is easy to state and constantly tempting to break, because every leak
looks reasonable in the moment. The test applied to anything proposed for Core:
*would this make sense in a cooking-recipe app?* If not, it belongs to the app.

That rule is executable, not aspirational. Two suites enforce it:

[`tests/test_spiritus_contract.py`](tests/test_spiritus_contract.py) fails the
build if Core source contains domain vocabulary. It has already caught a real
violation: a `Bridge.export_resume_pdf` method whose implementation was entirely
generic but whose *name* had leaked in from an app. It is now `export_pdf`.

[`tests/test_swap_invariant.py`](tests/test_swap_invariant.py) loads **both
frozen apps against the current Core**, exactly as the runtime does, and asserts
they still work: their `AppConfig` is accepted, their `Bridge` subclass
instantiates, Core loads the agents they declare, and every bridge method their
front-ends call still exists. That last check matters because app UIs name
bridge methods as *strings* over HTTP — a Core rename passes every import and
type check, then fails when a user clicks. Core's public API and its
JS-callable bridge surface are pinned as explicit lists, so changing either is a
deliberate act with a changelog entry, not an accident.

## Layout

```
Spiritus/
├── spiritus/              # the package — generic runtime, zero domain knowledge
│   ├── config.py         # the Core↔App contract
│   ├── runtime/          # desktop shell, OpenCode lifecycle, path resolution
│   ├── storage/          # file primitives, no folder semantics
│   ├── providers/        # provider credentials and model switching
│   ├── agents/           # reads app-declared agents (executes none)
│   ├── bridge.py         # JS↔Python bridge
│   └── ui/               # shared chat UI (branding injected via config)
├── apps/                 # example apps, frozen — proof the swap works
│   ├── learning-os/      # shared chat UI
│   └── jobsearch-os/     # custom UI + custom bridge
├── tests/
└── docs/
```

The apps under `apps/` are kept as evidence that Core runs more than one domain,
with different UIs and different agents. They consume Core as shared source via
`sys.path`; apps in their own repositories install the package instead.

## Development

```bash
uv sync --group dev --group apps
uv run pytest tests -m "not frozen_app"   # what CI gates on
uv run pytest tests                       # everything, including app internals
uv run ruff check .
uv build                                  # wheel + sdist
```

The `apps` group installs the frozen example apps' dependencies. They are not
Core dependencies — they are needed to *import* those apps, which the
swap-invariant suite does on every run. Without them that half of the baseline
skips, and a skipped baseline reads as green.

**What CI gates on, and why.** Two things: Core's own suite, and the swap
invariant. The `test_lexicon_*` suites are marked `frozen_app` and deselected.
They characterize apps/learning-os's internal pipeline — file and URL import,
wiki indexing, knowledge jobs — which is a multi-step filesystem workflow driven
by a background thread. That makes them the most platform-sensitive code here
and the least relevant to what the package promises, and the app they cover is
frozen. Run them when you touch that app; do not block a Core release on them.
The swap invariant still loads both apps on every CI run, so a Core change that
would break a real application still fails the build.

`tests/run_all.py` additionally runs the JS tests, which need `node`.

To run an example app:

```bash
cd apps/learning-os && make install && make run
```

## Releasing

The version lives in `pyproject.toml` (`project.version`) and nowhere else.
**Bumping that line is the release request.** When a change lands on `main`, CI
compares the declared version against the existing tags:

| On `main` | Result |
|-----------|--------|
| `project.version` names a version with no tag | tests run, then `vX.Y.Z` is tagged and a GitHub Release is published with the wheel and sdist |
| `project.version` unchanged | nothing — docs and test-only changes mint no versions |

`spiritus.__version__` is derived, not declared: it reads the installed
distribution's metadata, so it reports the version the running copy was
*installed from* rather than whatever a working tree happens to say now.

So a release is cut by editing one line in the same pull request as the change
it describes, alongside a [CHANGELOG.md](CHANGELOG.md) entry. Nothing is
published if lint or the test gate fails.

Pushing a `vX.Y.Z` tag by hand still works as an escape hatch; CI refuses a tag
that disagrees with the package version, since the artifact filenames would
otherwise contradict the release name.

## License

MIT — see [LICENSE](LICENSE).
