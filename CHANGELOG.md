# Changelog

All notable changes to Spiritus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is single-sourced from `project.version` in `pyproject.toml`.
Bumping it is what requests a release: when the change lands on `main`, CI tags
the new version and publishes it. Leaving it alone releases nothing.

## [Unreleased]

## [0.0.35] - 2026-08-15

### Added
- Added a shared diagnostics service for session and desktop-bridge runs,
  including filtered live terminal output through `spiritus dev run.py`.
- Added durable launch records, correlated trace events, structured run
  artifacts, checkpoint events, and process-crash diagnostics.

### Fixed
- Structured bridge results are captured from completion events and recovered
  locally when the pinned OpenCode history endpoint rejects schema output.
- Terminal failures now show their failure kind, owning layer, and error
  message inline.

## [0.0.34] - 2026-08-11

### Added
- Added declarative PyWebView window and rendering configuration plus a
  runtime-owned window controller for desktop applications.

### Fixed
- Centralized desktop shutdown across window-close, `finally`, and `atexit`
  paths, including explicit cleanup of the Spiritus UI server.
- Windows engine-tree shutdown now uses the same hidden-console policy as
  engine startup, preventing a terminal flash when applications close.

## [0.0.33] - 2026-08-11

- Added opt-in application update support with GitHub and GitLab Releases,
  generic HTTPS JSON feeds, SemVer comparison, channel filtering, platform asset
  selection, checksum-verified staging, and explicit installer handoff. Apps
  still own signing, trust, rollback, restart, and release policy.

## [0.0.32] - 2026-08-10

### Fixed
- Bundle preparation hooks can now create declared data and binary resources
  before Spiritus validates them. Resource validation still runs immediately
  before PyInstaller, so missing assets fail clearly at the build boundary.

## [0.0.31] - 2026-08-10

### Added
- Rebranded the runtime to Spiritus across the Python package, console script,
  environment variables, documentation, workflows, and repository metadata.
  Runtime behavior is unchanged.
- Added a repository-owned `spiritus.bundle.toml` workflow with
  `spiritus bundle init`, persistent-spec `spiritus bundle`, generated Windows
  PowerShell and macOS shell wrappers, and source/environment validation in
  `spiritus bundle-check`.
- Added Spiritus-owned hidden-console process options for frozen Windows engine
  and helper-process launches, while preserving explicit console requests.

## [0.0.3] - 2026-08-10

### Added
- Added a manifest-driven `spiritus bundle` command for one-folder PyInstaller
  application bundles.
- Added external data, binary, package, hidden-import, runtime-resource, and
  first-launch seed-file inputs so applications keep ownership of their
  product-specific assets.
- Added `spiritus bundle-check` and a bundle manifest for deterministic handoff
  to application-owned installers and smoke tests.
- Frozen applications now prefer `engine/opencode.exe` or `engine/opencode`
  from their own bundle before using a system-wide engine.

## [0.3.1] — 2026-08-07

### Fixed
- **A bundled app wrote its user data to a macOS path on every platform.**
  `app_data_dir` — where a frozen app keeps its workspace, `opencode.json`, and
  `.opencode-home`, since it cannot write beside itself — was hardcoded to
  `~/Library/Application Support/<app_id>`. On Windows that resolved to
  `C:\Users\<user>\Library\Application Support\<app_id>`, a directory no
  convention owns, and on Linux likewise. It never raised: the directory was
  created on demand, so a packaged app appeared to work while scattering user
  data somewhere nobody would look for it, and an OS-level backup or migration
  would skip it. Now `%LOCALAPPDATA%` on Windows, `$XDG_DATA_HOME` (falling back
  to `~/.local/share`) on Linux, and the unchanged Application Support path on
  macOS. Every branch is asserted on every host, since picking the wrong one
  produces no error to catch.

## [0.3.0] — 2026-08-06

### Changed
- **Breaking (packaging only): the distribution and import package are now both
  named `spiritus`.** The console script keeps the same name. Applications
  depend on the single package name:

  ```toml
  dependencies = ["spiritus"]

  [tool.uv.sources]
  spiritus = { git = "https://github.com/Dekode1859/Spiritus", tag = "v0.3.0" }
  ```
- **The version is now single-sourced from `pyproject.toml`.** It used to live
  in `spiritus/__init__.py`, with the build reading it out via
  `[tool.hatch.version]`; that relationship is now inverted.
  `spiritus.__version__` is derived at import from the installed distribution's
  metadata, so it reports the version the running copy was *installed from*
  instead of a hardcoded literal that a working tree can silently outrun. The
  attribute keeps its name, type, and meaning, so nothing that reads it changes.
  Uninstalled source trees fall back to reading the adjacent `pyproject.toml`.

## [0.2.0] — 2026-08-05

First release packaged for installation from outside the repository.

