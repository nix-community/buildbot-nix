# Feature-parity audit: replace-buildbot-engine

Audit of the current buildbot-nix implementation against the proposal, design,
specs, and tasks of this change. Intentional drops listed in the change
(oauth2-proxy mode, GitHub legacy token auth, master/worker split, Linux only,
Postgres only, badge endpoint, notifications, admin CLI, topic-filter demotion,
combined per-phase statuses) are not flagged as gaps.

## 1. Feature coverage table

| #  | Current feature                                                                                                                                                                                                                                                                             | Implemented in                                                                           | Plan coverage                                                                                                                                                                                                                                                                                                  |
| -- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1  | nix-eval-jobs evaluation (workers, max-memory, gc-roots dir, `--check-cache-status`, `--force-recurse`, eval-cache off, accept-flake-config)                                                                                                                                                | `nix_eval.py`                                                                            | Covered                                                                                                                                                                                                                                                                                                        |
| 2  | Dynamic eval worker/memory sizing (incl. ZFS ARC heuristic)                                                                                                                                                                                                                                 | `packages/master.cfg.py`                                                                 | Covered (task 2.1)                                                                                                                                                                                                                                                                                             |
| 3  | Per-repo config `buildbot-nix.toml`: `attribute`                                                                                                                                                                                                                                            | `repo_config/__init__.py`                                                                | Covered                                                                                                                                                                                                                                                                                                        |
| 4  | Per-repo config: `flake_dir` (with traversal validation), `lock_file` (`--reference-lock-file`)                                                                                                                                                                                             | `repo_config/__init__.py`, `nix_eval.py`                                                 | **Partially** — spec only mentions "attribute selection"; `flake_dir`/`lock_file` never named (gap G1)                                                                                                                                                                                                         |
| 5  | Per-repo config: `effects_on_pull_requests`, `effects_branches` (glob) read from default branch                                                                                                                                                                                             | `repo_config/__init__.py`, `nix_eval.py:BuildbotEffectsCommand._should_run_effects`      | **Missing** — design explicitly says "Effects: default branch only (current behavior), never PRs", which is factually wrong (gap G2)                                                                                                                                                                           |
| 6  | Extra-branch builds via repo-level `branches` list / server-side `branches` glob config (`match_glob`, `register_gcroots`, `update_outputs`)                                                                                                                                                | `models.py:BranchConfigDict`, `project_config.py`                                        | Covered (task 2.1d)                                                                                                                                                                                                                                                                                            |
| 7  | Merge-queue builds: branches `gh-readonly-queue/*`, `gitea-mq/*`, `staging`, `trying` always built                                                                                                                                                                                          | `project_config.py` (`-merge-queue` scheduler)                                           | **Missing** (gap G3)                                                                                                                                                                                                                                                                                           |
| 8  | PR builds with local merge of head into base                                                                                                                                                                                                                                                | `nix_eval.py:GitLocalPrMerge`                                                            | Covered                                                                                                                                                                                                                                                                                                        |
| 9  | Dependency-aware topological scheduling, dependency-failure propagation, supported-systems filter                                                                                                                                                                                           | `build_trigger.py`                                                                       | Covered (task 2.1c)                                                                                                                                                                                                                                                                                            |
| 10 | Failed-build cache (opt-in `cacheFailedBuilds`, default off), rebuild clears cache entry, link to first failing build                                                                                                                                                                       | `db/failed_builds.py`, `build_trigger.py`, `nix_error.py:CachedFailureStep`              | **Partially** — spec makes caching unconditional ("SHALL record"); current behavior is opt-in, default off (gap G4)                                                                                                                                                                                            |
| 11 | Per-attribute failure statuses on the forge (`nix-build <type>:<owner>/<repo>#checks.<attr>`), capped by `failedBuildReportLimit`, persisted in `failed_status` table, cleared by forced re-runs of already-built attrs                                                                     | `build_trigger.py`, `db/failed_status.py`                                                | **Partially / contradictory** — forge-integration spec says "Per-attribute detail is shown in the web UI, not as individual forge statuses" while also requiring the failed-build report limit and failed-status clearing, which only exist _because_ per-attribute failure statuses are posted today (gap G5) |
| 12 | Combined per-phase statuses `nix-eval`/`nix-build`/`nix-effects`                                                                                                                                                                                                                            | `nix_status_generator.py`                                                                | Covered                                                                                                                                                                                                                                                                                                        |
| 13 | Eval warnings: extraction from stderr, WARNINGS result, formatted warning log, warning count appended to forge status text                                                                                                                                                                  | `nix_eval.py:_process_warnings`, `nix_status_generator.py:NixEvalWarningsFormatter`      | **Missing** (gap G6)                                                                                                                                                                                                                                                                                           |
| 14 | `--show-trace` for eval and build (`showTrace`)                                                                                                                                                                                                                                             | `nix_eval.py`, `nix_build.py`, `master.nix`                                              | **Missing** option (gap G7)                                                                                                                                                                                                                                                                                    |
| 15 | `--max-silent-time` (`buildMaxSilentTime`, default 20 min) in addition to overall timeout                                                                                                                                                                                                   | `nix_build.py`, `master.nix`                                                             | **Missing** — plan only has the 2h per-attribute timeout; also silently changes the timeout default from 3h to 2h (gap G8)                                                                                                                                                                                     |
| 16 | Skipped (already-built) attributes: gcroot registration + outputs update without rebuilding                                                                                                                                                                                                 | `nix_build.py:ProcessSkippedBuilds`                                                      | **Partially** — "Cached derivation → succeeded without rebuilding" covered, but gcroot/outputs handling for skipped attrs not stated (gap G9)                                                                                                                                                                  |
| 17 | GC root per project/attr, rotation, stale `drvs/` cleanup via tmpfiles (7d)                                                                                                                                                                                                                 | `nix_gcroot.py`, `master.nix` tmpfiles                                                   | Covered (eval gc-roots release + stale cleanup in tasks 3.4)                                                                                                                                                                                                                                                   |
| 18 | Outputs path symlinks + nginx `autoindex` serving of `outputsPath` over HTTP                                                                                                                                                                                                                | `nix_build.py:UpdateBuildOutput`, `master.nix` (nginx `location /` alias)                | **Partially** — symlink updates covered; HTTP serving of the outputs directory not mentioned (gap G10)                                                                                                                                                                                                         |
| 19 | Post-build steps (command, env, Interpolate, warn-only), cachix + niks3 modules                                                                                                                                                                                                             | `models.py:PostBuildStep`, `nixosModules/cachix.nix`, `niks3.nix`                        | Covered (task 2.1b)                                                                                                                                                                                                                                                                                            |
| 20 | hercules-ci effects after build (`buildbot-effects list`/`run`), per-repo secrets incl. org wildcard `forge:owner/*`, `effects.extraSandboxPaths`                                                                                                                                           | `buildbot_effects.py`, `project_config.py:resolve_effects_secret`, `master.nix`          | **Partially** — effects covered; org-wildcard secret resolution and `extraSandboxPaths` option not mentioned (gap G11)                                                                                                                                                                                         |
| 21 | **Scheduled effects** (Hercules `onSchedule`): cron-like `when` spec (minute/hour/dayOfWeek/dayOfMonth, deterministic random defaults), discovered from flake via `buildbot-effects list-schedules` on default-branch push, cached, scheduler reconfig on change, `run-scheduled` execution | `scheduled.py`, `nix_eval.py:ScheduledEffectsEvaluateCommand`, `buildbot_effects/cli.py` | **Missing / mischaracterized** — plan reduces this to "periodic builds of a project's default branch at a configurable interval", which is a different feature (gap G12)                                                                                                                                       |
| 22 | Cancellation of superseded builds per branch/PR                                                                                                                                                                                                                                             | `build_canceller.py`                                                                     | Covered                                                                                                                                                                                                                                                                                                        |
| 23 | Cancelled scheduled attrs recorded as failed statuses so a later build re-reports them                                                                                                                                                                                                      | `build_trigger.py:interrupt`                                                             | Part of G5                                                                                                                                                                                                                                                                                                     |
| 24 | GitHub App: installations, JWT/installation tokens, project discovery, per-repo webhook creation                                                                                                                                                                                            | `github/`, `github_projects.py`                                                          | Covered (4.1/4.2; webhook model intentionally changed to App subscription)                                                                                                                                                                                                                                     |
| 25 | Gitea: discovery, webhook auto-registration, status push                                                                                                                                                                                                                                    | `gitea_projects.py`                                                                      | Covered                                                                                                                                                                                                                                                                                                        |
| 26 | Gitea SSH clone (`gitea.sshPrivateKeyFile`/`sshKnownHostsFile`) for fetching all Gitea repos                                                                                                                                                                                                | `gitea_projects.py:get_project_url`, `models.py:GiteaConfig`                             | **Partially** — design says "Gitea keeps token/netrc"; SSH-based fetch for the Gitea webhook backend not mentioned (only pull-based SSH keys are, task 4.3) (gap G13)                                                                                                                                          |
| 27 | Repo filters: `repoAllowlist`, `userAllowlist` (in addition to `topic`)                                                                                                                                                                                                                     | `common.py:filter_repos`, `models.py:RepoFilters`                                        | **Missing** — topic demotion is decided, but the allowlists are separate filters with no stated replacement (gap G14)                                                                                                                                                                                          |
| 28 | Project list refresh: on demand (force "Update projects") + bidaily periodic + on startup when cache missing                                                                                                                                                                                | `__init__.py:PeriodicWithStartup`                                                        | Covered ("periodically and on demand")                                                                                                                                                                                                                                                                         |
| 29 | Pull-based polling (interval, spread, per-repo SSH keys) with no status reporting (NullReporter)                                                                                                                                                                                            | `pull_based/`                                                                            | Covered (task 4.3)                                                                                                                                                                                                                                                                                             |
| 30 | Auth backends: GitHub OAuth, Gitea OAuth, OIDC                                                                                                                                                                                                                                              | `oidc.py`, backends' `create_auth`                                                       | Covered                                                                                                                                                                                                                                                                                                        |
| 31 | Auth backend `httpbasicauth` (oauth2-proxy front) + `accessMode.fullyPrivate`                                                                                                                                                                                                               | `oauth2_proxy_auth.py`, `master.nix`                                                     | Intentional drop — fine, but module should ship removed-option stubs for `authBackend = "httpbasicauth"`, `httpBasicAuthPasswordFile`, `accessMode` (tasks 6.2 covers stubs generally)                                                                                                                         |
| 32 | `allowUnauthenticatedControl` (open control actions, for VPN/local setups)                                                                                                                                                                                                                  | `models.py`, `authz.py:setup_authz`                                                      | **Missing** — not in plan and not in the intentional-drop list (gap G15)                                                                                                                                                                                                                                       |
| 33 | Org-membership authz: org members may rebuild/stop builds of their org's projects                                                                                                                                                                                                           | `authz.py`                                                                               | Covered (D5 / web-frontend spec)                                                                                                                                                                                                                                                                               |
| 34 | Admin role                                                                                                                                                                                                                                                                                  | `authz.py`, `admins` option                                                              | Covered                                                                                                                                                                                                                                                                                                        |
| 35 | `webhookBaseUrl` (register webhooks under a different base URL than the UI)                                                                                                                                                                                                                 | `master.nix`, backends                                                                   | **Missing** (gap G16)                                                                                                                                                                                                                                                                                          |
| 36 | Manual force/eval trigger per project (ForceScheduler)                                                                                                                                                                                                                                      | `project_config.py`                                                                      | Intentional drop (decision: event-driven only)                                                                                                                                                                                                                                                                 |
| 37 | Restart-recovery, retention, metrics, API tokens, queue page, search, live updates                                                                                                                                                                                                          | — (buildbot built-ins / new)                                                             | New features in plan, no parity concern                                                                                                                                                                                                                                                                        |
| 38 | Fork-PR trust gating                                                                                                                                                                                                                                                                        | **does not exist** (webhooks subscribe to `push`+`pull_request`, all PRs build)          | Plan claims to "keep current trust rules" — see §4                                                                                                                                                                                                                                                             |
| 39 | `[skip ci]` markers                                                                                                                                                                                                                                                                         | **does not exist** today                                                                 | Plan addition, fine                                                                                                                                                                                                                                                                                            |
| 40 | Eval concurrency: one eval at a time globally (`MasterLock("nix-eval")`)                                                                                                                                                                                                                    | `__init__.py`, `nix_eval.py`                                                             | **Partially** — plan fixes build concurrency (global FIFO) but does not state the eval-level serialization that protects memory today (gap G17)                                                                                                                                                                |

