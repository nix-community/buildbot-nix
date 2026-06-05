## Why

Buildbot is a heavyweight, general-purpose CI framework. buildbot-nix only uses
a narrow slice of it (schedulers, change sources, a web UI, status reporters)
while fighting its abstractions: we patch buildbot, depend on its unstable
internal APIs, carry a master/worker split we do not need (all builds go through
the shared nix store and remote builders anyway), and inherit its Twisted-based
async model. Replacing buildbot with our own purpose-built engine removes the
dependency, the patches, and the master/worker complexity while keeping the
user-visible behavior of the system.

## What Changes

- **BREAKING**: Remove the buildbot (and buildbot-worker) dependency entirely;
  buildbot-nix becomes a standalone CI service.
- **BREAKING**: Remove the master/worker separation; a single service evaluates
  and builds (builds are distributed via nix remote builders as before).
- New CI core engine: job queue, scheduling, state persistence (reuse existing
  Postgres/DB layer), build lifecycle (eval → build per attribute → gcroot).
- New web frontend: build/project overview, build logs, login via GitHub/OIDC to
  control builds (restart, cancel) — replacing the buildbot web UI.
- Keep all existing system properties:
  - Fast parallel evaluation via nix-eval-jobs
  - GitHub/Gitea integration (webhooks and pull-based polling), CI status on PRs
    and default branch
  - Build matrix from flake attributes (`.#checks`)
  - GC-root protection of the latest build per attribute
  - No arbitrary code outside the Nix sandbox
  - hercules-ci effects support
  - Scheduled Hercules effects (`onSchedule`), build cancellation on superseding
    pushes
- NixOS module updated: single service unit instead of master + worker units;
  old options deprecated/mapped where possible. Linux only.
- **BREAKING**: GitHub legacy token auth dropped; GitHub App credentials
  required.

## Capabilities

### New Capabilities

- `ci-engine`: core scheduling/execution engine — receives change events, runs
  nix-eval-jobs, fans out per-attribute builds, manages cancellation, retries,
  and state persistence.
- `web-frontend`: HTTP UI and API — project/build/log views, authenticated build
  control (restart/cancel) via GitHub/OIDC login, webhook endpoints.
- `forge-integration`: GitHub/Gitea project discovery, webhook + pull-based
  change sources, commit status reporting.
- `nix-build-pipeline`: evaluation via nix-eval-jobs, local/remote nix builds,
  gcroot management, effects execution.
- `nixos-module`: single-service NixOS module replacing master/worker modules,
  with migration path for existing options.

### Modified Capabilities

<!-- none: no existing specs in openspec/specs/ -->

## Impact

- `buildbot_nix/` Python package: large rewrite; buildbot plugin classes
  (schedulers, steps, reporters, workers) replaced by own engine code. DB models
  and forge clients largely reusable.
- `buildbot_effects/`: kept, invoked by the new engine instead of buildbot
  steps.
- `nix/master.nix`, `nix/worker.nix`, `nixosModules/`: replaced by
  single-service module.
- `patches/`: buildbot patches deleted.
- `packages/`, flake outputs, checks: updated for new service.
- Deployments: operators migrate config; build history from buildbot DB is not
  migrated (fresh state), but commit statuses on forges remain.
