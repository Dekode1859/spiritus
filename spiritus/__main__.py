"""
Command line entry point: ``spiritus <command>`` (or ``python -m spiritus``).

Provisioning only. Spiritus runs applications, not a CLI — this exists so the
execution engine can be installed explicitly rather than downloaded behind
someone's back at first launch.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, engine


def _human(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


def _progress(done: int, total: int | None) -> None:
    if total:
        pct = done * 100 // total
        sys.stderr.write(f"\r  downloading...{pct:3d}%  ({_human(done)} / {_human(total)})")
    else:
        sys.stderr.write(f"\r  downloading...{_human(done)}")
    sys.stderr.flush()


def cmd_install_engine(args) -> int:
    version = args.version or engine.PINNED_VERSION

    if not args.force:
        existing = engine.resolve(version)
        if existing is not None:
            found = engine.binary_version(existing)
            print(f"Engine already available: {existing}")
            if found:
                print(f"  version {found}")
                warning = engine.version_warning(found)
                if warning:
                    print(f"  warning: {warning}")
            print("Use --force to download the pinned build anyway.")
            return 0

    print(f"Installing OpenCode {version} for {engine.asset_name()}")
    print(f"  from {engine.download_url(version)}")
    try:
        path = engine.install(version, force=args.force, on_progress=_progress)
    except Exception as exc:
        sys.stderr.write("\n")
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sys.stderr.write("\n")
    print(f"Installed: {path}")
    installed = engine.binary_version(path)
    if installed:
        print(f"  version {installed}")
    return 0


def cmd_engine_path(_args) -> int:
    found = engine.resolve()
    if found is None:
        print(engine.missing_engine_message(), file=sys.stderr)
        return 1
    print(found)
    return 0


def cmd_engine_info(_args) -> int:
    supported = (f">={'.'.join(map(str, engine.MIN_VERSION))},"
                 f"<{'.'.join(map(str, engine.MAX_VERSION_EXCLUSIVE))}")
    rows = [
        ("spiritus", __version__),
        ("pinned engine", engine.PINNED_VERSION),
        ("supported range", supported),
        ("platform asset", engine.asset_name()),
        ("cache", str(engine.cache_root())),
        (engine.ENV_BIN, os.environ.get(engine.ENV_BIN) or "(unset)"),
    ]

    found = engine.resolve()
    version = engine.binary_version(found) if found else None
    rows.append(("resolved engine", str(found) if found else "(none)"))
    if found:
        rows.append(("engine version", version or "(unknown)"))

    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"{label:<{width}}  {value}")

    if found is None:
        print()
        print(engine.missing_engine_message(), file=sys.stderr)
        return 1

    warning = engine.version_warning(version)
    if warning:
        print(f"\nwarning: {warning}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spiritus",
        description="Spiritus runtime — execution-engine provisioning.",
    )
    parser.add_argument("--version", action="version", version=f"spiritus {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install-engine", help="download the OpenCode engine into the user cache")
    p.add_argument("--version", dest="version", default=None,
                   help=f"engine version to fetch (default: {engine.PINNED_VERSION})")
    p.add_argument("--force", action="store_true",
                   help="re-download even if an engine is already available")
    p.set_defaults(func=cmd_install_engine)

    p = sub.add_parser("engine-path", help="print the resolved engine path")
    p.set_defaults(func=cmd_engine_path)

    p = sub.add_parser("engine-info", help="show engine resolution and version details")
    p.set_defaults(func=cmd_engine_info)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
