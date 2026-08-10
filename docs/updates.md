# Application update discovery

Spiritus provides opt-in, check-only update discovery through
`spiritus.updates`. It does not download, replace, or restart an application.
The application decides when to check and owns its installer policy.

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
implement `VersionPolicy`. Arbitrary Python callbacks are intentionally not
encoded in TOML.

Download verification, signed manifests, and platform installer handoff are
later phases. GitLab Releases is planned as a named provider adapter after the
generic feed path is established; a GitLab project can use the JSON feed before
that adapter lands.
