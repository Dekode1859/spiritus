"""Engine provisioning: resolution order, platform mapping, version policy, install.

Nothing here touches the network. The download is exercised against a locally
built archive so the extract-and-place path is real without pulling ~60 MB.
"""
from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from spiritus import engine
from spiritus.__main__ import main as cli_main


class TestResolution:
    """Order matters: an explicit override must never be second-guessed."""

    def test_env_override_wins_over_path(self, tmp_path, monkeypatch):
        override = tmp_path / "my-opencode"
        override.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv(engine.ENV_BIN, str(override))
        monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/opencode")
        assert engine.resolve() == override

    def test_env_override_pointing_at_nothing_resolves_to_none(self, tmp_path, monkeypatch):
        """An operator who set the variable wrongly should be told, not silently
        given some other engine they did not ask for."""
        monkeypatch.setenv(engine.ENV_BIN, str(tmp_path / "absent"))
        monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/opencode")
        assert engine.resolve() is None

    def test_blank_env_override_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv(engine.ENV_BIN, "   ")
        monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/opencode")
        assert engine.resolve() == Path("/usr/bin/opencode")

    def test_path_wins_over_cache(self, tmp_path, monkeypatch):
        monkeypatch.delenv(engine.ENV_BIN, raising=False)
        monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/opencode")
        monkeypatch.setattr(engine, "cache_root", lambda: tmp_path)
        cached = tmp_path / engine.PINNED_VERSION / engine._binary_name()
        cached.parent.mkdir(parents=True)
        cached.write_text("x", encoding="utf-8")
        assert engine.resolve() == Path("/usr/bin/opencode")

    def test_cache_is_used_when_nothing_else_is_available(self, tmp_path, monkeypatch):
        monkeypatch.delenv(engine.ENV_BIN, raising=False)
        monkeypatch.setattr(engine.shutil, "which", lambda _: None)
        monkeypatch.setattr(engine, "cache_root", lambda: tmp_path)
        cached = tmp_path / engine.PINNED_VERSION / engine._binary_name()
        cached.parent.mkdir(parents=True)
        cached.write_text("x", encoding="utf-8")
        assert engine.resolve() == cached

    def test_resolve_returns_none_when_there_is_no_engine(self, tmp_path, monkeypatch):
        monkeypatch.delenv(engine.ENV_BIN, raising=False)
        monkeypatch.setattr(engine.shutil, "which", lambda _: None)
        monkeypatch.setattr(engine, "cache_root", lambda: tmp_path)
        assert engine.resolve() is None

    def test_resolve_never_downloads(self, tmp_path, monkeypatch):
        """The whole point of the explicit command: resolution is offline."""
        monkeypatch.delenv(engine.ENV_BIN, raising=False)
        monkeypatch.setattr(engine.shutil, "which", lambda _: None)
        monkeypatch.setattr(engine, "cache_root", lambda: tmp_path)

        def explode(*a, **k):
            raise AssertionError("resolve() attempted a download")

        monkeypatch.setattr(engine, "install", explode)
        assert engine.resolve() is None

    def test_missing_engine_message_names_the_command(self):
        msg = engine.missing_engine_message()
        assert "spiritus install-engine" in msg
        assert engine.ENV_BIN in msg


