"""Build a Spiritus application as a platform-local PyInstaller bundle.

The builder owns the reusable frozen-runtime pieces: collecting Spiritus,
staging declared files, resolving an optional bundled engine, and setting
resource paths before the application entry point runs. Applications declare
their own UI, configuration, optional packages, and integration binaries.

This module intentionally stops at the application bundle directory. Native
installers, signing, notarization, and release publication remain application
concerns until their platform contracts are stable.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_APP_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class BundleError(RuntimeError):
    """Raised when a bundle specification cannot be built safely."""


@dataclass(frozen=True, slots=True)
class BundleResource:
    """One file or directory copied into a PyInstaller bundle.

    ``target`` is the bundle directory passed to PyInstaller. A source file is
    copied into that directory; a source directory is copied as a directory.
    """

    source: Path | str
    target: str = "."


@dataclass(frozen=True, slots=True)
class BundleSpec:
    """Declarative inputs for a Spiritus application bundle."""

    project_root: Path
    entrypoint: Path | str
    name: str
    app_id: str
    version: str = ""
    datas: tuple[BundleResource, ...] = ()
    binaries: tuple[BundleResource, ...] = ()
    collect_packages: tuple[str, ...] = ()
    hidden_imports: tuple[str, ...] = ()
    runtime_env_paths: Mapping[str, str] = field(default_factory=dict)
    seed_files: Mapping[str, str] = field(default_factory=dict)
    output_dir: Path | None = None
    work_dir: Path | None = None
    console: bool = False
    icon: Path | str | None = None
    bundle_identifier: str | None = None
    variant: str = "production"
    install_dir: str | None = None
    config_dir: str | None = None
    workspace_dir: str | None = None
    update_channel: str | None = None
    runtime_env: Mapping[str, str] = field(default_factory=dict)
    installer_metadata: Mapping[str, str] = field(default_factory=dict)
    defer_resource_validation: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        root = Path(self.project_root).resolve()
        object.__setattr__(self, "project_root", root)
        object.__setattr__(self, "entrypoint", Path(self.entrypoint))
        for field_name in ("output_dir", "work_dir"):
            value = getattr(self, field_name)
            if value is not None:
                path = Path(value)
                if not path.is_absolute():
                    path = root / path
                object.__setattr__(self, field_name, path.resolve())
        object.__setattr__(self, "datas", tuple(self.datas))
        object.__setattr__(self, "binaries", tuple(self.binaries))
        object.__setattr__(self, "collect_packages", tuple(self.collect_packages))
        object.__setattr__(self, "hidden_imports", tuple(self.hidden_imports))
        object.__setattr__(self, "runtime_env_paths", dict(self.runtime_env_paths))
        object.__setattr__(self, "runtime_env", dict(self.runtime_env))
        object.__setattr__(self, "seed_files", dict(self.seed_files))
        object.__setattr__(self, "installer_metadata", dict(self.installer_metadata))
        self._validate()

    @property
    def resolved_entrypoint(self) -> Path:
        entrypoint = Path(self.entrypoint)
        if not entrypoint.is_absolute():
            entrypoint = self.project_root / entrypoint
        return _inside(self.project_root, entrypoint, "entrypoint")

    @property
    def resolved_icon(self) -> Path | None:
        if self.icon is None:
            return None
        icon = _inside(self.project_root, Path(self.icon), "icon")
        if not icon.is_file():
            raise BundleError(f"bundle icon does not exist: {icon}")
        return icon

    @property
    def resolved_output_dir(self) -> Path:
        return (self.output_dir or self.project_root / "dist").resolve()

    @property
    def resolved_work_dir(self) -> Path:
        return (self.work_dir or self.project_root / "build" / "spiritus").resolve()

    def _validate(self) -> None:
        if self.variant not in {"production", "dev"}:
            raise BundleError("bundle variant must be 'production' or 'dev'")
        if not self.name or self.name in {".", ".."} or "/" in self.name or "\\" in self.name:
            raise BundleError("bundle name must be one filesystem name")
        if not _APP_ID.fullmatch(self.app_id.strip()):
            raise BundleError(
                "app_id must start with a lowercase letter or digit and contain only "
                "lowercase letters, digits, '.', '_' or '-'")
        if not self.collect_packages or "spiritus" not in self.collect_packages:
            object.__setattr__(self, "collect_packages", ("spiritus", *self.collect_packages))
        _ = self.resolved_entrypoint
        _ = self.resolved_icon
        self._validate_resource_declarations()
        for variable, relative in self.runtime_env_paths.items():
            if not variable or not variable.replace("_", "").isalnum():
                raise BundleError(f"invalid runtime environment variable: {variable!r}")
            _bundle_relative(relative, "runtime resource")
        for variable, value in self.runtime_env.items():
            if not variable or not variable.replace("_", "").isalnum():
                raise BundleError(f"invalid runtime environment variable: {variable!r}")
            if not isinstance(value, str):
                raise BundleError(f"runtime environment value must be a string: {variable!r}")
        for source, target in self.seed_files.items():
            _bundle_relative(source, "seed source")
            _app_data_relative(target, "seed destination")
        for label, value in (
            ("install_dir", self.install_dir),
            ("config_dir", self.config_dir),
            ("workspace_dir", self.workspace_dir),
        ):
            if value is not None and not str(value).strip():
                raise BundleError(f"{label} must not be empty")
        for key, value in self.installer_metadata.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise BundleError("installer metadata keys and values must be strings")

    def _validate_resource_declarations(self) -> None:
        for resource in (*self.datas, *self.binaries):
            if not isinstance(resource, BundleResource):
                raise TypeError("datas and binaries must contain BundleResource values")
            source = _inside(self.project_root, Path(resource.source), "resource")
            if not self.defer_resource_validation and not source.exists():
                raise BundleError(f"bundle resource does not exist: {source}")
            _bundle_target(resource.target)

    def validate_resources(self) -> None:
        """Require every declared file or directory to exist before building."""
        for resource in (*self.datas, *self.binaries):
            source = _inside(self.project_root, Path(resource.source), "resource")
            if not source.exists():
                raise BundleError(f"bundle resource does not exist: {source}")


@dataclass(frozen=True, slots=True)
class BundleResult:
    """Paths and metadata produced by :func:`build_bundle`."""

    bundle_dir: Path
    manifest: Path
    spec_file: Path


def _inside(root: Path, value: Path, label: str) -> Path:
    candidate = value if value.is_absolute() else root / value
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BundleError(f"{label} must stay inside the project root: {value}") from exc
    return resolved


def _bundle_relative(value: str, label: str) -> str:
    raw = str(value).strip()
    path = PurePosixPath(raw.replace("\\", "/"))
    has_drive = len(raw) >= 2 and raw[1] == ":"
    if not raw or has_drive or path.is_absolute() or ".." in path.parts:
        raise BundleError(f"{label} must be a relative bundle path: {value!r}")
    return path.as_posix()


def _app_data_relative(value: str, label: str) -> str:
    return _bundle_relative(value, label)


def _bundle_target(value: str) -> str:
    return _bundle_relative(value or ".", "bundle target")


def _resource_tuple(resource: BundleResource, root: Path) -> tuple[str, str]:
    source = _inside(root, Path(resource.source), "resource")
    return str(source), _bundle_target(resource.target)


def _python_literal(value: object) -> str:
    return repr(value)


def _runtime_hook(spec: BundleSpec, directory: Path) -> Path | None:
    if not spec.runtime_env_paths and not spec.runtime_env and not spec.seed_files:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    hook = directory / "spiritus_bundle_runtime.py"
    payload = {
        "app_id": spec.app_id,
        "env_paths": dict(spec.runtime_env_paths),
        "env_values": dict(spec.runtime_env),
        "seed_files": dict(spec.seed_files),
    }
    hook.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import shutil\n"
        "import sys\n"
        "from spiritus.runtime.paths import app_data_dir\n"
        f"_CONFIG = {_python_literal(payload)}\n"
        "_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))\n"
        "for _name, _value in _CONFIG['env_values'].items():\n"
        "    os.environ.setdefault(_name, _value)\n"
        "for _name, _relative in _CONFIG['env_paths'].items():\n"
        "    _path = _ROOT / _relative\n"
        "    if _path.exists():\n"
        "        os.environ.setdefault(_name, str(_path))\n"
        "_data = app_data_dir(_CONFIG['app_id'])\n"
        "for _source, _target in _CONFIG['seed_files'].items():\n"
        "    _source_path = _ROOT / _source\n"
        "    _target_path = _data / _target\n"
        "    if _source_path.is_file() and not _target_path.exists():\n"
        "        _target_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "        shutil.copy2(_source_path, _target_path)\n",
        encoding="utf-8",
        newline="\n",
    )
    return hook


def render_spec(spec: BundleSpec, spec_file: Path, runtime_hook: Path | None) -> str:
    """Render the PyInstaller spec used by a bundle build."""
    root = spec.project_root
    datas = [_resource_tuple(resource, root) for resource in spec.datas]
    binaries = [_resource_tuple(resource, root) for resource in spec.binaries]
    hook_list = [str(runtime_hook)] if runtime_hook else []
    return f'''from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_all

ROOT = Path({_python_literal(str(root))})
ENGINE = ROOT / "__spiritus_no_engine__"
datas = []
binaries = []
hiddenimports = []
for package in {_python_literal(list(spec.collect_packages))}:
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hidden)
datas.extend({_python_literal(datas)})
binaries.extend({_python_literal(binaries)})
hiddenimports.extend({_python_literal(list(spec.hidden_imports))})

a = Analysis(
    [{_python_literal(str(spec.resolved_entrypoint))}],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks={_python_literal(hook_list)},
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name={_python_literal(spec.name)},
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console={_python_literal(spec.console)},
    icon={_python_literal(str(spec.resolved_icon)) if spec.resolved_icon else None},
)
if sys.platform == "darwin":
    BUNDLE(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        name={_python_literal(spec.name + ".app")},
        icon={_python_literal(str(spec.resolved_icon)) if spec.resolved_icon else None},
        bundle_identifier={_python_literal(spec.bundle_identifier)},
    )
else:
    COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        name={_python_literal(spec.name)},
    )
'''


def _manifest(spec: BundleSpec, bundle_dir: Path) -> Path:
    manifest = bundle_dir / "spiritus-bundle.json"
    files = []
    for path in sorted(bundle_dir.rglob("*")):
        if path.is_file() and path != manifest:
            files.append({
                "path": path.relative_to(bundle_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            })
    manifest.write_text(
        json.dumps({
            "format": 1,
            "spiritus": _package_version(),
            "app_id": spec.app_id,
            "name": spec.name,
            "version": spec.version,
            "variant": spec.variant,
            "bundle_identifier": spec.bundle_identifier,
            "install_dir": spec.install_dir,
            "config_dir": spec.config_dir,
            "workspace_dir": spec.workspace_dir,
            "update_channel": spec.update_channel,
            "installer": dict(spec.installer_metadata),
            "runtime_env": sorted(set(spec.runtime_env) | set(spec.runtime_env_paths)),
            "platform": sys.platform,
            "files": files,
        }, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def _package_version() -> str:
    source_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if source_pyproject.is_file():
        try:
            import tomllib

            with source_pyproject.open("rb") as handle:
                return tomllib.load(handle)["project"]["version"]
        except (KeyError, OSError, tomllib.TOMLDecodeError):
            pass
    try:
        from . import __version__
    except ImportError:
        return "unknown"
    return __version__


def _dev_version(version: str) -> str:
    """Append the development prerelease marker before build metadata."""
    value = str(version).strip() or _package_version()
    if value.endswith("-dev"):
        return value
    base, separator, build = value.partition("+")
    result = f"{base}-dev"
    return f"{result}{separator}{build}" if separator else result


def _sibling_variant_path(path: Path, suffix: str) -> Path:
    return path.parent / f"{path.name}-{suffix}"


def variant_spec(
    spec: BundleSpec,
    variant: str = "production",
    *,
    overrides: Mapping[str, object] | None = None,
) -> BundleSpec:
    """Return the effective spec for a production or development variant.

    Production is deliberately the identity transform. Development derives a
    separate identity and writable-data namespace while retaining the source
    assets and dependency declarations from the base spec.
    """
    selected = str(variant).strip().lower() or "production"
    if selected == "production":
        if not overrides:
            return spec
        raise BundleError("production variants do not accept overrides")
    if selected != "dev":
        raise BundleError("bundle variant must be 'production' or 'dev'")

    values = dict(overrides or {})
    name = str(values.get("name") or f"{spec.name} Dev")
    app_id = str(values.get("app_id") or f"{spec.app_id}-dev")
    version = str(values.get("version") or _dev_version(spec.version))
    bundle_identifier = values.get("bundle_identifier")
    if bundle_identifier is None:
        bundle_identifier = (
            f"{spec.bundle_identifier}.dev" if spec.bundle_identifier else f"{app_id}.dev"
        )
    output_dir = values.get("output_dir")
    if output_dir is None:
        output_dir = _sibling_variant_path(spec.resolved_output_dir, "dev")
    work_dir = values.get("work_dir")
    if work_dir is None:
        work_dir = _sibling_variant_path(spec.resolved_work_dir, "dev")
    install_dir = str(values.get("install_dir") or name)
    config_dir = str(values.get("config_dir") or app_id)
    workspace_dir = str(values.get("workspace_dir") or "workspace-dev")
    update_channel = str(values.get("update_channel") or "dev")

    runtime_env = dict(spec.runtime_env)
    runtime_env.update({
        "SPIRITUS_APP_VARIANT": "dev",
        "SPIRITUS_APP_ID": app_id,
        "SPIRITUS_APP_TITLE": name,
        "SPIRITUS_CONFIG_DIR": config_dir,
        "SPIRITUS_WORKSPACE_DIRNAME": workspace_dir,
        "SPIRITUS_UPDATE_CHANNEL": update_channel,
    })
    runtime_env.update({
        str(key): str(value)
        for key, value in dict(values.get("runtime_env") or {}).items()
    })
    runtime_env_paths = dict(spec.runtime_env_paths)
    runtime_env_paths.update({
        str(key): str(value)
        for key, value in dict(values.get("runtime_env_paths") or {}).items()
    })
    installer_metadata = dict(spec.installer_metadata)
    installer_metadata.update({
        "variant": "dev",
        "name": name,
        "app_id": app_id,
        "version": version,
        "install_dir": install_dir,
        "config_dir": config_dir,
        "workspace_dir": workspace_dir,
        "update_channel": update_channel,
    })
    installer_metadata.update({
        str(key): str(value)
        for key, value in dict(values.get("installer_metadata") or {}).items()
    })
    return BundleSpec(
        project_root=spec.project_root,
        entrypoint=values.get("entrypoint", spec.entrypoint),
        name=name,
        app_id=app_id,
        version=version,
        datas=spec.datas,
        binaries=spec.binaries,
        collect_packages=spec.collect_packages,
        hidden_imports=spec.hidden_imports,
        runtime_env_paths=runtime_env_paths,
        runtime_env=runtime_env,
        seed_files=spec.seed_files,
        output_dir=output_dir,
        work_dir=work_dir,
        console=spec.console,
        icon=spec.icon,
        bundle_identifier=str(bundle_identifier),
        variant="dev",
        install_dir=install_dir,
        config_dir=config_dir,
        workspace_dir=workspace_dir,
        update_channel=update_channel,
        installer_metadata=installer_metadata,
        defer_resource_validation=spec.defer_resource_validation,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_bundle(spec: BundleSpec) -> BundleResult:
    """Build and manifest a one-folder application bundle.

    PyInstaller is intentionally optional. Applications that want to build
    installable artifacts should install ``spiritus[bundle]`` or provide their
    own pinned PyInstaller environment, as Persona does.
    """
    spec.validate_resources()
    output_dir = spec.resolved_output_dir
    work_dir = spec.resolved_work_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = output_dir / (spec.name + ".app" if sys.platform == "darwin" else spec.name)
    if bundle_dir.exists():
        if bundle_dir.is_dir() and bundle_dir.parent == output_dir:
            shutil.rmtree(bundle_dir)
        else:
            raise BundleError(f"refusing to remove unexpected bundle path: {bundle_dir}")

    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        raise BundleError(
            "PyInstaller is required; install 'spiritus[bundle]' or provide a pinned build environment"
        ) from exc

    final_spec = work_dir / f"{spec.name}.spec"
    runtime_hook = _runtime_hook(spec, work_dir)
    final_spec.write_text(
        render_spec(spec, final_spec, runtime_hook), encoding="utf-8", newline="\n"
    )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(output_dir),
        "--workpath",
        str(work_dir / "pyinstaller"),
        str(final_spec),
    ]
    try:
        subprocess.run(command, cwd=spec.project_root, check=True)
    except FileNotFoundError as exc:
        raise BundleError("the build Python environment cannot run PyInstaller") from exc
    except subprocess.CalledProcessError as exc:
        raise BundleError(f"PyInstaller failed with exit code {exc.returncode}") from exc

    manifest = _manifest(spec, bundle_dir)
    return BundleResult(bundle_dir=bundle_dir, manifest=manifest, spec_file=final_spec)


def check_bundle(
    bundle_dir: Path,
    *,
    app_id: str | None = None,
    variant: str | None = None,
) -> dict:
    """Validate the Spiritus manifest and return its decoded metadata."""
    bundle_dir = Path(bundle_dir).resolve()
    manifest = bundle_dir / "spiritus-bundle.json"
    if not manifest.is_file():
        raise BundleError(f"missing Spiritus bundle manifest: {manifest}")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid Spiritus bundle manifest: {manifest}") from exc
    if payload.get("format") != 1:
        raise BundleError("unsupported Spiritus bundle manifest format")
    if app_id is not None and payload.get("app_id") != app_id:
        raise BundleError(
            f"bundle app_id is {payload.get('app_id')!r}, expected {app_id!r}"
        )
    if variant is not None and payload.get("variant", "production") != variant:
        raise BundleError(
            f"bundle variant is {payload.get('variant', 'production')!r}, expected {variant!r}"
        )
    missing = []
    changed = []
    for item in payload.get("files", []):
        relative = _bundle_relative(item.get("path", ""), "manifest file")
        path = bundle_dir / relative
        if not path.is_file():
            missing.append(relative)
            continue
        if "bytes" in item and path.stat().st_size != item["bytes"]:
            changed.append(relative)
            continue
        if item.get("sha256") and _sha256(path) != item["sha256"]:
            changed.append(relative)
    if missing:
        raise BundleError(f"bundle manifest lists missing files: {missing!r}")
    if changed:
        raise BundleError(f"bundle manifest files changed: {changed!r}")
    return payload


__all__ = [
    "BundleError",
    "BundleResource",
    "BundleResult",
    "BundleSpec",
    "build_bundle",
    "check_bundle",
    "render_spec",
    "variant_spec",
]
