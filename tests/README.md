# Tests

Three kinds of suite, with different standing.

| Suite | Covers | Gates CI |
|-------|--------|----------|
| `test_spiritus_*`, `test_bridge`, `test_runtime_shell_api` | Core: the `AppConfig` contract, storage, paths, agents, providers, engine provisioning, process lifecycle, the JS↔Python bridge | yes |
| `test_swap_invariant` | The boundary: both frozen apps loaded against the current Core | yes |
| `test_lexicon_*` (marked `frozen_app`) | apps/learning-os's own pipeline — import, wiki indexing, knowledge jobs, curation | no |

The `frozen_app` suites are excluded from the CI gate with
`-m "not frozen_app"`. They exercise a multi-step filesystem pipeline driven by a
background daemon thread, in an app that is no longer developed — so they are
both the most platform-sensitive tests here and the least relevant to whether
the `spiritus` package is releasable. Run them when you change that app.

Everything below still applies to how the suites work.

## Run everything

```bash
python tests/run_all.py
```

## Run individually

```bash
# Python — Bridge (storage, browser profile, server restart, tile layout).
# Needs `webview`, which is in the jobsearch-os venv:
apps/jobsearch-os/.venv/Scripts/python.exe -m unittest tests.test_bridge -v

# JavaScript — app.js pure functions (render snapshots, JSON parsers, merge):
node tests/test_app.mjs
```

## What's here

| File | Covers |
|------|--------|
| `test_bridge.py` | `core/spiritus/bridge.py` — storage CRUD, profile status/reset, browser guards, the provider server-restart methods, tile-layout geometry, and a `compile()` guard on the embedded browser-agent script. |
| `test_app.mjs` | `apps/jobsearch-os/ui/app.js` — golden snapshots of every section view/edit renderer, the resume + export HTML, JD/form renderers; structural tests for `mergeProfile` and the JSON parsers. |
| `__snapshots__/app-snapshots.json` | Golden output captured from the baseline. Delete a key (or the file) to re-record. |

### How the JS harness works

`app.js` is a browser script, not a module. The harness loads it with
`new Function`, stubs `window`, strips the trailing `init()` call, and appends an
export object — so the pure functions can be exercised in Node with zero deps.
Only functions that don't touch the DOM are tested.

### Snapshots

First run records snapshots and prints `N snapshots created`. Later runs compare.
A behavior-preserving refactor should produce **byte-identical** output and stay
green; if a change is intended, delete the affected key from the snapshot file
and re-run to re-record.