## 2. Gaps in detail

### G1: `flake_dir` / `lock_file` repo config

- Code: `repo_config/__init__.py` (`BranchConfig.flake_dir`, `lock_file`,
  `_validate_flake_dir`), used in `nix_eval.py`
  (`--flake {flake_dir}#{attribute}`, `--reference-lock-file`).
- Lost: repos with the flake in a subdirectory or an alternative lock file stop
  building.
- Suggest: nix-build-pipeline spec — extend the evaluation requirement to name
  `flake_dir` (traversal-safe) and `lock_file`; task 2.1 mention.

### G2: Effects on PRs and extra branches

- Code: `BuildbotEffectsCommand._should_run_effects` — runs effects on default
  branch, on PRs when default branch's `buildbot-nix.toml` sets
  `effects_on_pull_requests`, and on branches matching `effects_branches` globs.
- Lost: repos relying on PR/branch effects (e.g. preview deployments) lose them.
- Suggest: fix the design decision ("default branch only" is not current
  behavior); nix-build-pipeline spec: effects run per the default branch's repo
  config (`effects_on_pull_requests`, `effects_branches`), defaulting to
  default-branch-only; add to task 2.5.

### G3: Merge-queue branches

- Code: `project_config.py` `-merge-queue` scheduler, branch regex
  `(gh-readonly-queue/.*|gitea-mq/.*|staging|trying)`, unconditional.
