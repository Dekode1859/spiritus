"""Offline tests for Spiritus's check-only update discovery."""
from __future__ import annotations

import pytest

from spiritus import (
    GitHubReleaseSource,
    JsonFeedSource,
    SemVerPolicy,
    UpdateClient,
    UpdateConfig,
    UpdateStatus,
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
