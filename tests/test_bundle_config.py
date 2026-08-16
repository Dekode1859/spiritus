"""Tests for repository-owned Spiritus bundle specifications."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import spiritus.__main__ as cli
from spiritus.__main__ import main as cli_main
from spiritus.bundle_config import (
    init_bundle_config,
    load_bundle_config,
    render_platform_script,
)
from spiritus.bundling import BundleError


def test_bundle_init_detects_project_metadata_and_generates_windows_script(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "Example Desktop"\nversion = "2.3.4"\n',
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "ui").mkdir()

    config = init_bundle_config(tmp_path, platform="windows")

    assert config.spec.name == "Example Desktop"
    assert config.spec.app_id == "example-desktop"
    assert config.spec.version == "2.3.4"
    assert config.spec.resolved_entrypoint == tmp_path / "main.py"
    assert config.spec.datas[0].source == "ui"
    script = render_platform_script(config, "windows")
    assert "spiritus bundle --project-root $Root" in script
    assert "bundle-check" in script


def test_bundle_spec_round_trips_resources_and_hooks(tmp_path: Path):
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "asset.txt").write_text("asset\n", encoding="utf-8")
    config_path = tmp_path / "spiritus.bundle.toml"
    config_path.write_text(
        """format = 1
platforms = ["windows", "macos"]
entrypoint = "main.py"
name = "Example"
app_id = "example"
version = "1.0.0"
datas = ["asset.txt=."]
binaries = []
collect_packages = []
hidden_imports = []

[runtime_env_paths]
BROWSER_PATH = "browsers"

[seed_files]
"config.json" = "config.json"

[hooks]
prepare = ["uv", "run", "python", "prepare.py"]
verify = ["{bundle}/Example.exe", "--check-bundle"]
""",
        encoding="utf-8",
    )

    config = load_bundle_config(config_path, project_root=tmp_path)

    assert config.spec.datas[0].target == "."
    assert config.spec.runtime_env_paths == {"BROWSER_PATH": "browsers"}
    assert config.commands("verify", "windows")[-1] == "--check-bundle"


def test_bundle_spec_round_trips_generic_update_source(tmp_path: Path):
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    config_path = tmp_path / "spiritus.bundle.toml"
    config_path.write_text(
        """format = 1
platforms = ["windows"]
entrypoint = "main.py"
name = "Example"
app_id = "example"
version = "1.0.0"

[updates]
channel = "stable"
versioning = "semver"

[updates.source]
type = "json"
url = "https://downloads.example.test/stable.json"

[updates.assets]
windows_x86_64 = "Example-{version}.exe"
""",
        encoding="utf-8",
    )

    config = load_bundle_config(config_path, project_root=tmp_path)

    assert config.updates is not None
    assert config.updates.app_id == "example"
    assert config.updates.current_version == "1.0.0"
    assert config.updates.asset_patterns["windows_x86_64"] == "Example-{version}.exe"


def test_dev_variant_reuses_assets_and_overrides_identity_hooks_and_updates(tmp_path: Path):
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "main_dev.py").write_text("pass\n", encoding="utf-8")
    config_path = tmp_path / "spiritus.bundle.toml"
    config_path.write_text(
        """format = 1
platforms = ["windows"]
entrypoint = "main.py"
name = "Example"
app_id = "example"
version = "2.3.4"
datas = ["main.py=."]

[updates]
enabled = true
channel = "stable"
versioning = "semver"

[updates.source]
type = "json"
url = "https://downloads.example.test/stable.json"

[hooks]
verify = ["{executable}", "--check-bundle"]

[variants.dev]
entrypoint = "main_dev.py"
output_dir = "dist-dev"
work_dir = "build/dev"
version_suffix = "-preview"
update_channel = "nightly"

[variants.dev.runtime_env]
PERSONA_APP_ID = "example-dev"

[variants.dev.hooks]
verify = ["{executable}", "--check-dev-bundle"]
""",
        encoding="utf-8",
    )

    config = load_bundle_config(config_path, project_root=tmp_path, variant="dev")

    assert config.spec.entrypoint == Path("main_dev.py")
    assert config.spec.name == "Example Dev"
    assert config.spec.app_id == "example-dev"
    assert config.spec.version == "2.3.4-preview"
    assert config.spec.update_channel == "nightly"
    assert config.spec.runtime_env["PERSONA_APP_ID"] == "example-dev"
    assert config.commands("verify", "windows")[-1] == "--check-dev-bundle"
    assert config.updates is not None
    assert config.updates.app_id == "example-dev"
    assert config.updates.channel == "nightly"
    assert config.spec.datas[0].source == "main.py"


def test_prepare_hook_can_create_resources_before_build_validation(tmp_path: Path):
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    config_path = tmp_path / "spiritus.bundle.toml"
    config_path.write_text(
        """format = 1
