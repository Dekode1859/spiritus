"""Check-only application update discovery.

Spiritus owns the provider-neutral part of update discovery: release metadata,
version comparison, channel filtering, and platform asset selection. Download
and installation are intentionally separate concerns because applications own
their installers and platform update policy.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import platform as host_platform
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlparse
from uuid import uuid4

import requests


class UpdateError(RuntimeError):
    """Base class for update configuration and source failures."""


class UpdateConfigurationError(UpdateError, ValueError):
    """Raised when an update configuration is invalid."""


class UpdateSourceError(UpdateError):
    """Raised when a release source cannot be read or decoded."""


class UpdateDownloadError(UpdateError):
    """Raised when an update artifact cannot be downloaded or staged."""


class UpdateVerificationError(UpdateDownloadError):
    """Raised when an artifact fails its size or checksum verification."""


class UpdateInstallerError(UpdateError):
    """Raised when a staged installer cannot be handed off to the OS."""


class UpdateStatus(StrEnum):
    """The outcome of an update check."""

    DISABLED = "disabled"
    CURRENT = "current"
    AVAILABLE = "available"
    NO_COMPATIBLE_ASSET = "no_compatible_asset"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SemanticVersion:
    """A SemVer 2.0.0 value with an optional leading ``v`` accepted."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value


class VersionPolicy(Protocol):
    """Protocol implemented by version comparison strategies."""

    def normalize(self, value: str) -> str:
        """Return the canonical display/comparison form of ``value``."""

    def compare(self, left: str, right: str) -> int:
        """Return -1, 0, or 1 according to release precedence."""


_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _identifiers(value: str | None, *, prerelease: bool) -> tuple[str, ...]:
    if not value:
        return ()
    values = tuple(value.split("."))
    if prerelease:
        for item in values:
            if item.isdigit() and len(item) > 1 and item.startswith("0"):
                raise ValueError(f"invalid SemVer numeric prerelease identifier: {item!r}")
    return values


def parse_semver(value: str) -> SemanticVersion:
    """Parse a SemVer value, accepting a conventional leading ``v`` tag."""
    raw = str(value).strip()
    if raw.startswith(("v", "V")):
        raw = raw[1:]
    match = _SEMVER.fullmatch(raw)
    if not match:
        raise ValueError(f"invalid SemVer value: {value!r}")
    return SemanticVersion(
        major=int(match["major"]),
        minor=int(match["minor"]),
        patch=int(match["patch"]),
        prerelease=_identifiers(match["prerelease"], prerelease=True),
        build=_identifiers(match["build"], prerelease=False),
    )


def _compare_identifiers(left: Sequence[str], right: Sequence[str]) -> int:
    for left_item, right_item in zip(left, right, strict=False):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_item) > int(right_item) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_item > right_item else -1
    if len(left) == len(right):
        return 0
    return 1 if len(left) > len(right) else -1


class SemVerPolicy:
    """Default Semantic Versioning comparison policy."""

    def normalize(self, value: str) -> str:
        return str(parse_semver(value))

    def compare(self, left: str, right: str) -> int:
        left_version = parse_semver(left)
        right_version = parse_semver(right)
        for left_part, right_part in zip(
            (left_version.major, left_version.minor, left_version.patch),
            (right_version.major, right_version.minor, right_version.patch),
            strict=True,
        ):
            if left_part != right_part:
                return 1 if left_part > right_part else -1
        if not left_version.prerelease and not right_version.prerelease:
            return 0
        if not left_version.prerelease:
            return 1
        if not right_version.prerelease:
            return -1
        return _compare_identifiers(left_version.prerelease, right_version.prerelease)


@dataclass(frozen=True, slots=True)
class UpdateArtifact:
    """A downloadable application artifact for one release."""

    filename: str
    url: str
    platform: str | None = None
    architecture: str | None = None
    kind: str | None = None
    sha256: str | None = None
    signature_url: str | None = None
    size: int | None = None


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    """Normalized release metadata returned by a provider."""

    version: str
    artifacts: tuple[UpdateArtifact, ...] = ()
    app_id: str | None = None
    channel: str | None = None
    prerelease: bool = False
    release_notes: str | None = None
    release_notes_url: str | None = None
    published_at: str | None = None
    source_url: str | None = None