### Added
- **Engine provisioning.** The OpenCode engine is a ~60 MB native binary that a
  pure-Python wheel cannot carry, and it was previously an undeclared
  prerequisite: Spiritus called `shutil.which("opencode")` and, when it found
  nothing, warned and ran on with every agent dead. There is now an
  `spiritus.engine` module and a CLI:

  ```bash
  spiritus install-engine     # fetch the pinned build into a per-user cache
  spiritus engine-info        # resolution source, version, supported range
  spiritus engine-path
  ```

  Resolution is `SPIRITUS_OPENCODE_BIN` → PATH → per-user cache. Downloading is
  never implicit: `run()` only resolves, and an app calls `engine.ensure()` from
  its own bootstrap if it wants a one-time install. A missing engine now fails
  startup with a message naming the command to run.
- **Engine version checking.** Spiritus drives several of the engine's HTTP
  endpoints against what was previously a completely unpinned server. It now
  declares a supported range, reads the launched engine's version, and warns on
  a mismatch instead of failing mysteriously later. `OpenCodeServer.engine_version`
  exposes it.
- `spiritus` is now a real distribution, installable straight from git:
  `uv add "spiritus @ git+https://github.com/Dekode1859/Spiritus@v0.2.0"`.
  The shared UI ships as package data, so `resource_path("ui")` resolves inside
  site-packages.
- Optional Playwright integration for applications that need browser
  automation; Spiritus never imports it at package import time.
- Test suite covering the `AppConfig` contract, storage primitives, path
  resolution, agent loading, provider configuration, engine provisioning,
  process lifecycle, and multipart parsing.
- An executable form of the project's central rule: a test that fails if the
  reusable package
  source contains product-specific vocabulary.
- CI across Linux, macOS, and Windows on Python 3.11–3.13, plus a job that
  installs the package from its own git URL and smoke-tests the result.

### Changed
- **Breaking:** the package moved from `core/spiritus/` to `spiritus/` at the
  repository root, so installs no longer need a `#subdirectory=core` fragment.
- **Breaking:** `Bridge.export_resume_pdf` is now `Bridge.export_pdf`. The
  implementation was always generic — it renders arbitrary HTML — and the old
  name violated the rule that Spiritus carries no product-specific vocabulary.
  Callers must update the bridge method name; the signature is unchanged.
- Python 3.13 is supported. `runtime/shell.py` no longer imports the stdlib
  `cgi` module, removed in 3.13, so `import spiritus` failed outright there.
  Multipart uploads are parsed with `email` instead, with identical behavior for
  the `files` field.

### Fixed
- **The engine outlived the app on every exit.** `OpenCodeServer.stop()`
  terminated only its direct child, but the `opencode` launcher on PATH is a
  wrapper (an npm shim on Windows) that execs the real binary as a
  *grandchild*. Killing the wrapper left the engine running, reparented and
  holding its port — one orphaned process per app launch. `stop()` now takes
  the whole process tree (`taskkill /T` on Windows, process-group signal
  elsewhere), and the engine is started in its own process group on POSIX so
  the group signal has something to target.

  A force quit — Task Manager, `taskkill /F`, a crash — runs no `atexit`, no
  `finally`, and no signal handler, so cooperative shutdown cannot help there.
  On Windows the engine is now also assigned to a Job Object with
  `KILL_ON_JOB_CLOSE`, which makes the kernel terminate it when the app dies
  for any reason. Best effort: if the job cannot be created, behavior falls
  back to cooperative shutdown. POSIX still relies on the process group, so a
  `SIGKILL` of the app there can still leave the engine behind.
- **Path traversal in `storage._safe`.** Containment was checked with a string
  prefix comparison, so a sibling directory whose name began with the root's
  name (root `…/workspace`, target `…/workspace-evil`) was accepted as inside
  the root. Reads, writes, and deletes could therefore escape the workspace.
  Containment is now checked against the resolved path hierarchy.

## 0.1.0 — never released

Recorded for continuity only: this version existed solely as shared source,
was never tagged, and no artifact was ever published for it. It established
the initial `AppConfig`, `WorkspaceFolder`, and `run()` contract, application
shell, OpenCode process lifecycle, storage primitives, provider abstraction,
and shared chat UI.

[Unreleased]: https://github.com/Dekode1859/Spiritus/compare/v0.0.35...HEAD
[0.0.35]: https://github.com/Dekode1859/Spiritus/releases/tag/v0.0.35
[0.0.34]: https://github.com/Dekode1859/Spiritus/releases/tag/v0.0.34
[0.0.33]: https://github.com/Dekode1859/Spiritus/releases/tag/v0.0.33
[0.0.32]: https://github.com/Dekode1859/Spiritus/releases/tag/v0.0.32
[0.0.31]: https://github.com/Dekode1859/Spiritus/releases/tag/v0.0.31
[0.3.1]: https://github.com/Dekode1859/Spiritus/releases/tag/v0.3.1
[0.3.0]: https://github.com/Dekode1859/Spiritus/releases/tag/v0.3.0
[0.2.0]: https://github.com/Dekode1859/Spiritus/releases/tag/v0.2.0
