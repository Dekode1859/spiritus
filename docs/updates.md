# Application updates

Spiritus provides opt-in update discovery, verified staging, and explicit
installer handoff through `spiritus.updates`. It never replaces or restarts an
application automatically. The application decides when to check, where to
stage, and whether to launch its installer.

## GitHub Releases

```toml
[updates]
enabled = true
channel = "stable"
versioning = "semver"

[updates.source]
type = "github"
repository = "owner/repository"

[updates.assets]
windows_x86_64 = "MyApp-Setup-{version}.exe"
macos_arm64 = "MyApp-{version}.dmg"
```

The source reads release metadata and assets. Stable checks ignore prereleases.
The updater still compares normalized versions itself; provider ordering is not
treated as version precedence.

## GitLab Releases

GitLab projects use the same normalized release contract. The project may be a
namespace path or a numeric project ID:

```toml
[updates]
channel = "stable"
versioning = "semver"

[updates.source]
type = "gitlab"
project = "group/persona"
token_env = "PERSONA_GITLAB_TOKEN" # optional for private projects

[updates.assets]
windows_x86_64 = "Persona-Setup-{version}.exe"
```

The GitLab adapter reads release asset links and uses `PRIVATE-TOKEN` when a
credential environment variable is configured. A desktop application must not
ship a project maintainer or CI token.

## Generic HTTPS JSON feeds

Applications that do not use GitHub can host the following feed on a company
server, object store, or CDN:

```toml
[updates]
channel = "stable"
versioning = "semver"

[updates.source]
type = "json"
url = "https://downloads.example.com/myapp/stable.json"
token_env = "MYAPP_UPDATE_TOKEN" # optional; never put the token in TOML
```

The feed shape is:

```json
{
  "schema": 1,
  "app_id": "myapp",
  "channel": "stable",
  "version": "1.4.0",
  "release_notes_url": "https://downloads.example.com/myapp/1.4.0",
  "artifacts": [
    {
      "platform": "windows",
      "architecture": "x86_64",
      "kind": "installer",
      "filename": "MyApp-Setup-1.4.0.exe",
      "url": "https://downloads.example.com/myapp/MyApp-Setup-1.4.0.exe",
      "sha256": "..."
    }
  ]
}
```

For private applications, the feed and artifacts can require end-user
authentication. A desktop app must not contain a maintainer or CI token. Use a
user credential callback, an environment variable for controlled deployments,
or short-lived signed artifact URLs issued by the developer's service.

## Python usage

```python
from spiritus import GitHubReleaseSource, UpdateClient, UpdateConfig

config = UpdateConfig(
    app_id="myapp",
    current_version="1.0.0",
    source=GitHubReleaseSource("owner/repository"),
    asset_patterns={"windows_x86_64": "MyApp-Setup-{version}.exe"},
)

result = UpdateClient(config).check()
if result.available:
    print(result.candidate.version, result.artifact.url)
```

Custom release systems implement `ReleaseSource`; custom version schemes
implement `VersionPolicy`. TOML remains declarative; pass a custom policy to
`UpdateConfig.from_mapping(..., version_policy=policy)` or construct
`UpdateConfig` directly. Arbitrary Python callbacks are intentionally not
encoded in TOML.

## Staging and installer handoff

An available result can be streamed into an application-owned directory. A
SHA-256 checksum is required by default, the filename is constrained to a
single safe path component, the response size is bounded, and the final file is
published atomically only after verification:

```python
from spiritus import UpdateClient, UpdateDownloader, SubprocessInstallerHandoff
from spiritus.runtime.paths import app_data_dir

result = UpdateClient(config).check()
if result.available:
    staged = UpdateClient(config).stage_update(
        result,
        app_data_dir("myapp") / "updates",
        downloader=UpdateDownloader(),
    )
    # Application policy decides whether to prompt, quit, and launch this.
    SubprocessInstallerHandoff().launch(staged, args=("/S",))
```

`UpdateDownloader` rejects non-HTTPS URLs by default, checksum mismatches,
size mismatches, oversized responses, and partial files. The installer handoff
uses an argument vector with `shell=False`; signing, trust-store policy,
rollback, restart, and platform-specific installer arguments remain
application-owned.
