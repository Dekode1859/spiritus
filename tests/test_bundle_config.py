"""Tests for repository-owned Spiritus bundle specifications."""
from __future__ import annotations

from pathlib import Path

from spiritus.__main__ import main as cli_main
from spiritus.bundle_config import (
    init_bundle_config,
    load_bundle_config,
    render_platform_script,
)


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
