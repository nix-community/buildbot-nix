## ADDED Requirements

### Requirement: Evaluation via nix-eval-jobs

The system SHALL maintain one persistent clone per project in a central
repository location (fetched on demand, cleaned up for removed projects) and
create a git worktree per build from it; the local PR merge happens in the
worktree, the worktree is what gets evaluated, and it is removed when the build
finishes. Fetches into the shared clone SHALL be serialized per project;
orphaned worktrees SHALL be pruned at startup and periodically; on git
corruption of a shared clone the system SHALL re-clone automatically (clones are
cache, not state). The system SHALL evaluate the worktree's configured flake
attribute set. For pull requests, the system SHALL locally merge the PR head
into the base branch and evaluate the merge result; a merge conflict SHALL fail
the build with a clear message, while statuses remain attached to the PR head
commit. The system SHALL evaluate (default `.#checks`) using nix-eval-jobs with
parallel workers, producing one build job per attribute/system, honoring
per-repo configuration (`buildbot-nix.toml` / flake attrs, unchanged name and
format) for attribute selection, `flake_dir` (validated against path traversal),
and `lock_file` (`--reference-lock-file`). A `showTrace` option SHALL pass
`--show-trace` to evaluation and builds. Concurrent evaluations SHALL be capped
separately from build concurrency (default 1, matching today's global eval
lock); evaluation OOM SHALL be treated as a permanent failure with a clear
message (no automatic retry), with eval running under a cgroup memory limit.

Default eval-worker count and per-worker memory limits SHALL be computed
dynamically from host CPU count and available RAM (porting existing
master.cfg.py logic) and be overridable in configuration. Queued builds SHALL
run in FIFO order.

Evaluation SHALL register GC roots for evaluated derivations (nix-eval-jobs
gc-roots dir) for the duration of the build, releasing them afterwards.
Evaluation warnings (`evaluation warning:` blocks on stderr) SHALL be extracted,
shown on the build page, and their count appended to the `nix-eval` status
description.

#### Scenario: Evaluation fan-out

- **WHEN** evaluation of a commit completes
- **THEN** one build job exists per evaluated attribute, including its
  derivation path and system, and the derivations are GC-rooted until the build
  finishes

#### Scenario: PR merge conflict

- **WHEN** the local merge of a PR head into its base branch conflicts
- **THEN** the build fails with a merge-conflict message and a failure status is
  reported on the PR head commit

#### Scenario: Evaluation error

- **WHEN** nix-eval-jobs reports an evaluation error
- **THEN** the build fails with the evaluation error visible in the log and
  reported to the forge

### Requirement: Sandboxed builds and evaluation

The system SHALL execute repository code only inside the Nix sandbox; evaluation
and builds SHALL NOT run arbitrary repository scripts on the host. Evaluation
(nix-eval-jobs) SHALL additionally run inside a bubblewrap (bwrap) sandbox as
defense in depth, mounting only: the build worktree, the nix store, the gc-roots
directory, the nix daemon socket, and — when fetching private flake inputs — a
per-build, repository-scoped, short-lived credential file. Global service
credentials (`$CREDENTIALS_DIRECTORY`) SHALL never be visible inside the
sandbox. Residual risk is accepted and documented: with network access, the
repo-scoped credential is exfiltratable during eval (bounded by its scope and
lifetime), and the daemon socket allows requesting arbitrary builds — the
service user SHALL NOT be a nix trusted-user.

#### Scenario: No host code execution

- **WHEN** a project build runs
- **THEN** the only processes spawned for repository content are nix evaluation
  (inside bwrap) and nix builds (inside the Nix sandbox)

#### Scenario: Evaluation cannot read service secrets

- **WHEN** a malicious flake attempts to read service credentials or host paths
  during evaluation
- **THEN** the bwrap sandbox denies access; only the worktree, nix store,
  gc-roots dir, daemon socket, and the per-build repo-scoped credential (if any)
  are visible

### Requirement: Build execution with bounded concurrency