class TestPlatformMapping:
    @pytest.mark.parametrize("plat,machine,expected", [
        ("win32",  "AMD64",   "opencode-windows-x64.zip"),
        ("win32",  "ARM64",   "opencode-windows-arm64.zip"),
        ("darwin", "x86_64",  "opencode-darwin-x64.zip"),
        ("darwin", "arm64",   "opencode-darwin-arm64.zip"),
        ("linux",  "x86_64",  "opencode-linux-x64.tar.gz"),
        ("linux",  "aarch64", "opencode-linux-arm64.tar.gz"),
    ])
    def test_asset_name_per_platform(self, monkeypatch, plat, machine, expected):
        monkeypatch.setattr(engine.sys, "platform", plat)
        monkeypatch.setattr(engine.platform, "machine", lambda: machine)
        monkeypatch.setattr(engine, "_is_musl", lambda: False)
        assert engine.asset_name() == expected

    def test_musl_linux_gets_the_musl_build(self, monkeypatch):
        monkeypatch.setattr(engine.sys, "platform", "linux")
        monkeypatch.setattr(engine.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(engine, "_is_musl", lambda: True)
        assert engine.asset_name() == "opencode-linux-x64-musl.tar.gz"

    def test_unsupported_platform_is_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(engine.sys, "platform", "sunos5")
        with pytest.raises(RuntimeError, match="platform"):
            engine.asset_name()

    def test_unsupported_architecture_is_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(engine.sys, "platform", "linux")
        monkeypatch.setattr(engine.platform, "machine", lambda: "mips")
        with pytest.raises(RuntimeError, match="architecture"):
            engine.asset_name()

    def test_download_url_targets_the_pinned_release(self):
        url = engine.download_url()
        assert url.startswith("https://github.com/sst/opencode/releases/download/")
        assert f"/v{engine.PINNED_VERSION}/" in url
        assert url.endswith(engine.asset_name())

    def test_cache_root_is_per_user_not_inside_the_repo(self):
        root = engine.cache_root()
        assert root.parts[-2:] == ("spiritus", "engine")
        assert Path.cwd() not in root.parents


class TestVersionPolicy:
    @pytest.mark.parametrize("text,expected", [
        ("1.18.13", (1, 18, 13)),
        ("v1.18.13", (1, 18, 13)),
        ("1.18.13\n", (1, 18, 13)),
        ("1.18.13-beta.1", (1, 18, 13)),
        ("1.18", (1, 18)),
    ])
    def test_parses_version_strings(self, text, expected):
        assert engine.parse_version(text) == expected

    @pytest.mark.parametrize("text", ["", "unknown", "not.a.version"])
    def test_unparseable_versions_are_none(self, text):
        assert engine.parse_version(text) is None

    def test_supported_version_produces_no_warning(self):
        assert engine.version_warning(engine.PINNED_VERSION) is None

    def test_the_pin_is_inside_the_supported_range(self):
        """A pin outside its own range would warn on every launch."""
        parsed = engine.parse_version(engine.PINNED_VERSION)
        assert engine.MIN_VERSION <= parsed < engine.MAX_VERSION_EXCLUSIVE

    def test_older_than_minimum_warns_and_says_how_to_fix(self):
        warning = engine.version_warning("1.16.0")
        assert warning and "older" in warning
        assert "spiritus install-engine" in warning

    def test_newer_major_warns_about_api_drift(self):
        warning = engine.version_warning("2.0.0")
        assert warning and "newer" in warning

    def test_unknown_version_warns_rather_than_assuming_good(self):
        assert engine.version_warning(None) is not None

    def test_a_patch_ahead_of_the_pin_is_accepted(self):
        """Advisory by design — users should not be blocked by a patch bump."""
        major, minor, patch = engine.parse_version(engine.PINNED_VERSION)
        assert engine.version_warning(f"{major}.{minor}.{patch + 1}") is None

    def test_binary_version_reads_the_engines_own_output(self, tmp_path, monkeypatch):
        class Result:
            stdout, stderr = "1.18.13\n", ""

        monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: Result())
        assert engine.binary_version(tmp_path / "opencode") == "1.18.13"

    def test_binary_version_is_none_when_the_binary_will_not_run(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise OSError("not executable")

        monkeypatch.setattr(engine.subprocess, "run", boom)
        assert engine.binary_version(tmp_path / "opencode") is None


def _fake_release(dest: Path, asset: str, binary_name: str,
                  arcname: str | None = None) -> bytes:
    """Build an archive shaped like a real release asset.

    The published assets contain exactly one entry: the binary at the archive
    root (verified against opencode-linux-x64.tar.gz and
    opencode-darwin-arm64.zip). ``arcname`` overrides that to exercise layouts
    the extractor must survive.
    """
    payload = b"#!/bin/sh\necho 9.9.9\n"
    name = arcname or binary_name
    buf = io.BytesIO()
    if asset.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(name, payload)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class FakeResponse:
    status_code = 200

    def __init__(self, body: bytes):
        self._body = body
        self.headers = {"content-length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        yield self._body


class TestInstall:
    @pytest.fixture
    def cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "cache_root", lambda: tmp_path / "cache")
        return tmp_path

    def test_downloads_extracts_and_places_the_binary(self, cached, monkeypatch):
        asset = engine.asset_name()
        body = _fake_release(cached, asset, engine._binary_name())
        monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse(body))

        path = engine.install("9.9.9")
        assert path.is_file()
        assert path.name == engine._binary_name()
        assert path.parent.name == "9.9.9"
        assert path.read_bytes().startswith(b"#!")

    def test_a_binary_nested_under_a_same_named_directory_is_found(self, cached, monkeypatch):
        """Regression: `rglob` matched the directory, not the binary inside it.

        On POSIX the binary is named `opencode`, so an archive laying it out as
        `opencode/bin/opencode` made _extract return the *directory*. install()
        then moved a directory into the cache, and every later resolve() handed
        back something that could not be executed. Windows never saw it: there
        the binary is `opencode.exe`, which cannot collide with `opencode/`.
        """
        binary = engine._binary_name()
        body = _fake_release(cached, engine.asset_name(), binary,
                             arcname=f"opencode/bin/{binary}")
        monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse(body))

        path = engine.install("9.9.9")
        assert path.is_file(), f"{path} is a directory, not the engine binary"
        assert path.read_bytes().startswith(b"#!")

    def test_a_top_level_binary_wins_over_a_deeper_copy(self, cached, monkeypatch):
        binary = engine._binary_name()
        asset = engine.asset_name()
        payload = b"#!/bin/sh\necho top\n"
        buf = io.BytesIO()
        if asset.endswith(".zip"):
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(f"vendor/nested/{binary}", b"#!/bin/sh\necho nested\n")
                zf.writestr(binary, payload)
        else:
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for arc, data in ((f"vendor/nested/{binary}", b"#!/bin/sh\necho nested\n"),
                                  (binary, payload)):
                    info = tarfile.TarInfo(arc)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
        monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse(buf.getvalue()))

        assert engine.install("9.9.9").read_bytes() == payload

    def test_reports_progress_while_downloading(self, cached, monkeypatch):
        body = _fake_release(cached, engine.asset_name(), engine._binary_name())
        monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse(body))
        seen: list[tuple[int, int | None]] = []
        engine.install("9.9.9", on_progress=lambda d, t: seen.append((d, t)))
        assert seen and seen[-1][0] == len(body)

    def test_is_idempotent_and_does_not_redownload(self, cached, monkeypatch):
        body = _fake_release(cached, engine.asset_name(), engine._binary_name())
        calls = {"n": 0}

        def counting_get(*a, **k):
            calls["n"] += 1
            return FakeResponse(body)

        monkeypatch.setattr("requests.get", counting_get)
        first = engine.install("9.9.9")
        second = engine.install("9.9.9")
        assert first == second
        assert calls["n"] == 1

    def test_force_redownloads_over_an_existing_copy(self, cached, monkeypatch):
        body = _fake_release(cached, engine.asset_name(), engine._binary_name())
        calls = {"n": 0}

        def counting_get(*a, **k):
            calls["n"] += 1
            return FakeResponse(body)

        monkeypatch.setattr("requests.get", counting_get)
        engine.install("9.9.9")
        engine.install("9.9.9", force=True)
        assert calls["n"] == 2

    def test_a_missing_release_gives_an_actionable_error(self, cached, monkeypatch):
        class NotFound(FakeResponse):
            status_code = 404

        monkeypatch.setattr("requests.get", lambda *a, **k: NotFound(b""))
        with pytest.raises(RuntimeError, match="No engine build published"):
            engine.install("0.0.0-nope")

    def test_an_archive_without_a_binary_is_rejected(self, cached, monkeypatch):
        buf = io.BytesIO()
        if engine.asset_name().endswith(".zip"):
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("README.md", "nothing useful")
        else:
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                info = tarfile.TarInfo("README.md")
                info.size = 3
                tf.addfile(info, io.BytesIO(b"abc"))
        monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse(buf.getvalue()))
        with pytest.raises(RuntimeError, match="No engine binary found"):
            engine.install("9.9.9")

    def test_ensure_returns_an_existing_engine_without_downloading(self, cached, monkeypatch):
        monkeypatch.setenv(engine.ENV_BIN, "")
        monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/opencode")

        def explode(*a, **k):
            raise AssertionError("ensure() downloaded despite an engine being present")

        monkeypatch.setattr(engine, "install", explode)
        assert engine.ensure() == Path("/usr/bin/opencode")


