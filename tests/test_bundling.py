"""Deterministic tests for the application-owned bundle boundary."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spiritus.bundling import (
    BundleError,
    BundleResource,
    BundleSpec,
    check_bundle,
    render_spec,
)


def _spec(tmp_path: Path) -> BundleSpec:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<h1>Hi</h1>\n", encoding="utf-8")
    return BundleSpec(
        project_root=tmp_path,
        entrypoint="main.py",
        name="Example",
        app_id="example-app",
        version="1.2.3",
        datas=(BundleResource("ui", "ui"),),
        binaries=(BundleResource("main.py", "scripts"),),
        collect_packages=("webview",),
        hidden_imports=("example.hidden",),
        runtime_env_paths={"BROWSER_PATH": "browsers"},
        seed_files={"opencode.json": "opencode.json"},
    )


def test_spec_always_collects_spiritus_and_preserves_app_inputs(tmp_path):
    spec = _spec(tmp_path)

    assert spec.collect_packages == ("spiritus", "webview")
    rendered = render_spec(spec, tmp_path / "example.spec", None)
    assert "collect_all(package)" in rendered
    assert "webview" in rendered
    assert "example.hidden" in rendered
    assert "ui" in rendered


def test_runtime_hook_is_rendered_for_external_runtime_resources(tmp_path):
    spec = _spec(tmp_path)
    from spiritus.bundling import _runtime_hook

    hook = _runtime_hook(spec, tmp_path / "hooks")

    assert hook is not None
    source = hook.read_text(encoding="utf-8")
    assert "BROWSER_PATH" in source
    assert "opencode.json" in source
    assert "example-app" in source


@pytest.mark.parametrize("value", ["../outside", "/absolute", "C:\\outside"])
def test_bundle_targets_cannot_escape_the_bundle(value, tmp_path):
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(BundleError, match="bundle target"):
        BundleSpec(
            project_root=tmp_path,
            entrypoint="main.py",
            name="Example",
            app_id="example-app",
            datas=(BundleResource("main.py", value),),
        )


def test_resources_cannot_escape_the_project(tmp_path):
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    with pytest.raises(BundleError, match="inside the project root"):
        BundleSpec(
            project_root=tmp_path,
            entrypoint="main.py",
            name="Example",
            app_id="example-app",
            datas=(BundleResource(outside, "."),),
        )


def test_check_bundle_validates_manifest_files(tmp_path):
    bundle = tmp_path / "Example"
    bundle.mkdir()
    (bundle / "Example.exe").write_bytes(b"binary")
    digest = hashlib.sha256(b"binary").hexdigest()
    (bundle / "spiritus-bundle.json").write_text(
        json.dumps({
            "format": 1,
            "app_id": "example-app",
            "name": "Example",
            "version": "1.2.3",
            "files": [{"path": "Example.exe", "bytes": 6, "sha256": digest}],
        }),
        encoding="utf-8",
    )

    payload = check_bundle(bundle, app_id="example-app")

    assert payload["name"] == "Example"


def test_check_bundle_rejects_missing_manifest_files(tmp_path):
    bundle = tmp_path / "Example"
    bundle.mkdir()
    (bundle / "spiritus-bundle.json").write_text(
        json.dumps({"format": 1, "files": [{"path": "missing.dll"}]}),
        encoding="utf-8",
    )

    with pytest.raises(BundleError, match="missing files"):
        check_bundle(bundle)


def test_check_bundle_rejects_changed_manifest_files(tmp_path):
    bundle = tmp_path / "Example"
    bundle.mkdir()
    (bundle / "Example.exe").write_bytes(b"changed")
    (bundle / "spiritus-bundle.json").write_text(
        json.dumps({
            "format": 1,
            "files": [{"path": "Example.exe", "bytes": 6, "sha256": "wrong"}],
        }),
        encoding="utf-8",
    )

    with pytest.raises(BundleError, match="files changed"):
        check_bundle(bundle)
