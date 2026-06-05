## 1. Foundation

- [x] 1.1 Create new package skeleton `buildbot_nix/buildbot_nix/engine/`
      (module path decided now, service name stays buildbot-nix): asyncio entry
      point, config loading (pydantic models adapted from `models.py` — note it
      imports buildbot, e.g. `Interpolate.to_buildbot`, so strip buildbot
      couplings while porting), structured logging
- [x] 1.2 Add dependencies (FastAPI, uvicorn, SQLAlchemy[asyncio], asyncpg,
      Jinja2, httpx) to pyproject.toml and nix packaging; remove nothing yet
- [x] 1.3 Build migration framework (versioned SQL scripts, transactional,
      advisory lock) plus initial schema: projects, builds, build_attributes,
      log metadata, failed_builds, failed_statuses; later sections add their own
      tables (users/sessions, api_tokens, gitea webhook secrets, scheduled
      effects, ACL cache) via new migration scripts
- [x] 1.4 Unit tests for schema CRUD against ephemeral Postgres (e.g. pytest
      fixture with initdb)

## 2. Nix build pipeline

- [x] 2.0 Central per-project persistent clones + per-build git worktrees
      (created for eval, removed after build; per-project fetch lock,
      `git worktree prune` + orphan sweep, auto re-clone on corruption, periodic
      `git gc`); local merge of PR head into base branch in the worktree
      (conflict → failed build, status on head SHA); build identity = post-merge
      tree hash. NOTE: define a fetch-credentials provider interface here with a
      static/netrc implementation; the GitHub App per-fetch installation tokens
      plug in later via 4.1 — until then only public/netrc repos clone
- [x] 2.1 Implement async nix-eval-jobs runner (port logic from `nix_eval.py`,
      drop buildbot step classes), streaming results, honoring repo config
      (`repo_config/`, `project_config.py`); port dynamic eval worker/memory
      sizing from `packages/master.cfg.py`; wrap nix-eval-jobs in bwrap sandbox
      (worktree + nix store + gc-roots dir + daemon socket + per-build
      repo-scoped credential file only; never `$CREDENTIALS_DIRECTORY`),
      launched in a transient systemd scope with cgroup memory limit; eval
      concurrency cap (default 1); eval OOM = permanent failure; honor
      `flake_dir`/`lock_file`/`showTrace`; extract and store evaluation warnings
      (status-text formatting happens in 4.4); note: only the Linux path of
      `MemoryInfo` needs porting
- [x] 2.1b Port post-build steps (generic command, warn-only) incl. cachix and
      niks3 module integrations
- [x] 2.1c Port dependency-aware scheduling from `build_trigger.py` (treat it as
      the behavioral spec; enumerate its branches as test cases first):
      topological order by drv closures, dependency-failure propagation
      (preserve the get_failed_dependents-before-closure-pruning invariant),
      supported-systems filter, CacheStatus handling (local → skip but record
      out-paths for gcroots/outputs; cached → schedule to substitute),
      per-attribute failed-eval records
- [x] 2.2 Implement attribute build executor: `nix build` subprocess per
      derivation, global cap with fair round-robin dequeue across builds (FIFO
      within a build), per-attribute timeout (default 3h) + max-silent-time
      (default 20min), `--show-trace` option, cached-result detection, one
      automatic retry on transient errors (suppressed when cancel requested),
      eval gc-roots held during build, frame-chunked zstd log capture (single
      reader fan-out for tailing, compression off the event loop) with
      configurable size cap (default 64MB, keep head+tail)
- [x] 2.3 Port failed-build cache skip logic (from `build_trigger.py`
      `_process_build_for_scheduling`/`_update_failed_builds_cache` and
      `nix_error.py:CachedFailureStep`; storage from `db/failed_builds.py`) onto
      new schema; opt-in toggle, default off (current `cacheFailedBuilds`)
- [x] 2.4 Port gcroot management from `nix_gcroot.py`
- [x] 2.4b Port per-branch config (`match_glob`, `register_gcroots`,
      `update_outputs`) and outputs-path symlink updates (depends on 2.4
      gcroots)