platforms = ["windows"]
entrypoint = "main.py"
name = "Example"
app_id = "example"
datas = ["generated=generated"]
binaries = []

[hooks]
prepare = ["python", "prepare.py"]
""",
        encoding="utf-8",
    )

    deferred = load_bundle_config(
        config_path,
        project_root=tmp_path,
        defer_resource_validation=True,
    )
    assert deferred.spec.datas[0].source == "generated"
    with pytest.raises(BundleError, match="bundle resource does not exist"):
        deferred.spec.validate_resources()

    (tmp_path / "generated").mkdir()
    deferred.spec.validate_resources()


def test_bundle_cli_runs_prepare_before_build_validation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "prepare.py").write_text(
        "from pathlib import Path\nPath('generated').mkdir()\n",
        encoding="utf-8",
    )
    (tmp_path / "spiritus.bundle.toml").write_text(
        """format = 1
platforms = ["windows", "macos"]
entrypoint = "main.py"
name = "Example"
app_id = "example"
datas = ["generated=generated"]

[hooks]
prepare = ["python", "prepare.py"]
""",
        encoding="utf-8",
    )

    def fake_build(spec):
        spec.validate_resources()
        return SimpleNamespace(
            bundle_dir=tmp_path / "dist" / "Example",
            manifest=tmp_path / "dist" / "spiritus-bundle.json",
            spec_file=tmp_path / "dist" / "spiritus-bundle.spec",
        )

    monkeypatch.setattr(cli, "build_bundle", fake_build)
    assert cli_main(["bundle", "--project-root", str(tmp_path)]) == 0
    assert (tmp_path / "generated").is_dir()


def test_bundle_and_bundle_check_cover_production_and_dev_variants(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "spiritus.bundle.toml").write_text(
        """format = 1
platforms = ["windows"]
entrypoint = "main.py"
name = "Example"
app_id = "example"
version = "3.0.0"
""",
        encoding="utf-8",
    )

    def fake_build(spec):
        bundle_dir = spec.resolved_output_dir / spec.name
        bundle_dir.mkdir(parents=True, exist_ok=True)
        executable = bundle_dir / f"{spec.name}.exe"
        executable.write_bytes(spec.variant.encode("ascii"))
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        manifest = bundle_dir / "spiritus-bundle.json"
        manifest.write_text(
            json.dumps({
                "format": 1,
                "app_id": spec.app_id,
                "name": spec.name,
                "version": spec.version,
                "variant": spec.variant,
                "files": [{
                    "path": executable.name,
                    "bytes": executable.stat().st_size,
                    "sha256": digest,
                }],
            }),
            encoding="utf-8",
        )
        return SimpleNamespace(
            bundle_dir=bundle_dir,
            manifest=manifest,
            spec_file=tmp_path / f"{spec.name}.spec",
        )

    monkeypatch.setattr(cli, "build_bundle", fake_build)
    monkeypatch.setattr(cli, "check_environment", lambda config: [])

    assert cli_main(["bundle", "--project-root", str(tmp_path)]) == 0
    assert cli_main(["bundle", "--project-root", str(tmp_path), "--variant", "dev"]) == 0
    assert cli_main([
        "bundle-check", str(tmp_path / "dist" / "Example"), "--variant", "production",
        "--app-id", "example",
    ]) == 0
    assert cli_main([
        "bundle-check", str(tmp_path / "dist-dev" / "Example Dev"), "--variant", "dev",
        "--app-id", "example-dev",
    ]) == 0


def test_bundle_init_cli_reports_created_files(tmp_path: Path, capsys):
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")

    assert cli_main([
        "bundle",
        "init",
        "--project-root",
        str(tmp_path),
        "--platform",
        "macos",
    ]) == 0

    output = capsys.readouterr().out
    assert "spiritus.bundle.toml" in output
    assert (tmp_path / "packaging" / "spiritus-bundle-macos.sh").is_file()
