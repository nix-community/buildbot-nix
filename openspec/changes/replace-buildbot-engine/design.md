## Context

buildbot-nix today is a set of buildbot plugins: custom schedulers, build steps
(nix-eval-jobs, nix build, gcroot, effects), change sources (GitHub/Gitea
webhooks and pollers), status reporters, and a local worker. Buildbot provides
the process model (Twisted), the web UI, the database schema, and the
master/worker protocol. We patch buildbot and depend on internal APIs
(`buildbot.db.base`, step/reporter classes). The master/worker split adds
deployment complexity with no benefit: builds run via the nix daemon and remote
builders, not on buildbot workers.

This change replaces buildbot with a purpose-built single-process CI engine
while preserving all user-visible behavior (see proposal).

## Goals / Non-Goals

**Goals:**

- Single Python service (no master/worker) that evaluates and builds Nix
  projects.
- Keep: nix-eval-jobs evaluation, per-attribute build fan-out, GitHub/Gitea
  webhooks + polling, commit status reporting, OAuth/OIDC-gated build control,
  gcroots, effects, scheduled builds, cancellation of superseded builds,
  failed-build caching.
- Own web frontend with project/build/log views and restart/cancel actions.
- Reuse existing code where it is buildbot-independent: forge API clients,
  project config parsing, models, effects runner.
- NixOS module with a single systemd unit and a migration path for existing
  options.

**Non-Goals:**

- Migrating build history from buildbot's database.
- Distributed CI workers (nix remote builders cover multi-arch).
- Plugin system or general-purpose CI features beyond what buildbot-nix uses
  today.
- Feature parity with buildbot's web UI (only the views buildbot-nix users
  need).

## Decisions

### D1: Single asyncio process, no Twisted

Replace Twisted with stdlib asyncio. All concurrency (webhook handling,
eval/build subprocesses, status posting) runs in one event loop; nix does the
heavy lifting in subprocesses. Alternatives: keep Twisted (drags buildbot idioms
along), multi-process supervisor (unneeded complexity — nix-daemon already
parallelizes builds).

### D2: Web stack: FastAPI + uvicorn, server-rendered HTML + htmx

FastAPI gives async HTTP, OpenAPI for the JSON API, and is already nix-packaged.
UI is server-rendered Jinja2 templates with htmx for live updates (log streaming
via SSE/websocket). Alternatives: SPA (build-tooling burden, overkill), aiohttp
(less batteries for auth/validation).

### D3: State in PostgreSQL via SQLAlchemy (async), own schema

Define our own tables: projects, builds, build_steps/attributes, logs metadata,
failed_builds, failed_statuses. Reuse the intent of the existing `db/`
components but drop `buildbot.db.base`. Logs stored as files on disk
(compressed), DB stores metadata. PostgreSQL only — one code path, NixOS module
provisions a local Postgres by default. Alternatives: also support SQLite
(rejected: two code paths, divergent SQL behavior), keep buildbot schema
(couples us to buildbot).

### D4: Build pipeline = state machine per change event

Change event (push/PR) → create Build → run nix-eval-jobs → one BuildAttribute
per eval result → `nix build` each (bounded concurrency, skip cached failures) →
gcroot update → aggregate status → report to forge. Cancellation: new event for
same branch/PR cancels in-flight build (process-group kill). This mirrors
today's trigger/step graph without buildbot's scheduler/builder indirection.

### D5: Auth: OAuth2 (GitHub) and generic OIDC, signed session cookies

Reuse existing authz rules (org membership / user allowlist) from `authz.py`.
Read-only views public by default (configurable); restart/cancel require login.
Alternatives: oauth2-proxy in front (rejected and dropped, see Resolved
Decisions; the service never trusts identity headers from upstream proxies).

### D6: Incremental code reuse, not incremental migration

