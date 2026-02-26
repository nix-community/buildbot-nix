# (experimental) Hercules CI effects

See [flake.nix](../flake.nix) for an example and the
[Hercules CI effects documentation](https://docs.hercules-ci.com/hercules-ci/effects/)
for the upstream reference.

## CLI usage

The `buildbot-effects` CLI can list and run effects locally, or against remote
repositories using
[Nix flake references](https://nix.dev/manual/nix/latest/command-ref/new-cli/nix3-flake#flake-references).

### Local repository

```console
$ cd my-repo
$ buildbot-effects list
["deploy", "notify"]

$ buildbot-effects run deploy
```

### Remote repository (flake reference)

No local checkout needed:

```console
$ buildbot-effects run github:org/repo/branch#deploy
$ buildbot-effects list github:org/repo/branch
$ buildbot-effects list-schedules github:org/repo/branch
$ buildbot-effects run-scheduled github:org/repo#flake-update update
```

### Subcommands

| Command          | Description                           |
| ---------------- | ------------------------------------- |
| `list`           | List available effects                |
| `run`            | Run a single effect                   |
| `list-schedules` | List scheduled effects                |
| `run-scheduled`  | Run a specific effect from a schedule |

### Flags

All subcommands accept:

| Flag       | Description                                               |
| ---------- | --------------------------------------------------------- |
| `--rev`    | Git revision to use                                       |
| `--branch` | Git branch to use                                         |
| `--repo`   | Git repo name                                             |
| `--path`   | Path to the repository (default: current directory)       |
| `--debug`  | Enable debug mode (may leak secrets such as GITHUB_TOKEN) |

`run` and `run-scheduled` also accept:

| Flag        | Description                      |
| ----------- | -------------------------------- |
| `--secrets` | Path to a JSON file with secrets |

## Running effects locally with secrets

Pass `--secrets` to provide secrets when running effects locally. The file is a
JSON object where each key is a secret name and its value has a `"data"` field
containing key-value pairs:

```json
{
  "my-secret": {
    "data": {
      "token": "ghp_xxxxxxxxxxxx",
      "username": "deploy-bot"
    }
  }
}
```

```console
$ buildbot-effects run --secrets secrets.json deploy
```

Inside the effect, secrets are available at `/run/secrets.json` (via
`HERCULES_CI_SECRETS_JSON`). This follows the
[hercules-ci secrets format](https://docs.hercules-ci.com/hercules-ci/effects/declaration/#secrets).

## Buildbot secrets configuration

When running effects through buildbot (not locally), secrets are configured at
different scopes:

1. **Repository-specific**: `"github:owner/repo"` — applies to a single
   repository
2. **Organization-wide**: `"github:org/*"` — applies to all repositories in an
   organization

```nix
services.buildbot-nix.master.effects.perRepoSecretFiles = {
  # All repos in nix-community org get this token
  "github:nix-community/*" = config.agenix.secrets.nix-community-effects.path;

  # This specific repo gets its own token (overrides org-level)
  "github:nix-community/buildbot-nix" = config.agenix.secrets.buildbot-nix-effects.path;

  # All repos in a Gitea org
  "gitea:my-org/*" = config.agenix.secrets.my-org-effects.path;
};
```

The secrets files must be valid JSON files containing the secrets that will be
made available to your effects at runtime.