- [x] 2.5 Wire effects execution (reuse `buildbot_effects`) per default-branch
      repo config (`effects_on_pull_requests`, `effects_branches`); secret
      resolution incl. org wildcard `forge:owner/*`;
      `effects.extraSandboxPaths`; effects started-flag (never auto-re-run on
      recovery); wiring happens after 3.1
- [x] 2.6 Unit tests: eval fan-out, cached skip, failure aggregation using a
      real local nix store

## 3. CI engine core

- [x] 3.1 Implement build state machine and orchestrator: change event → build
      record → eval → attribute builds → aggregate result; attribute completion
      as single transactional write; re-aggregation serialized per build (row
      lock) with monotonic status generation; build reuse keyed on post-merge
      tree hash across contexts
- [x] 3.2 Implement cancellation: supersede in-flight builds per branch/PR
      (compare commits/tree, not arrival order; shared build cancelled only when
      superseded in all contexts; delivery-GUID dedupe lives in webhook
      ingestion 4.2, not here), cancel on PR close/merge, process-group kill;
      cancel flag suppresses retries and dependency-failure propagation
      (dependents get "skipped (superseded)"); port `build_canceller.py`
      semantics
- [x] 3.2b Honor `[skip ci]`/`[ci skip]` commit message markers
- [x] 3.3 Port scheduled Hercules effects (`onSchedule`) from `scheduled.py` +
      `nix_eval.py:ScheduledEffectsEvaluateCommand`: discover via
      `buildbot-effects list-schedules` on default-branch builds, persist
      cron-like `when` specs (deterministic pseudo-random defaults), execute via
      `run-scheduled`
- [x] 3.4 Implement crash recovery (resume: skip DB-terminal attributes,
      re-check pending via `nix path-info`, never auto-re-run effects),
      DB-outage policy (buffered state writes, health endpoint; webhook
      fail-fast lands in 4.2), log retention cleanup, stale workdir cleanup
- [x] 3.5 Unit tests: cancellation-during-retry race, out-of-order webhook
      supersede, shared-context cancellation, restart recovery (no duplicate
      effects/statuses), tree-hash dedup

## 4. Forge integration

- [x] 4.1 Port project discovery (GitHub App only — drop token mode, Gitea) with
      per-fetch short-lived installation tokens for private GitHub repos, from
      `github/`, `github_projects.py`, `gitea_projects.py` to httpx-based async
      clients; DB-backed project enablement flag keyed by stable forge repo ID
      (rename-safe) + one-shot legacy topic import (first startup with empty
      projects table) + repo/user allowlist discovery filters
- [x] 4.2 Implement webhook endpoints (`/webhooks/{github,gitea}` plus legacy
      `/change_hook/*` aliases) with signature validation (App secret for
      GitHub, per-repo secrets for Gitea; identical validation on legacy
      aliases), delivery-GUID dedupe, fail-fast 500 on DB outage (App
      redelivers), emitting change events; merge-queue branch patterns
      (`gh-readonly-queue/*`, `gitea-mq/*`, `staging`, `trying`) always build;
      all PRs build (no trust gating — matches current behavior)
- [x] 4.2b Gitea webhook auto-registration with per-repo secrets on enable;
      legacy buildbot webhook removal only when URL matches own configured
      webhook base URL; `webhookBaseUrl` option (webhook URL may differ from UI
      URL)
- [x] 4.2c Startup reconciliation of unbuilt default-branch and open-PR heads
      (needs forge clients from 4.1; runs after crash recovery 3.4, supersede
      resolves duplicates)
- [x] 4.3 Port pull-based polling mode from `pull_based/` (poll interval, poll
      spread, per-repo SSH keys/known-hosts); Gitea SSH fetch option
      (`gitea.sshPrivateKeyFile`/`sshKnownHostsFile`) for the webhook backend
      too
- [x] 4.4 Implement commit status reporter: combined per-phase statuses
      (`nix-eval` with warning count, `nix-build`, `nix-effects`) plus
      per-attribute failure statuses
      (`nix-build <type>:<owner>/<repo>#checks.<attr>`) capped by
      failedBuildReportLimit (default 47); target URLs generated from the
      decided URL scheme (no dependency on frontend 5.x); failed-status
      persistence per revision and success-flip on rebuild incl. force-running
      already-built attrs (`db/failed_status.py` storage, behavior from
      `build_trigger.py:_maybe_send_notification`/`_schedule_ready_jobs`/`_record_cancelled_statuses`);
      monotonic generation to drop stale posts; separate rate budget from ACL
      checks