class ReleaseSource(Protocol):
    """Provider interface used by :class:`UpdateClient`."""

    def releases(self, *, channel: str) -> tuple[ReleaseCandidate, ...]:
        """Return release candidates that may belong to ``channel``."""


class _HttpSource:
    def __init__(
        self,
        *,
        session: Any = None,
        timeout: float = 10.0,
        headers: Mapping[str, str] | None = None,
        token_env: str | None = None,
        token_header: str = "Authorization",
    ) -> None:
        if timeout <= 0:
            raise UpdateConfigurationError("update source timeout must be positive")
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_timeout", timeout)
        object.__setattr__(self, "_headers", dict(headers or {}))
        object.__setattr__(self, "_token_env", token_env)
        object.__setattr__(self, "_token_header", token_header)

    def _request_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", **self._headers}
        if self._token_env:
            token = os.environ.get(self._token_env)
            if not token:
                raise UpdateSourceError(
                    f"update source credential environment variable is not set: {self._token_env}"
                )
            if self._token_header.lower() == "authorization":
                headers[self._token_header] = f"Bearer {token}"
            else:
                headers[self._token_header] = token
        return headers

    def _get_json(self, url: str) -> object:
        if not url.startswith(("https://", "http://")):
            raise UpdateSourceError(f"update source URL must use HTTP(S): {url!r}")
        client = self._session or requests
        try:
            response = client.get(url, headers=self._request_headers(), timeout=self._timeout)
            response.raise_for_status()
            return response.json()
        except UpdateSourceError:
            raise
        except (requests.RequestException, ValueError) as exc:
            raise UpdateSourceError(f"could not read update source: {url}") from exc


@dataclass(frozen=True, slots=True)
class GitHubReleaseSource(_HttpSource):
    """Read release metadata from a public or authenticated GitHub repository."""

    repository: str
    api_url: str = "https://api.github.com"
    session: Any = field(default=None, repr=False, compare=False)
    timeout: float = 10.0
    headers: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    token_env: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.repository.count("/") != 1 or any(not part for part in self.repository.split("/")):
            raise UpdateConfigurationError("GitHub repository must use OWNER/REPOSITORY")
        _HttpSource.__init__(
            self,
            session=self.session,
            timeout=self.timeout,
            headers=self.headers,
            token_env=self.token_env,
            token_header="Authorization",
        )

    def releases(self, *, channel: str) -> tuple[ReleaseCandidate, ...]:
        owner, repository = self.repository.split("/", 1)
        path = f"/repos/{quote(owner)}/{quote(repository)}/releases"
        payload = self._get_json(self.api_url.rstrip("/") + path + "?per_page=100")
        if not isinstance(payload, list):
            raise UpdateSourceError("GitHub releases response must be an array")
        return tuple(_github_release(item) for item in payload if isinstance(item, dict))


@dataclass(frozen=True, slots=True)
class JsonFeedSource(_HttpSource):
    """Read Spiritus's normalized release feed from any HTTP(S) host."""

    url: str
    session: Any = field(default=None, repr=False, compare=False)
    timeout: float = 10.0
    headers: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    token_env: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _HttpSource.__init__(
            self,
            session=self.session,
            timeout=self.timeout,
            headers=self.headers,
            token_env=self.token_env,
            token_header="Authorization",
        )

    def releases(self, *, channel: str) -> tuple[ReleaseCandidate, ...]:
        payload = self._get_json(self.url)
        if isinstance(payload, dict) and isinstance(payload.get("releases"), list):
            values = payload["releases"]
        else:
            values = [payload]
        try:
            return tuple(_feed_release(item) for item in values if isinstance(item, dict))
        except (KeyError, TypeError, ValueError) as exc:
            raise UpdateSourceError("invalid Spiritus update feed") from exc


