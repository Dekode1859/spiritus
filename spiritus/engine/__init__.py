"""
Execution-engine provisioning.

Spiritus hosts OpenCode as its execution engine but is a pure-Python distribution,
so it cannot carry a ~60 MB platform binary in its wheel. This module resolves
an engine to run and, on explicit request, downloads one.

Resolution order, cheapest and most explicit first:

1. ``SPIRITUS_OPENCODE_BIN`` — an operator-supplied path, always wins.
2. ``opencode`` on PATH — a system-wide install (npm, brew, the install script).
3. The user cache — a copy this module downloaded earlier.

Downloading is **never implicit**. ``resolve()`` only looks; ``install()`` is
what fetches, and an application calls it from its own bootstrap (or a user runs
``spiritus install-engine``). A runtime that silently pulls 60 MB the first time
an app starts is a runtime that surprises people behind corporate proxies and in
CI, so the cost is made explicit and opt-in.
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ENV_BIN = "SPIRITUS_OPENCODE_BIN"

#: The engine build Spiritus is tested against; what ``install()`` fetches.
PINNED_VERSION = "1.18.13"

#: Engine versions Spiritus is known to speak. Spiritus drives a handful of the
#: engine's HTTP endpoints (sessions, providers, events, agents, auth); a major
#: bump is where those are liable to change shape.
MIN_VERSION = (1, 17, 0)
MAX_VERSION_EXCLUSIVE = (2, 0, 0)

_RELEASE_URL = "https://github.com/sst/opencode/releases/download/v{version}/{asset}"

_BIN_NAMES = ("opencode.exe", "opencode")


# ── platform mapping ────────────────────────────────────────────────────────

def _is_musl() -> bool:
    """True on musl-based Linux (Alpine), which needs a different build."""
    if sys.platform != "linux":
        return False
    if any(Path("/lib").glob("ld-musl-*.so.1")):
        return True
    try:
        return "musl" in (platform.libc_ver()[0] or "").lower()
    except Exception:
        return False


def asset_name() -> str:
    """Release asset for the running platform, e.g. ``opencode-linux-x64.tar.gz``."""
    os_part = {"win32": "windows", "darwin": "darwin", "linux": "linux"}.get(sys.platform)
    if os_part is None:
        raise RuntimeError(f"No published engine build for platform {sys.platform!r}")

    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise RuntimeError(f"No published engine build for architecture {machine!r}")

    suffix = "-musl" if (os_part == "linux" and _is_musl()) else ""
    ext = "tar.gz" if os_part == "linux" else "zip"
    return f"opencode-{os_part}-{arch}{suffix}.{ext}"


def download_url(version: str = PINNED_VERSION) -> str:
    return _RELEASE_URL.format(version=version, asset=asset_name())


# ── cache location ──────────────────────────────────────────────────────────

def cache_root() -> Path:
    """Per-user cache directory holding downloaded engines.

    Shared across every Spiritus app on the machine — the engine is a tool, not
    app data, and app data stays in the app's own ``.opencode-home``.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "spiritus" / "engine"


def _binary_name() -> str:
    return "opencode.exe" if sys.platform == "win32" else "opencode"


def cached_path(version: str = PINNED_VERSION) -> Path:
    return cache_root() / version / _binary_name()


# ── resolution ──────────────────────────────────────────────────────────────