- [x] 4.5 Tests with recorded/fake forge API responses

## 5. Web frontend

- [x] 5.1 FastAPI app (classless CSS, e.g. Pico): project list,
      paginated/filterable build lists (status, branch), build detail
      (per-attribute) HTML views with Jinja2 + htmx; per-project sequential
      build numbers in URLs; prev/next navigation between builds and
      per-attribute history across builds (buildbot-style); homepage
      recent-builds feed + project sidebar; substring search; attributes grouped
      by system with failed-first ordering, client-side filter, inline error
      excerpts (eval error text from nix-eval-jobs; port the three synthetic
      failed-attribute kinds from `nix_error.py`: failed eval,
      dependency-failed, cached failure with first-failure link; build-log
      excerpt extraction is new code); live page updates while builds run;
      system-preference dark mode; commit/PR/repo forge links; relative
      timestamps with hover absolute; global queue page with FIFO positions
- [x] 5.2 Live log streaming endpoint (SSE), log viewer page (ANSI colors, line
      permalinks, follow-tail toggle), raw log download (text/.zst)
- [x] 5.3 Auth: GitHub OAuth, Gitea OAuth, generic OIDC login, signed session
      cookies (configurable lifetime, default 30d; signing key in StateDirectory
      with two-key rotation, overridable via LoadCredential); CSRF protection on
      all state-changing endpoints; port authz rules from `authz.py` and
      `oidc.py`; provider-qualified admin/allowlist entries;
      `allowUnauthenticatedControl` option; PR-author rule: author (matched by
      forge identity) may restart/cancel builds of their own PR (PR author
      stored on build record)
- [x] 5.3b Private-project visibility: track repo visibility in project records;
      per-user accessible-repo-set fetch cached with configurable TTL (default
      1h, negatives cached, dropped on session revocation); enforce on all HTML,
      JSON API, log (viewer/SSE/raw), and live-update endpoints (`/metrics`
      unauthenticated but free of private repo names); tests for anonymous and
      unauthorized access incl. SSE
- [x] 5.4 Build control endpoints (restart build, restart single attribute
      without re-eval, rebuild-all-failed bulk action, cancel) gated by authz;
      admin project enable/disable toggle in UI
- [x] 5.4b Personal API tokens: generate/revoke in UI, shown once, stored hashed
      (constant-time compare), optional expiry, immediate revocation; token auth
      for read + control API
- [x] 5.4c Prometheus /metrics endpoint (build counts, durations, queue depth)
- [x] 5.5 JSON API mirroring views with OpenAPI docs
- [x] 5.6 Frontend/API tests (httpx test client)

## 6. NixOS module & packaging

- [x] 6.1 Package the new service in `packages/`; flake app/entry point; TCP +
      optional unix socket listeners
- [x] 6.2 Write single-service NixOS module: StateDirectory, hardening, operator
      secrets via LoadCredential, Postgres provisioning option (unix socket peer
      auth; remote DB w/ password), optional nginx+ACME vhost incl. optional
      outputs-dir serving (autoindex), `RestrictNamespaces` tuned for bwrap,
      health-check wiring; add `mkRemovedOptionModule` stubs for master/worker
      options (incl. `authBackend="httpbasicauth"`, `httpBasicAuthPasswordFile`,
      `accessMode`, `workersFile`, `localWorkers`, GitHub token mode)
- [x] 6.3 NixOS VM integration test: fake forge → webhook → eval → build →
      commit status assertion (GitHub and Gitea modes)
- [x] 6.4 Update examples/ and docs/ for new deployment

## 7. Removal & cleanup

- [x] 7.1 Delete buildbot plugin code (steps, reporters, schedulers,
      `worker.py`, `local_worker.py`, `db/` buildbot connectors),
      `nix/master.nix`, `nix/worker.nix`, `patches/`
- [x] 7.2 Drop buildbot/buildbot-worker/Twisted from dependencies and nix
      expressions
- [x] 7.3 Run flake-fmt, ruff, mypy; fix all findings; ensure flake checks build
- [x] 7.4 Update README and CHANGELOG with migration notes