@dataclass(frozen=True, slots=True)
class GitLabReleaseSource(_HttpSource):
    """Read release metadata from a public or authenticated GitLab project."""

    project: str
    api_url: str = "https://gitlab.com/api/v4"
    session: Any = field(default=None, repr=False, compare=False)
    timeout: float = 10.0
    headers: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    token_env: str | None = field(default=None, repr=False, compare=False)
    token_header: str = field(default="PRIVATE-TOKEN", repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.project.strip():
            raise UpdateConfigurationError("GitLab project must not be empty")
        _HttpSource.__init__(
            self,
            session=self.session,
            timeout=self.timeout,
            headers=self.headers,
            token_env=self.token_env,
            token_header=self.token_header,
        )

    def releases(self, *, channel: str) -> tuple[ReleaseCandidate, ...]:
        project = quote(self.project.strip(), safe="")
        path = f"/projects/{project}/releases"
        payload = self._get_json(self.api_url.rstrip("/") + path + "?per_page=100")
        if not isinstance(payload, list):
            raise UpdateSourceError("GitLab releases response must be an array")
        return tuple(_gitlab_release(item) for item in payload if isinstance(item, dict))


def _github_release(payload: Mapping[str, object]) -> ReleaseCandidate:
    version = str(payload.get("tag_name") or "").strip()
    if not version:
        raise UpdateSourceError("GitHub release is missing tag_name")
    artifacts = []
    raw_assets = payload.get("assets", [])
    if not isinstance(raw_assets, list):
        raise UpdateSourceError(f"GitHub release {version!r} has invalid assets")
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            continue
        digest = raw_asset.get("digest")
        digest_text = str(digest) if digest else None
        if digest_text and digest_text.startswith("sha256:"):
            digest_text = digest_text.removeprefix("sha256:")
        artifacts.append(
            UpdateArtifact(
                filename=str(raw_asset.get("name") or ""),
                url=str(raw_asset.get("browser_download_url") or ""),
                sha256=digest_text,
                size=int(raw_asset["size"]) if raw_asset.get("size") is not None else None,
            )
        )
    return ReleaseCandidate(
        version=version,
        artifacts=tuple(item for item in artifacts if item.filename and item.url),
        channel="beta" if bool(payload.get("prerelease")) else "stable",
        prerelease=bool(payload.get("prerelease")),
        release_notes=str(payload["body"]) if payload.get("body") is not None else None,
        release_notes_url=str(payload["html_url"]) if payload.get("html_url") else None,
        published_at=str(payload["published_at"]) if payload.get("published_at") else None,
        source_url=str(payload["html_url"]) if payload.get("html_url") else None,
    )


def _gitlab_release(payload: Mapping[str, object]) -> ReleaseCandidate:
    version = str(payload.get("tag_name") or "").strip()
    if not version:
        raise UpdateSourceError("GitLab release is missing tag_name")
    artifacts = []
    raw_assets = payload.get("assets", {})
    if not isinstance(raw_assets, dict):
        raise UpdateSourceError(f"GitLab release {version!r} has invalid assets")
    raw_links = raw_assets.get("links", [])
    if not isinstance(raw_links, list):
        raise UpdateSourceError(f"GitLab release {version!r} has invalid asset links")
    for raw_link in raw_links:
        if not isinstance(raw_link, dict):
            continue
        url = raw_link.get("direct_asset_url") or raw_link.get("url")
        artifacts.append(
            UpdateArtifact(
                filename=str(raw_link.get("name") or ""),
                url=str(url or ""),
                kind=str(raw_link["link_type"]) if raw_link.get("link_type") else None,
            )
        )
    release_url = payload.get("_links", {})
    if isinstance(release_url, dict):
        release_url = release_url.get("self")
    return ReleaseCandidate(
        version=version,
        channel=str(payload["channel"]) if payload.get("channel") else None,
        prerelease=bool(payload.get("upcoming_release")),
        release_notes=str(payload["description"]) if payload.get("description") else None,
        release_notes_url=str(payload["web_url"]) if payload.get("web_url") else None,
        published_at=str(payload["released_at"]) if payload.get("released_at") else None,
        artifacts=tuple(item for item in artifacts if item.filename and item.url),
        source_url=str(release_url) if release_url else None,
    )


def _feed_release(payload: Mapping[str, object]) -> ReleaseCandidate:
    if payload.get("schema") != 1:
        raise ValueError("unsupported update feed schema")
    if not str(payload.get("app_id") or "").strip():
        raise ValueError("app_id is required")
    if not str(payload.get("version") or "").strip():
        raise ValueError("version is required")
    artifacts = []
    raw_artifacts = payload.get("artifacts", [])
    if not isinstance(raw_artifacts, list):
        raise ValueError("artifacts must be an array")
    for raw_artifact in raw_artifacts:
        if not isinstance(raw_artifact, dict):
            raise ValueError("artifact entries must be objects")
        artifacts.append(
            UpdateArtifact(
                filename=str(raw_artifact["filename"]),
                url=str(raw_artifact["url"]),
                platform=str(raw_artifact["platform"]) if raw_artifact.get("platform") else None,
                architecture=(
                    str(raw_artifact["architecture"])
                    if raw_artifact.get("architecture")
                    else None
                ),
                kind=str(raw_artifact["kind"]) if raw_artifact.get("kind") else None,
                sha256=str(raw_artifact["sha256"]) if raw_artifact.get("sha256") else None,
                signature_url=(
                    str(raw_artifact["signature_url"])
                    if raw_artifact.get("signature_url")
                    else None
                ),
                size=int(raw_artifact["size"]) if raw_artifact.get("size") is not None else None,
            )
        )
    return ReleaseCandidate(
        version=str(payload["version"]),
        app_id=str(payload["app_id"]) if payload.get("app_id") else None,
        channel=str(payload["channel"]) if payload.get("channel") else None,
        prerelease=bool(payload.get("prerelease")),
        release_notes=str(payload["release_notes"]) if payload.get("release_notes") else None,
        release_notes_url=(
            str(payload["release_notes_url"]) if payload.get("release_notes_url") else None
        ),
        published_at=str(payload["published_at"]) if payload.get("published_at") else None,
        artifacts=tuple(artifacts),
        source_url=str(payload["source_url"]) if payload.get("source_url") else None,
    )


def _host_platform(value: str | None = None) -> str:
    value = value or sys.platform
    if value == "win32":
        return "windows"
    if value == "darwin":
        return "macos"
    if value.startswith("linux"):
        return "linux"
    return value


def _host_architecture(value: str | None = None) -> str:
    value = (value or host_platform.machine()).lower()
    if value in {"amd64", "x86_64", "x64"}:
        return "x86_64"
    if value in {"aarch64", "arm64"}:
        return "arm64"
    if value in {"x86", "i386", "i686"}:
        return "x86"
    return value


def _channel_matches(candidate: ReleaseCandidate, channel: str) -> bool:
    if channel == "stable":
        return not candidate.prerelease and candidate.channel in (None, "", "stable")
    if channel == "beta":
        return candidate.channel in (None, "", "stable", "beta")
    return candidate.channel in (None, "", channel)


def _asset_pattern(patterns: Mapping[str, str], platform: str, architecture: str) -> str | None:
    return (
        patterns.get(f"{platform}_{architecture}")
        or patterns.get(f"{platform}_*")
        or patterns.get(platform)
        or patterns.get("default")
    )


def _select_asset(
    candidate: ReleaseCandidate,
    *,
    platform: str,
    architecture: str,
    patterns: Mapping[str, str],
) -> UpdateArtifact | None:
    available = [
        artifact
        for artifact in candidate.artifacts
        if artifact.platform in (None, platform)
        and artifact.architecture in (None, architecture)
    ]
    if not available:
        return None
    pattern = _asset_pattern(patterns, platform, architecture)
    if pattern:
        rendered = pattern.format(version=candidate.version, platform=platform, architecture=architecture)
        available = [
            artifact
            for artifact in available
            if fnmatch.fnmatchcase(artifact.filename, rendered)
        ]
    if len(available) != 1:
        return None
    return available[0]


@dataclass(frozen=True, slots=True)
class UpdateConfig:
    """Declarative update discovery settings for one application."""

    app_id: str
    current_version: str
    source: ReleaseSource
    channel: str = "stable"
    version_policy: VersionPolicy = field(default_factory=SemVerPolicy)
    asset_patterns: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.app_id.strip():
            raise UpdateConfigurationError("update app_id must not be empty")
        if not self.current_version.strip():
            raise UpdateConfigurationError("update current_version must not be empty")
        self.version_policy.normalize(self.current_version)
        if not self.channel.strip():
            raise UpdateConfigurationError("update channel must not be empty")
        object.__setattr__(self, "asset_patterns", dict(self.asset_patterns))

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        app_id: str,
        current_version: str,
        session: Any = None,
        version_policy: VersionPolicy | None = None,
    ) -> UpdateConfig:
        """Build a config from the ``[updates]`` TOML table."""
        if not isinstance(payload, Mapping):
            raise UpdateConfigurationError("updates must be a TOML table")
        source_payload = payload.get("source")
        if not isinstance(source_payload, Mapping):
            raise UpdateConfigurationError("updates.source must be a TOML table")
        source = source_from_mapping(source_payload, session=session)
        patterns = payload.get("assets", {})
        if not isinstance(patterns, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in patterns.items()
        ):
            raise UpdateConfigurationError("updates.assets must be a table of string patterns")
        versioning = str(payload.get("versioning", "semver"))
        if version_policy is None and versioning != "semver":
            raise UpdateConfigurationError(
                f"unsupported built-in update versioning policy: {versioning!r}; "
                "provide a VersionPolicy in Python"
            )
        return cls(
            app_id=app_id,
            current_version=current_version,
            source=source,
            channel=str(payload.get("channel", "stable")),
            version_policy=version_policy or SemVerPolicy(),
            asset_patterns=patterns,
            enabled=bool(payload.get("enabled", True)),
        )