def resolve(version: str = PINNED_VERSION) -> Path | None:
    """Find an engine to run, without downloading. ``None`` if there is none."""
    override = os.environ.get(ENV_BIN, "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None

    # A frozen application owns its engine payload. Prefer it to a different
    # system-wide OpenCode installation so the application and its tested
    # Spiritus/OpenCode contract stay together. The bundle builder places the
    # binary in this stable location for every platform.
    if sys.platform == "win32":
        bundled_name = "opencode.exe"
    else:
        bundled_name = "opencode"
    try:
        from ..runtime.paths import is_bundled, resource_path

        if is_bundled():
            bundled = resource_path(f"engine/{bundled_name}")
            if bundled.is_file():
                return bundled
    except (ImportError, OSError):
        pass

    on_path = shutil.which("opencode")
    if on_path:
        return Path(on_path)

    cached = cached_path(version)
    return cached if cached.is_file() else None


def missing_engine_message() -> str:
    """Actionable text for the case where no engine could be resolved."""
    return (
        "No OpenCode engine found. Install one with:\n"
        "    spiritus install-engine\n"
        f"or point {ENV_BIN} at an existing binary, or install it system-wide:\n"
        "    curl -fsSL https://opencode.ai/install | bash"
    )


# ── version handling ────────────────────────────────────────────────────────

def parse_version(text: str) -> tuple[int, ...] | None:
    """Parse a bare version string like ``1.18.13`` into a comparable tuple."""
    if not text:
        return None
    head = text.strip().split()[0].lstrip("vV").split("-")[0]
    parts = head.split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return None


def binary_version(binary: Path | str) -> str | None:
    """Ask an engine binary for its version. ``None`` if it will not say."""
    try:
        out = subprocess.run(
            [str(binary), "--version"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except Exception:
        return None
    text = (out.stdout or out.stderr or "").strip()
    return text.split()[0] if text else None


def version_warning(version: str | None) -> str | None:
    """Return a warning if ``version`` is outside the supported range.

    Deliberately advisory: an engine one patch ahead of the pin almost always
    works, and refusing to start would strand users on a version they did not
    choose. Out-of-range means untested, not known-broken.
    """
    parsed = parse_version(version or "")
    if parsed is None:
        return (
            "Could not determine the OpenCode engine version; "
            f"Spiritus is tested against {PINNED_VERSION}."
        )
    if parsed < MIN_VERSION:
        return (
            f"OpenCode {version} is older than the supported minimum "
            f"{'.'.join(map(str, MIN_VERSION))}. Upgrade with: spiritus install-engine"
        )
    if parsed >= MAX_VERSION_EXCLUSIVE:
        return (
            f"OpenCode {version} is newer than the Spiritus version tested against "
            f"(< {'.'.join(map(str, MAX_VERSION_EXCLUSIVE))}). "
            "Engine API changes may break sessions, providers, or streaming."
        )
    return None


# ── installation ────────────────────────────────────────────────────────────

def _extract(archive: Path, dest_dir: Path) -> Path:
    """Unpack ``archive`` into ``dest_dir`` and return the engine binary."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            try:
                tf.extractall(dest_dir, filter="data")   # refuses paths escaping dest
            except TypeError:                            # Python < 3.11.4
                tf.extractall(dest_dir)

    # Files only. On POSIX the binary is named `opencode`, and an archive that
    # nests it under a directory of the same name would otherwise match the
    # directory first — install() would then move a directory into the cache and
    # every later resolve() would find something unrunnable. Shallowest match
    # wins so a top-level binary beats a bundled copy deeper in the tree.
    for name in _BIN_NAMES:
        matches = sorted((p for p in dest_dir.rglob(name) if p.is_file()),
                         key=lambda p: len(p.relative_to(dest_dir).parts))
        if matches:
            return matches[0]
    raise RuntimeError(f"No engine binary found inside {archive.name}")


def install(version: str = PINNED_VERSION, force: bool = False,
            on_progress=None) -> Path:
    """Download the engine into the user cache and return its path.

    Idempotent: an existing cached copy is returned untouched unless ``force``.
    ``on_progress`` receives ``(bytes_done, total_or_None)``.
    """
    import requests  # local import: only installing needs the network

    target = cached_path(version)
    if target.is_file() and not force:
        return target

    url = download_url(version)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="spiritus-engine-") as tmp:
        tmpdir = Path(tmp)
        archive = tmpdir / asset_name()

        with requests.get(url, stream=True, timeout=120) as r:
            if r.status_code == 404:
                raise RuntimeError(
                    f"No engine build published at {url}\n"
                    f"(version {version!r} may not exist for {asset_name()})"
                )
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0) or None
            done = 0
            with open(archive, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)

        binary = _extract(archive, tmpdir / "unpacked")
        if target.exists():
            target.unlink()
        shutil.move(str(binary), str(target))

    if sys.platform != "win32":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def ensure(version: str = PINNED_VERSION, on_progress=None) -> Path:
    """Resolve an engine, downloading one only if none is present.

    This is the call an application wires into its own bootstrap, where a
    one-time download is expected and can be reported to the user.
    """
    found = resolve(version)
    if found is not None:
        return found
    return install(version, on_progress=on_progress)
