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
from pathlib import Path

from . import __version__, engine
from .bundling import BundleError, BundleResource, BundleSpec, build_bundle, check_bundle


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


def _mapping(value: str, label: str) -> tuple[str, str]:
    source, separator, target = value.partition("=")
    if not separator or not source or not target:
        raise ValueError(f"{label} must use SOURCE=TARGET")
    return source, target


def cmd_bundle(args) -> int:
    root = Path(args.project_root).resolve()
    try:
        data = tuple(
            BundleResource(source, target)
            for value in args.data
            for source, target in [_mapping(value, "--data")]
        )
        binaries = tuple(
            BundleResource(source, target)
            for value in args.binary
            for source, target in [_mapping(value, "--binary")]
        )
        env_paths = dict(
            _mapping(value, "--runtime-env-path") for value in args.runtime_env_path
        )
        seed_files = dict(
            _mapping(value, "--seed-file") for value in args.seed_file
        )
        result = build_bundle(BundleSpec(
            project_root=root,
            entrypoint=args.entrypoint,
            name=args.name,
            app_id=args.app_id,
            version=args.app_version,
            datas=data,
            binaries=binaries,
            collect_packages=tuple(args.collect_package),
            hidden_imports=tuple(args.hidden_import),
            runtime_env_paths=env_paths,
            seed_files=seed_files,
            output_dir=Path(args.output_dir).resolve() if args.output_dir else None,
            work_dir=Path(args.work_dir).resolve() if args.work_dir else None,
            console=args.console,
            icon=args.icon,
            bundle_identifier=args.bundle_identifier,
        ))
    except (BundleError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Built: {result.bundle_dir}")
    print(f"Manifest: {result.manifest}")
    print(f"Spec: {result.spec_file}")
    return 0


def cmd_bundle_check(args) -> int:
    try:
        payload = check_bundle(Path(args.bundle_dir), app_id=args.app_id)
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Bundle OK: {payload.get('name', '(unnamed)')} "
        f"({payload.get('version') or 'unversioned'})"
    )
    print(f"Files: {len(payload.get('files', []))}")
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

    p = sub.add_parser("bundle", help="build a one-folder application bundle")
    p.add_argument("--project-root", default=".", help="application project root")
    p.add_argument("--entrypoint", required=True, help="application entry script")
    p.add_argument("--name", required=True, help="bundle and executable name")
    p.add_argument("--app-id", required=True, help="stable writable-data identifier")
    p.add_argument("--app-version", default="", help="application version for the manifest")
    p.add_argument(
        "--data", action="append", default=[], metavar="SOURCE=TARGET_DIR",
        help="copy an application data file/directory into the bundle (repeatable)",
    )
    p.add_argument(
        "--binary", action="append", default=[], metavar="SOURCE=TARGET_DIR",
        help="copy an application binary into the bundle (repeatable)",
    )
    p.add_argument(
        "--collect-package", action="append", default=[], metavar="PACKAGE",
        help="collect a package and its data/binaries (repeatable)",
    )
    p.add_argument(
        "--hidden-import", action="append", default=[], metavar="MODULE",
        help="add a PyInstaller hidden import (repeatable)",
    )
    p.add_argument(
        "--runtime-env-path", action="append", default=[], metavar="NAME=BUNDLE_PATH",
        help="set an environment variable to a bundle resource path (repeatable)",
    )
    p.add_argument(
        "--seed-file", action="append", default=[], metavar="BUNDLE_PATH=APP_DATA_PATH",
        help="copy a bundled file to writable app data on first launch (repeatable)",
    )
    p.add_argument("--output-dir", default=None, help="PyInstaller output directory")
    p.add_argument("--work-dir", default=None, help="PyInstaller work directory")
    p.add_argument("--icon", default=None, help="application icon inside the project")
    p.add_argument("--bundle-identifier", default=None, help="macOS bundle identifier")
    p.add_argument("--console", action="store_true", help="keep a console window")
    p.set_defaults(func=cmd_bundle)

    p = sub.add_parser("bundle-check", help="validate a Spiritus bundle manifest")
    p.add_argument("bundle_dir", help="built bundle directory")
    p.add_argument("--app-id", default=None, help="expected application id")
    p.set_defaults(func=cmd_bundle_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