def source_from_mapping(payload: Mapping[str, object], *, session: Any = None) -> ReleaseSource:
    """Create a built-in source from an update-source TOML table."""
    source_type = str(payload.get("type", "")).lower()
    token_env = str(payload["token_env"]) if payload.get("token_env") else None
    timeout = float(payload.get("timeout", 10.0))
    headers = payload.get("headers", {})
    if not isinstance(headers, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise UpdateConfigurationError("updates.source.headers must be a table of strings")
    if source_type == "github":
        repository = str(payload.get("repository", ""))
        return GitHubReleaseSource(
            repository=repository,
            api_url=str(payload.get("api_url", "https://api.github.com")),
            session=session,
            timeout=timeout,
            headers=headers,
            token_env=token_env,
        )
    if source_type == "json":
        url = str(payload.get("url", ""))
        if not url:
            raise UpdateConfigurationError("JSON update source requires url")
        return JsonFeedSource(
            url=url,
            session=session,
            timeout=timeout,
            headers=headers,
            token_env=token_env,
        )
    if source_type == "gitlab":
        project = str(payload.get("project") or payload.get("repository") or "")
        return GitLabReleaseSource(
            project=project,
            api_url=str(payload.get("api_url", "https://gitlab.com/api/v4")),
            session=session,
            timeout=timeout,
            headers=headers,
            token_env=token_env,
            token_header=str(payload.get("token_header", "PRIVATE-TOKEN")),
        )
    raise UpdateConfigurationError(
        "updates.source.type must be 'github', 'gitlab', or 'json'"
    )


@dataclass(frozen=True, slots=True)
class UpdateCheck:
    """Structured, side-effect-free result of :meth:`UpdateClient.check`."""

    status: UpdateStatus
    current_version: str
    candidate: ReleaseCandidate | None = None
    artifact: UpdateArtifact | None = None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.status is UpdateStatus.AVAILABLE


@dataclass(frozen=True, slots=True)
class StagedUpdate:
    """An update artifact downloaded and verified in an app-owned directory."""

    artifact: UpdateArtifact
    path: Path
    bytes: int
    sha256: str


class UpdateDownloader:
    """Stream, verify, and atomically stage an update artifact.

    Staging never executes or replaces the application. Callers choose the
    writable directory and decide when, or whether, to hand the file to an
    installer.
    """

    def __init__(
        self,
        *,
        session: Any = None,
        timeout: float = 120.0,
        chunk_size: int = 1 << 16,
        max_bytes: int = 2 * 1024 * 1024 * 1024,
        allow_insecure_http: bool = False,
    ) -> None:
        if timeout <= 0:
            raise UpdateConfigurationError("update download timeout must be positive")
        if chunk_size <= 0:
            raise UpdateConfigurationError("update download chunk size must be positive")
        if max_bytes <= 0:
            raise UpdateConfigurationError("update download max_bytes must be positive")
        self._session = session
        self._timeout = timeout
        self._chunk_size = chunk_size
        self._max_bytes = max_bytes
        self._allow_insecure_http = allow_insecure_http

    def stage(
        self,
        artifact: UpdateArtifact,
        destination: Path,
        *,
        require_sha256: bool = True,
    ) -> StagedUpdate:
        """Download ``artifact`` into ``destination`` and verify it atomically."""
        filename = self._safe_filename(artifact.filename)
        expected_sha256 = self._expected_sha256(artifact.sha256, require_sha256=require_sha256)
        self._validate_url(artifact.url)
        if artifact.size is not None and artifact.size < 0:
            raise UpdateVerificationError("update artifact size must not be negative")
        if artifact.size is not None and artifact.size > self._max_bytes:
            raise UpdateDownloadError("update artifact exceeds configured maximum size")

        destination = Path(destination).resolve()
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UpdateDownloadError(f"could not create update staging directory: {destination}") from exc
        final_path = destination / filename
        temporary_path = destination / f".{filename}.{uuid4().hex}.part"
        downloaded = 0
        digest = hashlib.sha256()
        response = None
        try:
            client = self._session or requests
            response = client.get(
                artifact.url,
                headers={"Accept": "application/octet-stream"},
                timeout=self._timeout,
                stream=True,
            )
            response.raise_for_status()
            self._validate_response_url(response)
            content_length = self._content_length(response)
            if content_length is not None and content_length > self._max_bytes:
                raise UpdateDownloadError("update response exceeds configured maximum size")
            if artifact.size is not None and content_length is not None and content_length != artifact.size:
                raise UpdateVerificationError("update response size does not match release metadata")
            with temporary_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=self._chunk_size):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > self._max_bytes:
                        raise UpdateDownloadError("update download exceeds configured maximum size")
                    digest.update(chunk)
                    handle.write(chunk)
            if artifact.size is not None and downloaded != artifact.size:
                raise UpdateVerificationError("downloaded update size does not match release metadata")
            actual_sha256 = digest.hexdigest()
            if expected_sha256 and actual_sha256 != expected_sha256:
                raise UpdateVerificationError("downloaded update checksum does not match release metadata")
            os.replace(temporary_path, final_path)
            return StagedUpdate(
                artifact=artifact,
                path=final_path,
                bytes=downloaded,
                sha256=actual_sha256,
            )
        except UpdateError:
            raise
        except (OSError, requests.RequestException, ValueError) as exc:
            raise UpdateDownloadError(f"could not stage update artifact: {artifact.filename}") from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _safe_filename(filename: str) -> str:
        value = str(filename).strip()
        if not value or value in {".", ".."} or "\x00" in value or Path(value).name != value:
            raise UpdateDownloadError(f"unsafe update artifact filename: {filename!r}")
        return value

    @staticmethod
    def _expected_sha256(value: str | None, *, require_sha256: bool) -> str | None:
        if not value:
            if require_sha256:
                raise UpdateVerificationError("update artifact has no SHA-256 checksum")
            return None
        normalized = str(value).strip().lower().removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise UpdateVerificationError("update artifact SHA-256 checksum is invalid")
        return normalized

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(str(url))
        allowed = {"https"}
        if self._allow_insecure_http:
            allowed.add("http")
        if parsed.scheme.lower() not in allowed or not parsed.netloc:
            raise UpdateDownloadError(f"update artifact URL must use HTTP(S): {url!r}")

    def _validate_response_url(self, response: Any) -> None:
        final_url = getattr(response, "url", None)
        if final_url:
            self._validate_url(str(final_url))

    @staticmethod
    def _content_length(response: Any) -> int | None:
        headers = getattr(response, "headers", {}) or {}
        raw_value = next(
            (value for key, value in headers.items() if str(key).lower() == "content-length"),
            None,
        )
        if raw_value is None:
            return None
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise UpdateDownloadError("update response has an invalid content length") from exc
        if value < 0:
            raise UpdateDownloadError("update response has a negative content length")
        return value