The system SHALL build each derivation via the nix daemon (using remote builders
as configured in nix), with a configurable limit on concurrent build jobs,
skipping locally cached results. Each attribute build SHALL be subject to a
configurable overall timeout (default 3 hours, current default) and a
configurable max-silent-time (default 20 minutes, passed as
`--max-silent-time`), after which it is killed and marked failed. The
concurrency limit SHALL be a single global limit across all projects, dequeued
fair round-robin across builds (FIFO within a build's topological order) so
large matrices cannot starve small projects. Within a build, jobs SHALL be
scheduled in topological order of their derivation closures; when a derivation
fails, dependent jobs SHALL be marked failed (dependency failure) without being
built. Evaluated attributes SHALL be filtered to the configured list of
supported systems. Each attribute's captured log SHALL be capped at a
configurable size (default 64 MB), keeping head and tail on truncation.

Attribute builds failing with transient errors (e.g. remote builder disconnect,
network failure) SHALL be retried once automatically before being marked failed.

#### Scenario: Transient failure retried

- **WHEN** an attribute build fails due to a transient infrastructure error
- **THEN** it is retried once, and only marked failed if the retry also fails

#### Scenario: Build timeout

- **WHEN** an attribute build exceeds the configured timeout
- **THEN** its process group is killed and the attribute is marked failed with a
  timeout message

#### Scenario: Failed dependency skips dependents

- **WHEN** an attribute's derivation fails and other attributes depend on it
- **THEN** the dependent attributes are marked failed as dependency failures
  without running their builds

#### Scenario: Cached derivation

- **WHEN** a derivation's outputs are already valid in the local store
- **THEN** the attribute is reported as succeeded without rebuilding, and its
  outputs still receive gcroot registration and outputs-path updates

#### Scenario: Substitutable derivation

- **WHEN** a derivation is substitutable from a binary cache but not present
  locally
- **THEN** the build job still runs to trigger substitution

#### Scenario: Small project not starved

- **WHEN** a build with thousands of attributes is in progress and a small build
  is enqueued
- **THEN** the small build's attributes are interleaved by round-robin dequeue
  rather than waiting for the large build to finish

### Requirement: GC-root protection and outputs path

The system SHALL maintain a garbage-collection root for the latest successful
build of each attribute per project branch, replacing the previous root.
Per-branch configuration (glob-matched branch rules, as today: `match_glob`,
`register_gcroots`, `update_outputs`) SHALL control whether gcroots are
registered and whether output links are updated. When an outputs path is
configured, the system SHALL update per-branch symlinks to the latest successful
outputs (used by deployment tooling).

#### Scenario: Outputs path updated

- **WHEN** a build on a branch with `update_outputs` enabled succeeds and an
  outputs path is configured
- **THEN** the outputs directory contains updated symlinks to the new build
  outputs

#### Scenario: Root rotation

- **WHEN** a newer build of an attribute succeeds
- **THEN** the gcroot points to the new output and the old root is released

### Requirement: Post-build steps and binary cache upload

The system SHALL support configurable post-build steps (arbitrary command with
environment, warn-only option) executed after successful attribute builds,
matching current behavior, including the existing cachix and niks3 integrations
and custom upload commands (e.g. attic, s3).

#### Scenario: Warn-only post-build step fails

- **WHEN** a post-build step configured as warn-only fails
- **THEN** the attribute build remains successful and the step failure is
  visible in the log

#### Scenario: Outputs pushed to cache

- **WHEN** an attribute build succeeds and a cache upload is configured
- **THEN** the outputs are uploaded to the cache and upload failures are visible
  in the build log

### Requirement: Effects execution

The system SHALL run hercules-ci effects using the existing buildbot_effects
runner after successful builds according to the default branch's repo config,
exactly as today: always on the default branch; on pull requests when
`effects_on_pull_requests` is set; on branches matching `effects_branches`
globs. Per-repo secret files SHALL resolve by exact `forge:owner/repo` match and
then org wildcard `forge:owner/*`; `effects.extraSandboxPaths` SHALL be
supported. Operator-supplied effects secrets are provided via systemd
`LoadCredential`.

#### Scenario: Effect after successful build

- **WHEN** a default-branch build succeeds and the flake defines effects
- **THEN** the configured effects run and their logs and statuses appear in the
  build

#### Scenario: Effects on PR when opted in

- **WHEN** the default branch's `buildbot-nix.toml` sets
  `effects_on_pull_requests` and a PR build succeeds
- **THEN** the effects run for that PR build