- Lost: GitHub merge queue / bors-style workflows silently stop being built —
  breaks merge queues that require the statuses.
- Suggest: forge-integration spec "Webhook-driven change events": add
  merge-queue branch patterns to the always-build set; task 3.1/4.2 note.

### G4: Failed-build caching is opt-in today

- Code: `cacheFailedBuilds` (default false) gates all `failed_builds` DB use in
  `build_trigger.py`.
- Lost: spec as written changes default behavior (cached failures where
  operators expect retries).
- Suggest: ci-engine spec: make the failed-build cache configurable, default
  matching current (off), or explicitly decide to flip the default.

### G5: Per-attribute failure statuses on the forge

- Code: `build_trigger.py` (`status_name = f"nix-build {name}"`,
  `_maybe_send_notification`, `_failed_statuses`, `_record_cancelled_statuses`,
  forced re-run of already-built attrs "to update status"),
  `db/failed_status.py`.
- Current behavior: besides the 3 combined contexts, each _failing_ attribute
  (failed eval, cached failure, dependency failure, build failure, cancellation)
  gets its own commit status, capped at `failedBuildReportLimit` (default 47);
  failed statuses persist per revision across builds and are overwritten by
  success on rebuild — even for attributes that would otherwise be skipped as
  already built.
- The forge-integration spec keeps the limit and the clearing semantics but
  simultaneously states per-attribute statuses are _not_ posted. As written, the
  limit and `failed_status` clearing have nothing to apply to.
