# Changelog

## Unreleased — standalone CI engine

buildbot-nix no longer runs on top of Buildbot. The old master/worker pair is
now a single asyncio service that does everything itself: forge webhooks,
evaluation with nix-eval-jobs, builds through the local nix daemon, commit
statuses, hercules-ci-style effects, and its own web UI with a JSON API, live
logs over SSE, and Prometheus metrics.

### What you need to do when upgrading

**NixOS module.** Your existing `services.buildbot-nix.master.*` config keeps
working: options rename to `services.buildbot-nix.*` automatically and print
deprecation warnings. `nixosModules.buildbot-master` and
`nixosModules.buildbot-worker` are aliases for the new module. Options that have
no equivalent anymore (workers, oauth2-proxy mode, `dbUrl`, the Gitea webhook
secret) fail the build with a hint on what to do instead.

**Workers are gone.** Delete `workersFile`, worker passwords and `localWorkers`.
Builds go through the nix daemon and scale with ordinary nix remote builders.

**Database.** PostgreSQL only, with a fresh schema and plain SQL migrations.
Build history does not carry over. By default the module provisions a local
PostgreSQL over the unix socket; for a remote database set `database.url` or
`database.urlFile`.

**Authentication.** `httpbasicauth` and the oauth2-proxy
`accessMode.fullyPrivate` setup are gone. The built-in GitHub/Gitea/OIDC login
covers the same need: private repositories are hidden from anyone not authorized
to see them. GitHub token mode is also gone; use a GitHub App.

`authBackend` is removed. Enable forges explicitly (`github.enable`,
`gitea.enable`, `oidc.enable`); every enabled forge with OAuth credentials
configured offers a login, and several can be active at once.

`admins` entries must be provider-qualified: `github:Mic92`, not `Mic92`.
Unqualified entries never match and only log a warning.

OAuth callback URLs change: update your GitHub App / Gitea application to
`https://<domain>/auth/<provider>/callback` (buildbot used `/auth/login`), e.g.
`https://buildbot.example.com/auth/github/callback`.

**Commit statuses.** Nothing to do: context names keep the `buildbot/` prefix
(`buildbot/nix-eval`, `buildbot/nix-build ...`), so branch protection rules keep
working. Statuses link to the new web UI.

**Webhooks.** The old endpoints (`/change_hook/github`, `/change_hook/gitea`)
still work as aliases. However, per-repository GitHub webhooks are no longer
created: events arrive through the App-level webhook. Enable the webhook on your
GitHub App (Active, URL `https://<domain>/webhooks/github`, secret matching
`webhookSecretFile`, events `push` and `pull_request`); see
[docs/GITHUB.md](./docs/GITHUB.md). Subscribing to the `pull_request` event
requires the "Pull requests: Read-only" repository permission. Adding a
permission must be accepted on every installation of the app (your user account
and each organization) under Settings → GitHub Apps → Configure. The engine logs
a warning at startup if the app is misconfigured.

Gitea webhooks now register at `https://<domain>/webhooks/gitea` with an
auto-generated per-repository secret stored in the database —
`gitea.webhookSecretFile` is gone. Existing hooks are re-synced in place,
leftover buildbot-era hooks pointing at this instance are removed. Hooks
subscribe to `push`, `pull_request` and `pull_request_sync`.

**Project enablement.** Which repositories get built is now a per-project toggle
in the web UI (admins only). `topic` is reduced to a one-shot import: on first
startup with an empty database, repositories carrying the topic are enabled;
afterwards it is ignored. `userAllowlist`/`repoAllowlist` remain a hard boundary
at discovery time.

**Per-repository config.** `buildbot-nix.toml` is unchanged.

**Post-build steps.** `interpolate` placeholders still work. Properties: `attr`,
`out_path`, `drv_path`, `system`, `project`, `branch`, `revision`, `pr_number`,
`default_branch`. Two changes:

- PR builds no longer run under a `refs/pull/N/merge` branch — use
  `%(prop:pr_number)s` instead of parsing the branch name.
- `%(secret:NAME)s` reads systemd credentials of the `buildbot-nix` unit, so
  move `LoadCredential` entries from `systemd.services.buildbot-master` to
  `systemd.services.buildbot-nix`.

**Buildbot customizations.** Anything that reached into Buildbot itself —
`services.buildbot-master.extraConfig`, the manhole, `pythonPackages` — has no
equivalent.

**API.** The JSON API moves to `/api/*`, with an OpenAPI schema at
`/openapi.json`.

### What you get

- Builds are keyed by the post-merge tree hash: identical trees across branches
  and PRs reuse results instead of rebuilding.
- Crash recovery: after a restart, unfinished builds resume from their stored
  eval results without re-evaluating.
- Evaluation runs in a bwrap sandbox with a kernel-enforced memory cap
  (delegated cgroup v2 subtree).
- Per-user API tokens for scripted access.
- GitLab support (`services.buildbot-nix.gitlab`): token-based, with
  per-repository webhooks and commit statuses. See docs/GITLAB.md.