class InstallerHandoff(Protocol):
    """Application-selected handoff for a verified staged installer."""

    def launch(
        self,
        staged: StagedUpdate,
        *,
        args: Sequence[str] = (),
        cwd: Path | None = None,
    ) -> Any:
        """Launch the staged installer and return its process handle."""


class SubprocessInstallerHandoff:
    """Launch a staged installer without a shell or interpolated command line."""

    def __init__(self, *, creationflags: int = 0) -> None:
        self._creationflags = creationflags

    def launch(
        self,
        staged: StagedUpdate,
        *,
        args: Sequence[str] = (),
        cwd: Path | None = None,
    ) -> Any:
        path = Path(staged.path).resolve()
        if not path.is_file():
            raise UpdateInstallerError(f"staged installer does not exist: {path}")
        if staged.bytes != path.stat().st_size:
            raise UpdateVerificationError("staged installer size changed before launch")
        if staged.sha256:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest() != staged.sha256.lower().removeprefix("sha256:"):
                raise UpdateVerificationError("staged installer checksum changed before launch")
        command = [str(path), *(str(value) for value in args)]
        kwargs: dict[str, Any] = {"cwd": str(cwd) if cwd else None, "shell": False}
        if self._creationflags:
            kwargs["creationflags"] = self._creationflags
        try:
            return subprocess.Popen(command, **kwargs)
        except OSError as exc:
            raise UpdateInstallerError(f"could not launch staged installer: {path}") from exc