Forge clients (`github/`, `gitea_projects.py`), `project_config.py`,
`models.py`, `repo_config/`, `buildbot_effects/` are kept nearly as-is.
Buildbot-coupled modules (steps, reporters, workers, schedulers) are rewritten.
The service ships as one new entry point; no period of running both engines side
by side in one deployment.

## Risks / Trade-offs

- [Rewrite regressions in subtle behaviors (status text, skip logic,
  cancellation races)] → Port existing unit tests; add integration tests driving
  fake forge + real nix builds in NixOS VM tests.
- [Losing buildbot UI features users rely on (e.g. waterfall)] → Survey needed
  views first; keep JSON API so users can script around gaps.
- [Existing deployments break on upgrade] → NixOS module asserts on removed
  options with migration messages; document upgrade; tag last buildbot-based
  release.
- [Log storage growth without buildbot's janitor] → Built-in retention/cleanup
  task with configurable horizon.
- [Single process becomes bottleneck on huge instances] → Eval/build work is in
  subprocesses; HTTP and bookkeeping are I/O-bound; acceptable. Revisit only if
  proven.

## Migration Plan

1. Build new engine + frontend behind new package/module
   (`nixosModules.buildbot-nix` single service) while old modules remain in
   repo.
2. Validate via NixOS VM tests covering webhook → eval → build → status flow for
   GitHub and Gitea.
3. Release: new major version; remove `nix/master.nix`, `nix/worker.nix`,
   buildbot patches; module renames with
   `mkRemovedOptionModule`/`mkRenamedOptionModule`.
4. Rollback: operators pin previous release tag; forges are stateless from our
   side (statuses re-posted on next push).

## Resolved Decisions

- Webhooks: new paths (`/webhooks/github`, `/webhooks/gitea`) plus aliases at
  old buildbot paths (`/change_hook/github`, `/change_hook/gitea`); aliases
  removed in a later release.
- Service/binary name stays `buildbot-nix` for brand continuity.
- Visibility: builds of public repositories readable without login; builds,
  logs, and existence of private repositories visible only to logged-in users
  whose forge account can access the repository (checked via forge API, cached).
  Login required for restart/cancel everywhere.
- Migration: clean cut — new major release replaces buildbot modules in the same
  release, no transition period with both engines.
- No buildbot-DB import tooling: projects re-discovered from forge. Migration
  support is docs + module asserts only.
- Restart granularity: whole build and individual attributes (per-attribute
  rebuild reuses existing eval results, no re-eval).
- Forge statuses: same as today — combined per phase (`nix-eval`, `nix-build`,
  `nix-effects`).
- Branch scope: same as today — default branch, pull requests, and per-repo
  configured extra branches.
- Observability: Prometheus `/metrics` endpoint (build counts, durations, queue
  depth).
- API auth: personal API tokens generated in the UI after login, alongside
  session cookies.
- Multi-forge: one instance serves GitHub and Gitea simultaneously (current
  behavior).
- Per-repo config: `buildbot-nix.toml` name and format unchanged.
- NixOS module offers optional nginx + ACME virtual host (as today); service
  also runnable bare behind own proxy.
- Build queue: global concurrency cap with fair round-robin dequeue across
  builds (FIFO within a build's topological order) to avoid head-of-line
  blocking by huge matrices.
- Secrets (forge tokens, effects secrets): delivered via systemd
  `LoadCredential`, read from `$CREDENTIALS_DIRECTORY`.
- Concurrency defaults derived from CPU count (eval workers, parallel attribute
  builds), overridable in module options.
- Fork PRs: all pull requests are built (current behavior — audit showed no
  trust gating exists today; the Nix sandbox is the trust boundary).
- Commit status context names unchanged from current implementation so existing
  branch protection rules keep working.
- GitHub auth: GitHub App only; legacy token mode dropped (module assert with
  migration message).
- Per-attribute build timeout: configurable, default 3h (current default); plus
  configurable max-silent-time (default 20 min, current behavior).
- JSON API unversioned under `/api/...`.
- Host platform: Linux only (NixOS module); other architectures via nix remote
  builders.
- PR close/merge cancels in-flight builds for that PR.
- `[skip ci]` / `[ci skip]` in commit message skips the build.
- No badge endpoint.
- DB migrations: simple versioned SQL scripts applied at startup (no Alembic),
  run in a transaction under an advisory lock.
- Postgres outage policy: webhook handlers fail fast with 500 (GitHub App
  redelivers; Gitea backstopped by startup reconciliation incl. open PRs);
  running builds buffer state transitions with bounded retry, service exits for
  systemd restart if irrecoverable; DB-connectivity health endpoint.
- Logs stored zstd-compressed on disk in independent frames (frame per flush) so
  live tailing decompresses only new frames; one disk reader per running
  attribute fans out to all SSE subscribers; (de)compression off the event loop
  via threads.
- oauth2-proxy deployment mode dropped; built-in OAuth/OIDC replaces it (module
  assert with migration message).
- Binary cache upload (cachix/attic/s3 post-build) kept as today.
- Retention default: 90 days.
- No outgoing notifications beyond forge statuses.
- Eval worker count/memory limits computed dynamically from host RAM, porting
  the existing `MemoryInfo` logic from `packages/master.cfg.py`; overridable.
- Build lists paginated with status/branch filters.
- Build URLs use per-project sequential numbers (e.g.
  `/projects/<name>/builds/42`).
- Login providers: GitHub OAuth, Gitea OAuth, and generic OIDC; private-repo
  access checks use the matching forge identity.
- Private-project visibility: fetch the user's accessible-repo set once per user
  (cached, configurable TTL, default 1h, negatives cached) instead of
  per-(user,repo) checks, to avoid forge API amplification on feed/search pages;
  separate rate budget from status posting.
- Private repo fetch auth: GitHub uses short-lived App installation tokens
  minted per fetch; Gitea uses token/netrc or per-repo SSH key/known-hosts
  (current `gitea.sshPrivateKeyFile` behavior).
- Raw log download endpoint (plain text and .zst) in addition to HTML viewer.
- Global admin role: configured admin user list sees all private projects and
  may control any build.
- Source checkout: git clone to a working directory, evaluate the local checkout
  (current approach).
- HTTP: TCP port plus optional unix socket listener.
- No manual arbitrary-branch trigger; builds are event-driven only (restart of
  existing builds is allowed).
- Project enablement lives in the database and is toggled by admins in the web
  UI; topic-based filtering retained only as a legacy import aid for initial
  discovery.
- No built-in rate limiting (reverse proxy responsibility).
- No admin CLI; JSON API + OpenAPI docs suffice.
- PR builds: engine performs a local merge of the PR head into the base branch
  in the workdir and builds the merge result; statuses are reported on the PR
  head SHA; merge conflicts fail the build.
- Login open to any forge/OIDC account; authz rules gate visibility and control.
- Build identity keyed on the post-merge git tree hash (`HEAD^{tree}` after
  local merge), not the head SHA: gives correct dedup across contexts (branch
  push, PR, fast-forward, re-push of same content) and makes supersede checks
  robust against out-of-order webhook delivery. A shared build is only cancelled
  when superseded in all contexts referencing it.
- Concurrency: single global build-job limit, no per-project caps (fair
  round-robin across builds, see queue bullet). Separate, smaller cap on
  concurrent evaluations (default 1, matching today's global eval lock); eval
  OOM is a permanent failure with a clear message (no auto-retry), eval runs in
  a transient systemd scope with a cgroup memory limit.
- Nix store GC left to operator's nix.gc configuration (no module options).
- Per-attribute log size capped (configurable, default 64 MB); head and tail
  kept on truncation.
- One central location with a persistent clone per project; each build gets its
  own git worktree from that clone (enables concurrent builds of the same
  project without re-fetching). Worktrees removed after build; stale clones
  cleaned up.
- Eval derivations GC-rooted during build via nix-eval-jobs gc-roots dir,
  released after.
- Evaluation runs inside a bubblewrap sandbox: only checkout, nix store,
  gc-roots dir, and nix daemon socket mounted; no access to
  `$CREDENTIALS_DIRECTORY` or other host paths. Defense in depth — nix
  evaluation is mostly pure but the evaluator process itself is a large attack
  surface. Network stays available for flake input fetching; a per-build
  netrc/token file (only the credentials needed for that repository) is
  bind-mounted in, never the full credentials directory. NixOS module must
  permit user namespaces for bwrap within the hardened unit
  (`RestrictNamespaces` tuned accordingly).
- Startup reconciliation: build unbuilt default-branch heads and open-PR heads
  after downtime (“built” = any build record exists for that merge-tree,
  regardless of result); runs after crash recovery, supersede rules resolve
  duplicates.
- Crash recovery resumes rather than restarts: attribute completion is one
  transactional DB write; on startup, attributes already terminal in DB are
  skipped, pending ones re-checked via `nix path-info` before re-running.
  Effects are guarded by a started-flag and never auto-re-run on recovery
  (manual restart only — deploys are not idempotent).
- Forge statuses are combined per phase (`nix-eval`, `nix-build`, `nix-effects`)
  exactly as today — not per attribute; corrects earlier assumption.
- Transient infrastructure failures retried once automatically; cancellation
  sets a flag checked before retry scheduling and before dependency propagation
  (cancelled attributes propagate “skipped (superseded)” to dependents, never
  “dependency failed”, and are not cached as failures).
- Re-aggregation of a build's result (after attribute rebuilds) is serialized
  per build (row lock) with a monotonic status generation so stale forge status
  posts are dropped.
- Projects keyed by stable forge repo ID; renames/transfers keep history.
- Webhooks: GitHub events delivered via the GitHub App subscription (no per-repo
  hook creation needed); Gitea hooks auto-registered per repo on enable. On
  migration, leftover buildbot webhooks are removed only when their URL matches
  this instance's own configured webhook base URL (never “anything that looks
  like buildbot”). Deliveries deduplicated by delivery GUID; supersede decisions
  compare commits/merge-trees, not arrival order. A configurable webhook base
  URL (today's `webhookBaseUrl`) may differ from the UI URL.
- Webhook secrets: GitHub uses the App-level webhook secret (operator-supplied
  via LoadCredential); Gitea secrets are per repository, generated by the
  service and stored in the database.
- Database: local Postgres over unix socket with peer auth by default; module
  option for remote DB with password credential.
- Effects scope follows the default branch's repo config exactly as today:
  default branch always; PRs when `effects_on_pull_requests` is set; branches
  matching `effects_branches` globs. Per-repo secret mapping incl. org wildcard
  (`forge:owner/*`) and `effects.extraSandboxPaths` kept.
- Scheduled effects (Hercules `onSchedule`): discovered via
  `buildbot-effects list-schedules` on default-branch builds, cron-like `when`
  specs with deterministic pseudo-random defaults, persisted, executed via
  `run-scheduled` — ported as-is (this is the real “scheduled” feature; there is
  no plain periodic-build feature today).
- Sessions: configurable lifetime (default 30 days), sliding expiry, signed
  cookies; cookie-signing key auto-generated in StateDirectory (overridable via
  LoadCredential), two-key rotation window. All state-changing endpoints require
  CSRF protection (SameSite cookies + Origin/Sec-Fetch-Site checks or CSRF
  tokens).
- Admin list and allowlist entries are provider-qualified (`github:alice`,
  `gitea:bob`, `oidc:<issuer>/<sub>`) — same username on a different provider
  must not match.
- API tokens stored only as hashes (shown once at creation), constant-time
  compare, optional expiry, immediate revocation; usable for read and control
  with the owner's authz.
- `allowUnauthenticatedControl` option kept for VPN/local dev instances (current
  behavior).
- PR authors may restart/cancel builds of their own PR (matched by forge
  identity on the same forge); scope limited to that PR's builds. Store the PR
  author with the build record.
- UI styling: lightweight classless CSS (e.g. Pico), no JS/CSS build step.
- Build history navigation: prev/next links between a project's builds and
  between results of the same attribute across builds, plus per-attribute
  history view (buildbot-style).
- Homepage: global recent-builds feed + project sidebar; substring search over
  projects/attributes.
- Build page: attributes grouped by system, failed first, client-side filter;
  inline error excerpt per failed attribute (port `nix_error.py` extraction).
- Build list/detail pages live-update while builds run; theme follows system
  preference.
- Log viewer: ANSI colors, line-number permalinks, follow-tail toggle.
- Build page: "rebuild all failed" bulk action; commit/PR/repo forge links;
  relative timestamps (absolute on hover, browser timezone).
- Global queue page showing pending (with FIFO position) and running builds,
  visibility-filtered.
- Per-project UI settings limited to enable/disable; everything else stays in
  `buildbot-nix.toml` (repo as source of truth).
- Feature audit of current code folded in: dependency-aware topological
  scheduling with dependency-failure propagation (`build_trigger.py` is the
  behavioral reference; enumerate its branches as test cases), supported-systems
  filter, per-branch glob config
  (`match_glob`/`register_gcroots`/`update_outputs`), outputs-path symlinks,
  generic post-build steps incl. cachix and niks3 modules, failed-build report
  limit, pull-based poll spread and per-repo SSH keys.
- Forge statuses: combined per-phase contexts
  (`nix-eval`/`nix-build`/`nix-effects`) PLUS per-attribute failure statuses
  (`nix-build <type>:<owner>/<repo>#checks.<attr>`) for failing/cancelled
  attributes, capped by `failedBuildReportLimit` (default 47), persisted per
  revision (`failed_status` semantics) and flipped to success on rebuild —
  including force-running already-built attributes to update a previously failed
  status. Audit showed both layers exist today.
- Per-repo config also includes `flake_dir` (traversal-validated) and
  `lock_file` (`--reference-lock-file`).
- Eval warnings (`evaluation warning:` blocks) extracted, shown on the build
  page, count appended to the `nix-eval` status description; `showTrace` option
  for eval and build.
- Merge-queue branches (`gh-readonly-queue/*`, `gitea-mq/*`, `staging`,
  `trying`) always built (required for merge queues).
- Failed-build cache is opt-in (`cacheFailedBuilds`), default off — matching
  current behavior; timeouts are cached when enabled (accepted trade-off: manual
  restart clears).
- Attributes skipped as already built (local) still get gcroot registration and
  outputs updates; substitutable (cached) jobs are scheduled to trigger
  substitution; mixed eval results produce per-attribute failed records, not one
  build-level failure.
- Repo/user allowlists (`repoAllowlist`/`userAllowlist`) kept as discovery-time
  filters (security boundary), independent of the legacy topic import.
- Optional HTTP serving of the outputs directory (nginx autoindex location)
  kept.
- Worktree/clone hygiene: per-project fetch lock, `git worktree prune` + orphan
  sweep at startup and periodically, auto re-clone on git corruption (clones are
  cache), periodic `git gc`.
- bwrap eval sandbox is defense in depth, not full isolation: the nix daemon
  socket inside the sandbox is the real privilege (do not configure the service
  user as a nix trusted-user); residual risk that the repo-scoped, short-lived
  credential mounted for flake-input fetching can be exfiltrated via network
  during eval — bounded by token scope/lifetime; prefer read-only Gitea deploy
  keys over a global token. Mounts: worktree, nix store, gc-roots dir, daemon
  socket, per-build credential file. `RestrictNamespaces` tuned to allow bwrap.