- Suggest: pick one: (a) keep per-attribute failure statuses (true parity;
  branch-protection rules on attr contexts keep working) and reword the spec, or
  (b) declare them an intentional drop and remove the
  report-limit/failed-status-clearing language (then `db/failed_status.py`
  semantics are mostly moot). Update design bullet "failed-build report limit"
  accordingly.

### G6: Evaluation warnings

- Code: `nix_eval.py:_process_warnings`/`_format_warnings` (parses
  `evaluation warning:` blocks from stderr), WARNINGS build result, HTML
  warnings log with repro command, warning count in step summary;
  `nix_status_generator.py:NixEvalWarningsFormatter` appends "(N warnings)" to
  the `nix-eval` status description.
- Lost: warning surfacing in UI and status text.
- Suggest: nix-build-pipeline spec: eval warnings are extracted and shown on the
  build page; optionally append count to the `nix-eval` status; add to task
  2.1/5.1.

### G7: `showTrace`

- Code: `--show-trace` passed to nix-eval-jobs and `nix build` when
  `showTrace = true`.
- Suggest: keep as engine/module option; task 2.1/2.2.

### G8: `buildMaxSilentTime` and timeout default

- Code: `nix build --max-silent-time` (default 1200 s); overall step timeout
  default 3 h (`buildTimeout`).
