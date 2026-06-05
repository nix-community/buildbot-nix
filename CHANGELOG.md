# Changelog

## Unreleased — standalone CI engine

buildbot-nix no longer runs on top of Buildbot. The master/worker pair is
replaced by one asyncio service that handles forge webhooks, nix-eval-jobs
evaluation, builds through the local nix daemon, commit statuses,
hercules-ci-style effects, and its own web frontend with JSON API, SSE live
logs, and Prometheus metrics.

### Migration

- Module: `services.buildbot-nix.master.*` options rename automatically to
  `services.buildbot-nix.*` with deprecation warnings; update at your
  convenience. `nixosModules.buildbot-master` and `nixosModules.buildbot-worker`
  alias the new module. Options without an engine equivalent (workers,
  oauth2-proxy mode, dbUrl, Gitea webhook secret) raise errors with migration
  hints.
- Workers are gone: delete `workersFile`, worker passwords, and `localWorkers`.
  Builds run through the nix daemon and scale via nix remote builders.
- Database: PostgreSQL only, fresh schema with plain SQL migrations. Build
  history does not migrate. A local PostgreSQL over the unix socket is
  provisioned by default; remote databases use `database.url` or
  `database.urlFile`.
- Authentication: `httpbasicauth` and the oauth2-proxy `accessMode.fullyPrivate`
  deployment are removed. The built-in GitHub/Gitea/OIDC login hides private
  repositories from unauthorized users. GitHub token mode is removed; use a
  GitHub App.
- Webhook endpoints keep their legacy aliases (`/change_hook/github`,
  `/change_hook/gitea`). Commit status context names lose the buildbot-era
  `buildbot/` prefix (e.g. `buildbot/nix-build ...` becomes `nix-build ...`);
  update branch protection rules that require the old contexts.
- `buildbot-nix.toml` per-repository configuration is unchanged.
- Commit statuses link to the new web UI; the JSON API moves to `/api/*` with an
  OpenAPI schema at `/openapi.json`.

### Improvements

- Build identity is the post-merge tree hash: identical trees across
  branches/PRs reuse results instead of rebuilding.
- Crash recovery: unfinished builds resume from stored eval results after a
  restart, without re-evaluation.
- Evaluation runs in a bwrap sandbox with a kernel-enforced memory cap
  (delegated cgroup v2 subtree).
- Per-user API tokens for scripted access.