class TestCli:
    def test_engine_path_prints_the_resolved_engine(self, monkeypatch, capsys):
        monkeypatch.setattr(engine, "resolve", lambda *a, **k: Path("/usr/bin/opencode"))
        assert cli_main(["engine-path"]) == 0
        assert "opencode" in capsys.readouterr().out

    def test_engine_path_fails_loudly_when_absent(self, monkeypatch, capsys):
        monkeypatch.setattr(engine, "resolve", lambda *a, **k: None)
        assert cli_main(["engine-path"]) == 1
        assert "install-engine" in capsys.readouterr().err

    def test_install_engine_skips_when_one_is_already_present(self, monkeypatch, capsys):
        monkeypatch.setattr(engine, "resolve", lambda *a, **k: Path("/usr/bin/opencode"))
        monkeypatch.setattr(engine, "binary_version", lambda _: engine.PINNED_VERSION)

        def explode(*a, **k):
            raise AssertionError("install-engine downloaded despite an existing engine")

        monkeypatch.setattr(engine, "install", explode)
        assert cli_main(["install-engine"]) == 0
        assert "already available" in capsys.readouterr().out

    def test_install_engine_reports_failure_as_a_nonzero_exit(self, monkeypatch, capsys):
        monkeypatch.setattr(engine, "resolve", lambda *a, **k: None)

        def boom(*a, **k):
            raise RuntimeError("network unreachable")

        monkeypatch.setattr(engine, "install", boom)
        assert cli_main(["install-engine"]) == 1
        assert "network unreachable" in capsys.readouterr().err

    def test_engine_info_reports_missing_engine_as_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(engine, "resolve", lambda *a, **k: None)
        assert cli_main(["engine-info"]) == 1
        assert "pinned engine" in capsys.readouterr().out

    def test_a_command_is_required(self):
        with pytest.raises(SystemExit):
            cli_main([])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable bit only")
def test_installed_binary_is_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "cache_root", lambda: tmp_path / "cache")
    body = _fake_release(tmp_path, engine.asset_name(), engine._binary_name())
    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse(body))
    path = engine.install("9.9.9")
    assert path.stat().st_mode & 0o111