- Lost: stuck builds (no output) currently die after 20 min instead of occupying
  a slot for hours; plan also changes the overall default from 3h to 2h without
  saying so.
- Suggest: nix-build-pipeline spec: add configurable max-silent-time (default 20
  min); document the timeout default change or keep 3 h.

### G9: Skipped builds still gcrooted / outputs updated

- Code: `nix_build.py:ProcessSkippedBuilds` (run from eval builder),
  `*-skipped_out_path` properties.
- Lost: without it, a no-op build lets the previous gcroot rotation lapse for
  unchanged attrs only if implementation forgets; outputs dir would go stale.
- Suggest: nix-build-pipeline "GC-root protection" requirement: state gcroots
  and outputs are also refreshed for attributes skipped as already built.

### G10: HTTP serving of `outputsPath`

- Code: `master.nix` nginx vhost `location /` → `alias cfg.outputsPath` with
  `autoindex` (when `enableNginx && outputsPath != null`).
- Lost: deployment tooling that fetches outputs over HTTP.
- Suggest: nixos-module spec: optional static serving of the outputs directory
  (nginx location or app route); task 6.2.

### G11: Effects secret wildcard + extra sandbox paths

- Code: `project_config.py:resolve_effects_secret` (`forge:owner/repo` exact,
  then `forge:owner/*`), `effects.extraSandboxPaths` module option →
  `--extra-sandbox-path`.
- Suggest: nix-build-pipeline effects requirement: keep per-repo and
  org-wildcard secret mapping and extra sandbox paths; tasks 2.5.

### G12: Hercules scheduled effects (`onSchedule`)

- Code: `scheduled.py` (Nightly schedulers from `ScheduleWhen` cron specs,
  deterministic pseudo-random minute/hour defaults to avoid thundering herd),
  `nix_eval.py:ScheduledEffectsEvaluateCommand` (discovers schedules via
  `buildbot-effects list-schedules` on default-branch push, caches under
  `/var/lib/buildbot/scheduled-effects-cache`, triggers reconfig on change),
  `buildbot_effects/cli.py run-scheduled`,
  `models.py:ScheduleWhen/ScheduledEffectConfig`.
- Plan: ci-engine "Scheduled builds" describes interval-based default-branch
  _builds_ — a feature that does not exist today; the actual feature
  (cron-scheduled _effects_ defined in the flake) is absent from all specs.
