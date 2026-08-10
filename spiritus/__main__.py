"""
Command line entry point: ``spiritus <command>`` (or ``python -m spiritus``).

Provisioning only. Spiritus runs applications, not a CLI — this exists so the
execution engine can be installed explicitly rather than downloaded behind
someone's back at first launch.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, engine
from .bundle_config import (
    CONFIG_NAME,
    SUPPORTED_PLATFORMS,
    BundleConfig,
    check_environment,
    init_bundle_config,
    load_bundle_config,
    write_platform_scripts,
)
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


def _config_path(root: Path, value: str | None) -> Path:
    path = Path(value or CONFIG_NAME)
    return path if path.is_absolute() else root / path


def _run_hook(config: BundleConfig, kind: str) -> None:
    command = config.commands(kind)
    if not command:
        return
    print(f"Running {kind} hook: {' '.join(command)}")
    subprocess.run(command, cwd=config.project_root, check=True)


def cmd_bundle_init(args) -> int:
    root = Path(args.project_root).resolve()
    try:
        config = init_bundle_config(
            root,
            path=_config_path(root, args.config),
            platform=args.platform,
            entrypoint=args.entrypoint,
            force=args.force,
        )
        scripts = write_platform_scripts(config)
    except (BundleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Created: {config.path}")
    for script in scripts:
        print(f"Script:  {script}")
    print(f"Entrypoint: {config.spec.resolved_entrypoint}")
    print("Edit the spec to declare application resources, packages, hooks, and installers.")
    return 0


def cmd_bundle(args) -> int:
    root = Path(args.project_root).resolve()
    if args.action == "init":
        return cmd_bundle_init(args)
    try:
        config_file = _config_path(root, args.config)
        explicit = any((args.entrypoint, args.name, args.app_id))
        config = None
        if not explicit and config_file.is_file():
            config = load_bundle_config(
                config_file,
                project_root=root,
                defer_resource_validation=True,
            )
            if _platform_name() not in config.platforms:
                raise BundleError(
                    f"bundle spec does not enable the current platform {_platform_name()!r}"
                )
            _run_hook(config, "prepare")
            result = build_bundle(config.spec)
        else:
            if not all((args.entrypoint, args.name, args.app_id)):
                raise BundleError(
                    "entrypoint, name, and app-id are required, or initialize "
                    f"{config_file.name} with `spiritus bundle init`"
                )
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
    except (BundleError, ValueError, TypeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Built: {result.bundle_dir}")
    print(f"Manifest: {result.manifest}")
    print(f"Spec: {result.spec_file}")
    return 0


def cmd_bundle_check(args) -> int:
    try:
        if args.bundle_dir:
            payload = check_bundle(Path(args.bundle_dir), app_id=args.app_id)
            config = None
        else:
            root = Path(args.project_root).resolve()
            config = load_bundle_config(_config_path(root, args.config), project_root=root)
            missing = check_environment(config)
            if missing:
                raise BundleError("missing build prerequisites: " + ", ".join(missing))
            payload = check_bundle(config.bundle_dir, app_id=config.spec.app_id)
            if args.run_verify:
                _run_hook(config, "verify")
    except (BundleError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Bundle OK: {payload.get('name', '(unnamed)')} "
        f"({payload.get('version') or 'unversioned'})"
    )
    print(f"Files: {len(payload.get('files', []))}")
    return 0


def _platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    raise BundleError("Spiritus bundling supports Windows and macOS hosts")


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

    p = sub.add_parser("bundle", help="initialize or build a one-folder application bundle")
    p.add_argument("action", nargs="?", choices=("init",), default=None,
                   help="initialize a repository-owned bundle spec and platform scripts")
    p.add_argument("--project-root", default=".", help="application project root")
    p.add_argument("--config", default=None, help=f"bundle spec path (default: {CONFIG_NAME})")
    p.add_argument("--entrypoint", default=None, help="application entry script")
    p.add_argument("--name", default=None, help="bundle and executable name")
    p.add_argument("--app-id", default=None, help="stable writable-data identifier")
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
    p.add_argument("--platform", choices=("auto", "all", *SUPPORTED_PLATFORMS), default="auto",
                   help="platform scripts to generate when using `bundle init`")
    p.add_argument("--force", action="store_true", help="overwrite an existing spec during init")
    p.set_defaults(func=cmd_bundle)

    p = sub.add_parser("bundle-check", help="validate a Spiritus spec, environment, and bundle manifest")
    p.add_argument("bundle_dir", nargs="?", help="built bundle directory; omit to use the repository spec")
    p.add_argument("--app-id", default=None, help="expected application id")
    p.add_argument("--project-root", default=".", help="application project root")
    p.add_argument("--config", default=None, help=f"bundle spec path (default: {CONFIG_NAME})")
    p.add_argument("--run-verify", action="store_true", help="run the configured application smoke check")
    p.set_defaults(func=cmd_bundle_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
