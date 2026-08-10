"""Offline tests for Spiritus's check-only update discovery."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from spiritus import (
    GitHubReleaseSource,
    GitLabReleaseSource,
    JsonFeedSource,
    SemVerPolicy,
    StagedUpdate,
    SubprocessInstallerHandoff,
    UpdateArtifact,
    UpdateClient,
    UpdateConfig,
    UpdateDownloader,
    UpdateDownloadError,
    UpdateStatus,
    UpdateVerificationError,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, *, headers, timeout):
        self.calls.append((url, headers, timeout))
        return FakeResponse(self.payload)


class FakeDownloadResponse:
    def __init__(self, payload: bytes, *, url: str = "https://downloads.example.test/app.exe"):
        self.payload = payload
        self.url = url
        self.headers = {"content-length": str(len(payload))}
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, *, chunk_size):
        for offset in range(0, len(self.payload), max(1, chunk_size)):
            yield self.payload[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class FakeDownloadSession:
    def __init__(self, response: FakeDownloadResponse):
        self.response = response
        self.calls = []

    def get(self, url, *, headers, timeout, stream):
        self.calls.append((url, headers, timeout, stream))
        return self.response


def test_semver_policy_accepts_tags_and_orders_prereleases():
    policy = SemVerPolicy()

    assert policy.normalize("v1.2.3") == "1.2.3"
    assert policy.compare("1.0.0", "1.0.0-rc.1") > 0
    assert policy.compare("1.0.0-beta.2", "1.0.0-beta.11") < 0
    assert policy.compare("1.0.0+build.1", "1.0.0+build.2") == 0

    with pytest.raises(ValueError, match="invalid SemVer"):
        policy.normalize("1.2")


def test_github_source_selects_matching_windows_asset():
    session = FakeSession(
        [
            {
                "tag_name": "v0.1.1",
                "prerelease": False,
                "published_at": "2026-08-11T00:00:00Z",
                "html_url": "https://github.com/example/persona/releases/tag/v0.1.1",
                "body": "Fixes",
                "assets": [
                    {
                        "name": "Persona-Setup-0.1.1.exe",
                        "browser_download_url": "https://example.test/Persona-Setup-0.1.1.exe",
                        "digest": "sha256:abc123",
                        "size": 123,
                    },
                    {
                        "name": "Persona-0.1.1-macos.dmg",
                        "browser_download_url": "https://example.test/Persona-0.1.1-macos.dmg",
                    },
                ],
            },
            {
                "tag_name": "v0.2.0-beta.1",
                "prerelease": True,
                "assets": [],
            },
        ]
    )
    source = GitHubReleaseSource("example/persona", session=session)
    config = UpdateConfig(
        app_id="persona",
        current_version="0.1.0",
        source=source,
        asset_patterns={"windows_x86_64": "Persona-Setup-{version}.exe"},
    )

    result = UpdateClient(config, platform="win32", architecture="AMD64").check()

    assert result.status is UpdateStatus.AVAILABLE
    assert result.candidate is not None
    assert result.candidate.version == "0.1.1"
    assert result.artifact is not None
    assert result.artifact.filename == "Persona-Setup-0.1.1.exe"
    assert result.artifact.sha256 == "abc123"
    assert session.calls[0][0].endswith("/repos/example/persona/releases?per_page=100")


def test_github_stable_channel_excludes_prereleases():
    session = FakeSession(
        [
            {
                "tag_name": "v1.1.0-beta.1",
                "prerelease": True,
                "assets": [],
            }
        ]
    )
    config = UpdateConfig(
        app_id="example",
        current_version="1.0.0",
        source=GitHubReleaseSource("example/project", session=session),
    )

    result = UpdateClient(config, platform="win32", architecture="AMD64").check()

    assert result.status is UpdateStatus.CURRENT
    assert result.candidate is None


def test_semver_stable_channel_excludes_unmarked_prerelease_tags():
    session = FakeSession(
        {
            "schema": 1,
            "app_id": "example",
            "version": "2.0.0-beta.1",
            "artifacts": [],
        }
    )
    config = UpdateConfig(
        app_id="example",
        current_version="1.0.0",
        source=JsonFeedSource("https://downloads.example.test/stable.json", session=session),
    )

    result = UpdateClient(config).check()

    assert result.status is UpdateStatus.CURRENT


def test_gitlab_source_reads_encoded_project_and_release_links():
    session = FakeSession(
        [
            {
                "tag_name": "v1.2.0",
                "name": "Persona 1.2.0",
                "description": "GitLab release",
                "web_url": "https://gitlab.example.test/group/persona/-/releases/v1.2.0",
                "released_at": "2026-08-11T00:00:00Z",
                "assets": {
                    "links": [
                        {
                            "name": "Persona-Setup-1.2.0.exe",
                            "url": "https://gitlab.example.test/downloads/1.2.0.exe",
                            "direct_asset_url": "https://gitlab.example.test/-/project/1/jobs/artifacts/main/raw/Persona-Setup-1.2.0.exe",
                            "link_type": "package",
                        }
                    ]
                },
            }
        ]
    )
    source = GitLabReleaseSource(
        "group/persona",
        api_url="https://gitlab.example.test/api/v4",
        session=session,
    )
    config = UpdateConfig(
        app_id="persona",
        current_version="1.0.0",
        source=source,
        asset_patterns={"windows_x86_64": "Persona-Setup-{version}.exe"},
    )

    result = UpdateClient(config, platform="win32", architecture="x86_64").check()

    assert result.status is UpdateStatus.AVAILABLE
    assert result.artifact is not None
    assert result.artifact.url.endswith("Persona-Setup-1.2.0.exe")
    assert session.calls[0][0] == (
        "https://gitlab.example.test/api/v4/projects/group%2Fpersona/releases?per_page=100"
    )
    mapped = UpdateConfig.from_mapping(
        {
            "source": {
                "type": "gitlab",
                "project": "group/persona",
                "api_url": "https://gitlab.example.test/api/v4",
            }
        },
        app_id="persona",
        current_version="1.0.0",
        session=session,
    )
    assert isinstance(mapped.source, GitLabReleaseSource)


def test_custom_version_policy_can_be_supplied_alongside_toml():
    class BuildPolicy:
        def normalize(self, value: str) -> str:
            return str(value).removeprefix("build-")

        def compare(self, left: str, right: str) -> int:
            return (int(left) > int(right)) - (int(left) < int(right))

    session = FakeSession(
        {
            "schema": 1,
            "app_id": "example",
            "version": "build-11",
            "artifacts": [
                {
                    "filename": "Example-11.exe",
                    "url": "https://downloads.example.test/Example-11.exe",
                }
            ],
        }
    )
    config = UpdateConfig.from_mapping(
        {
            "versioning": "build-number",
            "source": {"type": "json", "url": "https://downloads.example.test/feed.json"},
        },
        app_id="example",
        current_version="build-10",
        session=session,
        version_policy=BuildPolicy(),
    )

    result = UpdateClient(config, platform="win32", architecture="x86_64").check()

    assert result.status is UpdateStatus.AVAILABLE
    assert result.current_version == "10"
    assert result.candidate is not None
    assert result.candidate.version == "11"


def test_downloader_streams_verifies_and_atomically_stages(tmp_path: Path):
    payload = b"Persona installer payload"
    response = FakeDownloadResponse(payload)
    session = FakeDownloadSession(response)
    artifact = UpdateArtifact(
        filename="Persona-Setup-1.2.0.exe",
        url=response.url,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    staged = UpdateDownloader(session=session, chunk_size=4).stage(artifact, tmp_path)

    assert staged.path == tmp_path / artifact.filename
    assert staged.path.read_bytes() == payload
    assert staged.bytes == len(payload)
    assert staged.sha256 == artifact.sha256
    assert response.closed is True
    assert list(tmp_path.glob("*.part")) == []
    assert session.calls[0][3] is True


def test_downloader_rejects_checksum_mismatch_and_removes_partial_file(tmp_path: Path):
    payload = b"tampered installer"
    response = FakeDownloadResponse(payload)
    artifact = UpdateArtifact(
        filename="Persona-Setup.exe",
        url=response.url,
        sha256="0" * 64,
    )

    with pytest.raises(UpdateVerificationError, match="checksum"):
        UpdateDownloader(session=FakeDownloadSession(response)).stage(artifact, tmp_path)

    assert not (tmp_path / artifact.filename).exists()
    assert list(tmp_path.glob("*.part")) == []


def test_downloader_requires_https_and_checksum_by_default(tmp_path: Path):
    with pytest.raises(UpdateVerificationError, match="no SHA-256"):
        UpdateDownloader().stage(
            UpdateArtifact("Example.exe", "https://downloads.example.test/Example.exe"),
            tmp_path,
        )

    with pytest.raises(UpdateDownloadError, match=r"HTTP\(S\)"):
        UpdateDownloader().stage(
            UpdateArtifact("Example.exe", "http://downloads.example.test/Example.exe", sha256="0" * 64),
            tmp_path,
        )


def test_subprocess_handoff_uses_argument_vector_without_shell(tmp_path: Path, monkeypatch):
    installer = tmp_path / "Persona-Setup.exe"
    installer.write_bytes(b"verified")
    artifact = UpdateArtifact(installer.name, "https://downloads.example.test/Persona-Setup.exe")
    staged = StagedUpdate(
        artifact,
        installer,
        installer.stat().st_size,
        hashlib.sha256(b"verified").hexdigest(),
    )
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return "process"

    monkeypatch.setattr("spiritus.updates.subprocess.Popen", fake_popen)

    process = SubprocessInstallerHandoff().launch(staged, args=("/S",))

    assert process == "process"
    assert captured["command"] == [str(installer.resolve()), "/S"]
    assert captured["kwargs"]["shell"] is False


def test_json_feed_selects_platform_metadata_without_filename_rules():
    session = FakeSession(
        {
            "schema": 1,
            "app_id": "private-app",
            "channel": "stable",
            "version": "1.4.0",
            "release_notes_url": "https://downloads.example.test/1.4.0",
            "artifacts": [
                {
                    "platform": "windows",
                    "architecture": "x86_64",
                    "kind": "installer",
                    "filename": "PrivateApp-Setup-1.4.0.exe",
                    "url": "https://downloads.example.test/PrivateApp-Setup-1.4.0.exe",
                    "sha256": "deadbeef",
                }
            ],
        }
    )
    source = JsonFeedSource("https://downloads.example.test/stable.json", session=session)
    config = UpdateConfig(
        app_id="private-app",
        current_version="1.3.0",
        source=source,
    )

    result = UpdateClient(config, platform="win32", architecture="x86_64").check()

    assert result.status is UpdateStatus.AVAILABLE
    assert result.artifact is not None
    assert result.artifact.kind == "installer"
    assert result.artifact.sha256 == "deadbeef"


def test_json_feed_rejects_a_different_app_id():
    session = FakeSession(
        {
            "schema": 1,
            "app_id": "other-app",
            "version": "2.0.0",
            "artifacts": [],
        }
    )
    config = UpdateConfig(
        app_id="example",
        current_version="1.0.0",
        source=JsonFeedSource("https://downloads.example.test/stable.json", session=session),
    )

    result = UpdateClient(config).check()

    assert result.status is UpdateStatus.CURRENT


def test_json_feed_requires_schema_one():
    session = FakeSession(
        {
            "schema": 2,
            "app_id": "example",
            "version": "2.0.0",
            "artifacts": [],
        }
    )
    config = UpdateConfig(
        app_id="example",
        current_version="1.0.0",
        source=JsonFeedSource("https://downloads.example.test/stable.json", session=session),
    )

    result = UpdateClient(config).check()

    assert result.status is UpdateStatus.ERROR
    assert result.error == "invalid Spiritus update feed"


def test_update_reports_when_release_has_no_matching_asset():
    session = FakeSession(
        {
            "schema": 1,
            "app_id": "example",
            "version": "2.0.0",
            "artifacts": [
                {
                    "platform": "macos",
                    "architecture": "arm64",
                    "filename": "Example-2.0.0.dmg",
                    "url": "https://downloads.example.test/Example-2.0.0.dmg",
                }
            ],
        }
    )
    config = UpdateConfig(
        app_id="example",
        current_version="1.0.0",
        source=JsonFeedSource("https://downloads.example.test/stable.json", session=session),
    )

    result = UpdateClient(config, platform="win32", architecture="AMD64").check()

    assert result.status is UpdateStatus.NO_COMPATIBLE_ASSET
    assert result.candidate is not None
    assert result.artifact is None


def test_update_config_can_be_created_from_toml_mapping():
    session = FakeSession({"app_id": "example", "version": "1.0.0", "artifacts": []})
    config = UpdateConfig.from_mapping(
        {
            "enabled": True,
            "channel": "stable",
            "versioning": "semver",
            "source": {
                "type": "json",
                "url": "https://downloads.example.test/stable.json",
            },
            "assets": {"windows_x86_64": "Example-{version}.exe"},
        },
        app_id="example",
        current_version="1.0.0",
        session=session,
    )

    assert config.channel == "stable"
    assert config.asset_patterns["windows_x86_64"] == "Example-{version}.exe"
    assert isinstance(config.source, JsonFeedSource)