- Lost: repos using `herculesCI.onSchedule` (e.g. nightly deploy/update effects)
  lose them entirely.
- Suggest: replace/extend the ci-engine "Scheduled builds" requirement: parse
  `onSchedule` from the default branch on each successful default-branch build,
  persist schedules, run the named effects at the cron times (UTC, deterministic
  defaults), statuses/logs like other effects. Rewrite task 3.3.

### G13: Gitea SSH fetch

- Code: `GiteaProject.get_project_url` returns `ssh_url` when
  `gitea.sshPrivateKeyFile` is set; otherwise token-in-URL HTTPS.
- Suggest: forge-integration "Private repository fetch credentials": Gitea
  supports token _or_ SSH key/known-hosts for fetches; nixos-module keeps the
  options.

### G14: `repoAllowlist` / `userAllowlist`

- Code: `common.py:filter_repos`, `models.py:RepoFilters`, module options under
  `github.*`/`gitea.*`.
- Plan demotes only the _topic_ filter; allowlists are a different mechanism
  (restrict which discovered repos can ever be enabled — a security boundary on
  big instances).
- Suggest: either fold into the one-shot legacy import as well (explicit
  decision) or keep as a discovery-time filter option; clarify in
  forge-integration spec + task 4.1.

### G15: `allowUnauthenticatedControl`

- Code: `authz.py:setup_authz` early-return allowing all control endpoints.
- Plan: web-frontend spec requires login for control everywhere.
- Suggest: decide explicitly: keep an equivalent option (useful for VPN'd/local
  dev instances, and `packages/master.cfg.py`-style local development) or list
  as intentional drop with a removed-option stub.

### G16: `webhookBaseUrl`

- Code: `master.nix` option, passed to backends as the URL under which webhooks
  are registered (split webhook host from UI host).
- Suggest: nixos-module/forge-integration: keep a configurable external webhook
  base URL (matters for Gitea auto-registration and GitHub App setup docs).

### G17: Eval serialization

- Code: `util.MasterLock("nix-eval")` — exactly one nix-eval-jobs run at a time
  across all projects (memory protection).
- Plan only fixes build-job concurrency. Eval concurrency default should be
  stated (1, or N with derived memory budget) so multi-project pushes don't OOM
  the host.
- Suggest: nix-build-pipeline: add configurable max concurrent evaluations
  (default 1).

## 3. Config options with no planned equivalent

`models.py` / nixosModules options not addressed by the plan (beyond intentional
drops; items above referenced where applicable):

| Option                                                                                                                           | Status                                                                                                          |
| -------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `showTrace` / `show_trace_on_failure`                                                                                            | G7                                                                                                              |
| `buildMaxSilentTime`                                                                                                             | G8                                                                                                              |
| `buildTimeout` default 3h (plan: 2h)                                                                                             | G8                                                                                                              |
| `cacheFailedBuilds` toggle                                                                                                       | G4                                                                                                              |
| `allowUnauthenticatedControl`                                                                                                    | G15                                                                                                             |
| `webhookBaseUrl`                                                                                                                 | G16                                                                                                             |
| `github.repoAllowlist` / `github.userAllowlist`, `gitea.repoAllowlist` / `gitea.userAllowlist`                                   | G14                                                                                                             |
| `gitea.sshPrivateKeyFile` / `gitea.sshKnownHostsFile` (webhook backend, not pull-based)                                          | G13                                                                                                             |
| `effects.extraSandboxPaths`                                                                                                      | G11                                                                                                             |
| `effects.perRepoSecretFiles` org wildcard (`forge:owner/*`)                                                                      | G11                                                                                                             |
| `outputsPath` HTTP serving via nginx                                                                                             | G10                                                                                                             |
| `useHTTPS` (URL scheme behind external proxy)                                                                                    | not mentioned; presumably subsumed by a `url` option — should be stated in module spec                          |
| `authBackend = "none"` (anonymous read-only, no login at all)                                                                    | plan always configures login providers; an explicit "no auth provider" mode for read-only instances is unstated |
| `gcroots_user`, `workersFile`, `local_workers`, `nixEvalJobs.package` (worker module), `accessMode`, `httpBasicAuthPasswordFile` | obsolete with single-service / intentional drops — need `mkRemovedOptionModule` stubs (task 6.2)                |