@dataclass(frozen=True, slots=True)
class UpdateClient:
    """Check for updates without downloading or modifying the application."""

    config: UpdateConfig
    platform: str | None = None
    architecture: str | None = None

    def check(self) -> UpdateCheck:
        current_version = self.config.version_policy.normalize(self.config.current_version)
        if not self.config.enabled:
            return UpdateCheck(UpdateStatus.DISABLED, current_version)
        try:
            releases = self.config.source.releases(channel=self.config.channel)
            candidates = []
            for release in releases:
                try:
                    normalized = self.config.version_policy.normalize(release.version)
                except (TypeError, ValueError):
                    continue
                semver_prerelease = False
                try:
                    semver_prerelease = bool(parse_semver(normalized).prerelease)
                except ValueError:
                    pass
                normalized_release = replace(
                    release,
                    version=normalized,
                    prerelease=release.prerelease or semver_prerelease,
                )
                if normalized_release.app_id and normalized_release.app_id != self.config.app_id:
                    continue
                if not _channel_matches(normalized_release, self.config.channel):
                    continue
                candidates.append(normalized_release)
            candidate = self._newest(candidates)
            if candidate is None:
                return UpdateCheck(UpdateStatus.CURRENT, current_version)
            if self.config.version_policy.compare(candidate.version, current_version) <= 0:
                return UpdateCheck(UpdateStatus.CURRENT, current_version, candidate=candidate)
            selected = _select_asset(
                candidate,
                platform=_host_platform(self.platform),
                architecture=_host_architecture(self.architecture),
                patterns=self.config.asset_patterns,
            )
            if selected is None:
                return UpdateCheck(
                    UpdateStatus.NO_COMPATIBLE_ASSET,
                    current_version,
                    candidate=candidate,
                )
            return UpdateCheck(
                UpdateStatus.AVAILABLE,
                current_version,
                candidate=candidate,
                artifact=selected,
            )
        except UpdateError as exc:
            return UpdateCheck(UpdateStatus.ERROR, current_version, error=str(exc))

    def stage_update(
        self,
        check: UpdateCheck,
        destination: Path,
        *,
        downloader: UpdateDownloader | None = None,
        require_sha256: bool = True,
    ) -> StagedUpdate:
        """Stage an available result; this never launches or installs it."""
        if not check.available or check.artifact is None:
            raise UpdateDownloadError("update check does not contain an available artifact")
        worker = downloader or UpdateDownloader()
        return worker.stage(check.artifact, destination, require_sha256=require_sha256)

    def _newest(self, releases: Sequence[ReleaseCandidate]) -> ReleaseCandidate | None:
        newest = None
        for release in releases:
            if newest is None or self.config.version_policy.compare(release.version, newest.version) > 0:
                newest = release
        return newest


__all__ = [
    "GitLabReleaseSource",
    "GitHubReleaseSource",
    "InstallerHandoff",
    "JsonFeedSource",
    "ReleaseCandidate",
    "ReleaseSource",
    "SemanticVersion",
    "SemVerPolicy",
    "UpdateArtifact",
    "UpdateCheck",
    "UpdateClient",
    "UpdateConfig",
    "UpdateDownloader",
    "UpdateConfigurationError",
    "UpdateDownloadError",
    "UpdateError",
    "UpdateInstallerError",
    "UpdateSourceError",
    "UpdateStatus",
    "UpdateVerificationError",
    "VersionPolicy",
    "StagedUpdate",
    "SubprocessInstallerHandoff",
    "parse_semver",
    "source_from_mapping",
]
