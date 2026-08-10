"""Persistent configuration and script generation for Spiritus bundles."""
from __future__ import annotations

import importlib.util
import re
import shlex
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .bundling import BundleError, BundleResource, BundleSpec

CONFIG_NAME = "spiritus.bundle.toml"
SUPPORTED_PLATFORMS = ("windows", "macos")


def _pairs(values: list[str], label: str) -> tuple[BundleResource, ...]:
    resources = []
    for value in values:
        source, separator, target = value.partition("=")
        if not separator or not source or not target:
            raise BundleError(f"{label} must use SOURCE=TARGET_DIR: {value!r}")
        resources.append(BundleResource(source, target))
    return tuple(resources)


def _mapping(values: object, label: str) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise BundleError(f"{label} must be a TOML table")
    return {str(key): str(value) for key, value in values.items()}


def _command(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BundleError(f"{label} must be an array of command arguments")
    return tuple(value)


def _current_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    raise BundleError("Spiritus bundling supports Windows and macOS hosts")


def _platform_values() -> dict[str, str]:
    platform = _current_platform()
    return {
        "platform": platform,
        "engine_binary": "opencode.exe" if platform == "windows" else "opencode",
    }


def _format_value(value: str, label: str) -> str:
    if "{" not in value:
        return value
    try:
        return value.format(**_platform_values())
    except KeyError as exc:
        raise BundleError(f"unknown placeholder {exc.args[0]!r} in {label}") from exc


def _app_id(name: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-._").lower()
    return value or "app"


def _project_metadata(root: Path) -> tuple[str, str]:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise BundleError(f"cannot read project metadata: {pyproject}") from exc
        if isinstance(project, dict):
            name = str(project.get("name") or root.name)
            version = str(project.get("version") or "0.1.0")
            return name, version
    return root.name, "0.1.0"


def _discover_entrypoint(root: Path) -> str:
    for candidate in ("main.py", "app.py", "run.py"):
        if (root / candidate).is_file():
            return candidate
    raise BundleError(
        "bundle init could not find an entrypoint; create main.py, app.py, or run.py "
        "or pass --entrypoint"
    )


def _discover_data(root: Path) -> list[str]:
    values = []
    if (root / "ui").is_dir():
        values.append("ui=ui")
    if (root / "opencode.json").is_file():
        values.append("opencode.json=.")
    return values


@dataclass(frozen=True, slots=True)
class BundleConfig:
    """A repository-owned bundle spec plus optional build hooks."""

    path: Path
    project_root: Path
    spec: BundleSpec
    platforms: tuple[str, ...]
    prepare: tuple[str, ...] = ()
    verify: tuple[str, ...] = ()
    installer: tuple[str, ...] = ()
    platform_hooks: dict[str, dict[str, tuple[str, ...]]] | None = None

    def _bundle_dir_for(self, platform: str) -> Path:
        suffix = ".app" if platform == "macos" else ""
        return self.spec.resolved_output_dir / (self.spec.name + suffix)

    @property
    def bundle_dir(self) -> Path:
        return self._bundle_dir_for(_current_platform())

    def _executable_for(self, platform: str) -> Path:
        bundle_dir = self._bundle_dir_for(platform)
        if platform == "macos":
            return bundle_dir / "Contents" / "MacOS" / self.spec.name
        return bundle_dir / f"{self.spec.name}.exe"

    @property
    def executable(self) -> Path:
        return self._executable_for(_current_platform())

    def commands(self, kind: str, platform: str | None = None) -> tuple[str, ...]:
        selected = platform or _current_platform()
        platform_hooks = self.platform_hooks or {}
        command = platform_hooks.get(selected, {}).get(kind) or getattr(self, kind)
        if not command:
            return ()
        values = {
            "root": str(self.project_root),
            "config": str(self.path),
            "bundle": str(self._bundle_dir_for(selected)),
            "executable": str(self._executable_for(selected)),
            "version": self.spec.version,
            "app_id": self.spec.app_id,
            "name": self.spec.name,
            "platform": selected,
        }
        return tuple(item.format(**values) for item in command)


def load_bundle_config(
    path: Path,
    *,
    project_root: Path | None = None,
    defer_resource_validation: bool = False,
) -> BundleConfig:
    path = Path(path).resolve()
    if not path.is_file():
        raise BundleError(f"missing Spiritus bundle spec: {path}; run `spiritus bundle init`")
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BundleError(f"invalid Spiritus bundle spec: {path}") from exc
    if payload.get("format") != 1:
        raise BundleError("unsupported Spiritus bundle spec format")

    root = Path(project_root or path.parent).resolve()
    try:
        data_values = [
            "=".join(_format_value(part, "datas") for part in value.split("=", 1))
            for value in payload.get("datas", [])
        ]
        binary_values = [
            "=".join(_format_value(part, "binaries") for part in value.split("=", 1))
            for value in payload.get("binaries", [])
        ]
        datas = _pairs(data_values, "datas")
        binaries = _pairs(binary_values, "binaries")
        spec = BundleSpec(
            project_root=root,
            entrypoint=str(payload.get("entrypoint", "main.py")),
            name=str(payload["name"]),
            app_id=str(payload["app_id"]),
            version=str(payload.get("version", "")),
            datas=datas,
            binaries=binaries,
            collect_packages=tuple(str(item) for item in payload.get("collect_packages", [])),
            hidden_imports=tuple(str(item) for item in payload.get("hidden_imports", [])),
            runtime_env_paths=_mapping(payload.get("runtime_env_paths"), "runtime_env_paths"),
            seed_files=_mapping(payload.get("seed_files"), "seed_files"),
            output_dir=payload.get("output_dir"),
            work_dir=payload.get("work_dir"),
            console=bool(payload.get("console", False)),
            icon=payload.get("icon"),
            bundle_identifier=payload.get("bundle_identifier"),
            defer_resource_validation=defer_resource_validation,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleError(f"invalid bundle settings in {path}") from exc

    platforms = tuple(str(item) for item in payload.get("platforms", SUPPORTED_PLATFORMS))
    if not platforms or any(item not in SUPPORTED_PLATFORMS for item in platforms):
        raise BundleError(f"platforms must contain only {SUPPORTED_PLATFORMS}")
    hooks = payload.get("hooks", {})
    if not isinstance(hooks, dict):
        raise BundleError("hooks must be a TOML table")
    platform_hooks = {}
    for platform in SUPPORTED_PLATFORMS:
        values = hooks.get(platform, {})
        if not isinstance(values, dict):
            raise BundleError(f"hooks.{platform} must be a TOML table")
        platform_hooks[platform] = {
            kind: _command(values.get(kind), f"hooks.{platform}.{kind}")
            for kind in ("prepare", "verify", "installer")
        }
    return BundleConfig(
        path=path,
        project_root=root,
        spec=spec,
        platforms=platforms,
        prepare=_command(hooks.get("prepare"), "hooks.prepare"),
        verify=_command(hooks.get("verify"), "hooks.verify"),
        installer=_command(hooks.get("installer"), "hooks.installer"),
        platform_hooks=platform_hooks,
    )


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_config(
    *,
    name: str,
    app_id: str,
    version: str,
    entrypoint: str,
    datas: list[str],
    platforms: tuple[str, ...],
) -> str:
    lines = [
        "# Spiritus bundle specification. Keep this file in version control.",
        "format = 1",
        f"platforms = [{', '.join(_toml_string(item) for item in platforms)}]",
        f"entrypoint = {_toml_string(entrypoint)}",
        f"name = {_toml_string(name)}",
        f"app_id = {_toml_string(app_id)}",
        f"version = {_toml_string(version)}",
        "console = false",
        "",
        "# Application-owned files copied into the frozen bundle.",
        "datas = [",
    ]
    lines.extend(f"  {_toml_string(item)}," for item in datas)
    lines.extend([
        "]",
        "binaries = []",
        "collect_packages = []",
        "hidden_imports = []",
        "",
        "[runtime_env_paths]",
        "# BROWSER_PATH = \"relative/path/in/bundle\"",
        "# Use {platform} or {engine_binary} in resource paths when platforms differ.",
        "",
        "[seed_files]",
        "# \"bundle/config.json\" = \"config.json\"",
        "",
        "[hooks]",
        "# prepare = [\"uv\", \"run\", \"python\", \"packaging/prepare-assets.py\"]",
        "# verify = [\"{bundle}/MyApp.exe\", \"--check-bundle\"]",
        "# installer = [\"iscc\", \"packaging/MyApp.iss\"]",
        "",
    ])
    return "\n".join(lines)


def init_bundle_config(
    root: Path,
    *,
    path: Path | None = None,
    platform: str = "auto",
    entrypoint: str | None = None,
    force: bool = False,
) -> BundleConfig:
    root = Path(root).resolve()
    path = (path or root / CONFIG_NAME).resolve()
    if path.exists() and not force:
        raise BundleError(f"bundle spec already exists: {path}; use --force to regenerate")
    selected = (_current_platform(),) if platform == "auto" else (
        tuple(SUPPORTED_PLATFORMS) if platform == "all" else (platform,)
    )
    if any(item not in SUPPORTED_PLATFORMS for item in selected):
        raise BundleError(f"platform must be auto, all, or one of {SUPPORTED_PLATFORMS}")
    name, version = _project_metadata(root)
    entrypoint = entrypoint or _discover_entrypoint(root)
    config_text = render_config(
        name=name.rsplit(".", 1)[-1],
        app_id=_app_id(name),
        version=version,
        entrypoint=entrypoint,
        datas=_discover_data(root),
        platforms=selected,
    )
    path.write_text(config_text, encoding="utf-8", newline="\n")
    return load_bundle_config(path, project_root=root)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_platform_script(config: BundleConfig, platform: str) -> str:
    if platform not in config.platforms:
        raise BundleError(f"platform {platform!r} is not enabled in {config.path}")
    relative_config = config.path.relative_to(config.project_root).as_posix()
    if platform == "windows":
        lines = [
            '$ErrorActionPreference = "Stop"',
            "$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path",
            "Push-Location $Root",
            "try {",
            f"    uv run spiritus bundle --project-root $Root --config {_ps_quote(relative_config)}",
            "    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            f"    uv run spiritus bundle-check --project-root $Root --config {_ps_quote(relative_config)} --run-verify",
            "    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        ]
        if config.installer:
            command = config.commands("installer", platform)
            lines.extend([
                "    $Installer = @(",
                *[f"        {_ps_quote(item)}," for item in command],
                "    )",
                "    & $Installer[0] $Installer[1..($Installer.Length - 1)]",
                "    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            ])
        lines.extend(["} finally {", "    Pop-Location", "}", ""])
        return "\n".join(lines)

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
        "cd \"$ROOT\"",
        f"uv run spiritus bundle --project-root \"$ROOT\" --config {shlex.quote(relative_config)}",
        f"uv run spiritus bundle-check --project-root \"$ROOT\" --config {shlex.quote(relative_config)} --run-verify",
    ]
    if config.installer:
        lines.append(" ".join(shlex.quote(item) for item in config.commands("installer", platform)))
    lines.append("")
    return "\n".join(lines)


def write_platform_scripts(config: BundleConfig, directory: Path | None = None) -> tuple[Path, ...]:
    directory = (directory or config.project_root / "packaging").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for platform in config.platforms:
        suffix = ".ps1" if platform == "windows" else ".sh"
        path = directory / f"spiritus-bundle-{platform}{suffix}"
        path.write_text(render_platform_script(config, platform), encoding="utf-8", newline="\n")
        if platform == "macos":
            path.chmod(path.stat().st_mode | 0o111)
        paths.append(path)
    return tuple(paths)


def check_environment(config: BundleConfig) -> list[str]:
    """Return missing local build prerequisites without running application hooks."""
    missing = []
    if importlib.util.find_spec("PyInstaller") is None:
        missing.append("PyInstaller (install spiritus[bundle] or add pyinstaller to the app dev group)")
    for package in config.spec.collect_packages:
        if importlib.util.find_spec(package) is None:
            missing.append(f"Python package {package}")
    for command_name in ("prepare", "verify", "installer"):
        command = config.commands(command_name)
        if command and shutil.which(command[0]) is None and not Path(command[0]).is_file():
            missing.append(f"{command_name} command {command[0]}")
    return missing


__all__ = [
    "CONFIG_NAME",
    "SUPPORTED_PLATFORMS",
    "BundleConfig",
    "check_environment",
    "init_bundle_config",
    "load_bundle_config",
    "render_config",
    "render_platform_script",
    "write_platform_scripts",
]