Covered fine: `dbUrl` (→ Postgres provisioning), `admins`, `buildSystems`,
`evalMaxMemorySize`, `evalWorkerCount`, `domain`/`url`, `enableNginx`,
`outputsPath` (writing), `failedBuildReportLimit` (pending G5 resolution),
`branches.*`, `postBuildSteps`, cachix/niks3 modules, OIDC options, pull-based
options, GitHub App options, Gitea token/webhook/oauth options.

## 4. Incorrect code references in tasks.md

1. **3.3** "Implement scheduled (periodic) default-branch builds, port from
   `scheduled.py`" — `scheduled.py` does not implement periodic default-branch
   builds; it implements Hercules `onSchedule` _effects_ (cron specs from the
   flake, discovered via `buildbot-effects list-schedules`). See G12.
2. **4.3b** "Port fork-PR trust rules (org members/trusted users; approval
   gating) from current implementation" — no such code exists. Webhooks
   subscribe to `push` + `pull_request` and every PR is built
   (`project_config.py` `-prs` scheduler, no author gating; no
   "trusted"/"approval" logic anywhere in `buildbot_nix/`). This is a new
   feature, not a port; the design decision "keep current trust rules" is based
   on a false premise (current rule = build all PRs).
3. **5.1 / design** "inline error excerpts (port `nix_error.py`)" —
   `nix_error.py` contains the dummy _builders_ for failed-eval /
   dependency-failed / cached-failure display (using the eval `error` property
   and `first_failure_url`), not a log-excerpt extractor. The eval error text
   does come from nix-eval-jobs via `models.NixEvalJobError.error`; an
   excerpt-from-build-log extractor would be new code. Worth porting from
   `nix_error.py` regardless: the three synthetic attribute result kinds (failed
   eval with error text, dependency-failed with culprit attr, cached failure
   with first-failure link).
4. **2.3** "Port failed-build cache skip logic (from `db/failed_builds.py` +
   `nix_build.py`)" — the skip logic lives in `build_trigger.py`
   (`_process_build_for_scheduling`, `_update_failed_builds_cache`) plus
   `nix_error.py:CachedFailureStep`; `nix_build.py` contains none of it.
5. **4.4** references `db/failed_status.py` semantics — correct file, but note
   the semantics only matter if per-attribute failure statuses are kept (G5);
   also the status forced-rerun behavior lives in
   `build_trigger.py:_schedule_ready_jobs`.
6. **2.1** "port dynamic eval worker/memory sizing from
   `packages/master.cfg.py`" — correct, but note the macOS path there is dead
   under the Linux-only decision; only `get_memory_info_linux` (incl. ZFS ARC
   handling) needs porting.

## 5. Verdict

The plan is unusually thorough; most user-visible behavior is covered, including
many small ones (poll spread, per-branch globs, outputs symlinks, niks3/cachix).
The substantive risks are:

- **G12 (scheduled `onSchedule` effects)** — a whole feature mischaracterized;
  would be silently lost.
- **G2 (effects on PRs / extra branches)** — design decision contradicts code.
- **G5 (per-attribute failure statuses)** — spec is internally inconsistent;
  needs an explicit keep-or-drop decision.
- **G3 (merge-queue branches)** — breaks merge queues if lost.
- **§4.2 (fork-PR trust rules)** — plan promises gating that doesn't exist; must
  be specced as new behavior (with defaults!) or dropped.

Everything else is config-option plumbing that fits naturally into existing
tasks once named in the specs.
