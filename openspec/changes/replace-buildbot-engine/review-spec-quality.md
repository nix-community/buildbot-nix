# Review: replace-buildbot-engine — spec quality & consistency

Scope: internal consistency, ambiguity, format, task coverage, dependency order,
security of the spec'd behavior. Plan files were not modified.

---

## Blockers

### B1. Eval sandbox contradiction: netrc/token mount vs "no secrets"

- **Files:** `design.md` (Resolved Decisions),
  `specs/nix-build-pipeline/spec.md` (Sandboxed builds and evaluation),
  `tasks.md` 2.1
- **Quotes:**
  - design.md: "a per-build netrc/token file (only the credentials needed for
    that repository) is bind-mounted in, never the full credentials directory"
  - spec: "restricting filesystem access to the checkout, the nix store, and the
    nix daemon socket, with no capability to read service secrets"
  - spec scenario: "the evaluation only sees the checkout, nix store, and daemon
    socket"
  - tasks 2.1: "wrap nix-eval-jobs in bwrap sandbox (checkout + nix store +
    daemon socket only, no secrets)"
- **Problem:** Three mutually inconsistent statements. The design mounts a
  per-repo credential into the sandbox (needed for private flake inputs); the
  spec and task claim no secrets are visible. Since the design also keeps
  network access in the sandbox, a malicious flake _can_ exfiltrate the
  bind-mounted token during evaluation. The spec scenario "Evaluation cannot
  read service secrets" is untestable as written because it contradicts the
  intended mount list.
- **Fix:** Pick one model and state it everywhere. Suggested: spec says the
  sandbox mounts checkout, nix store, daemon socket, the nix-eval-jobs gc-roots
  dir, and _optionally_ a per-build, repo-scoped, short-lived credential file;
  explicitly state the residual risk (repo-scoped token exfiltratable via
  network during eval) and that global credentials in `$CREDENTIALS_DIRECTORY`
  are never visible. Update task 2.1 accordingly.

### B2. Webhook secrets: LoadCredential vs service-generated in DB

- **Files:** `specs/nixos-module/spec.md` (Hardened service unit), `design.md`
- **Quotes:**
  - nixos-module: "Secrets (forge credentials, webhook secrets, effects secrets)
    SHALL be passed via systemd `LoadCredential`"
  - design.md: "Webhook secrets: per repository, generated and stored by the
    service."
- **Problem:** Per-repo Gitea webhook secrets are generated at runtime and
  stored in the DB; they cannot be delivered via `LoadCredential`. As written
  the module spec requirement is unimplementable for webhook secrets, and the
  GitHub App webhook secret (a static operator-supplied credential) is the only
  one that fits LoadCredential.
- **Fix:** In nixos-module spec, scope LoadCredential to operator-supplied
  secrets (GitHub App key/webhook secret, Gitea token, OAuth client secrets,
  cookie-signing key, DB password, effects secrets) and state that per-repo
  Gitea webhook secrets are service-generated and stored in the database.

### B3. oauth2-proxy: "keep as optional" vs "dropped" inside design.md

- **File:** `design.md`
- **Quotes:**
  - D5: "oauth2-proxy in front (keep as optional deployment mode, but built-in
    login removes mandatory extra service)"
  - Resolved Decisions: "oauth2-proxy deployment mode dropped; built-in
    OAuth/OIDC replaces it (module assert with migration message)"
- **Problem:** Direct self-contradiction. Implementers won't know whether the
  module must keep working behind oauth2-proxy (which affects whether the app
  must trust forwarded identity headers — a security-relevant design point).
- **Fix:** Update D5's alternatives text to "(rejected; dropped, see Resolved
  Decisions)". If bare-proxy operation is kept, explicitly state the service
  never trusts identity headers from upstream proxies.

---

## Major

### M1. CSRF protection unspecified for cookie-authenticated control actions

- **File:** `specs/web-frontend/spec.md` (Authenticated build control, JSON API)
- **Quote:** "It SHALL allow authenticated users ... to restart and cancel
  builds." / "signed-cookie sessions of approximately 30 days sliding expiry"
- **Problem:** State-changing endpoints (restart, cancel, rebuild-all-failed,
  project enable/disable, token generate/revoke) are authenticated by a
  long-lived session cookie. Nothing requires CSRF defenses. htmx POSTs from a
  session cookie are a classic CSRF target; an attacker page could cancel builds
  or disable projects on behalf of a logged-in admin.
- **Fix:** Add a requirement + scenario: all state-changing endpoints SHALL
  require a CSRF token (or `SameSite=Strict`/`Lax` cookies plus
  Origin/Sec-Fetch-Site checking); cross-origin requests with a valid session
  cookie but no CSRF proof SHALL be rejected.

### M2. Admin/allowlist identity namespace ambiguous across providers

- **Files:** `specs/web-frontend/spec.md`, `design.md`
- **Quote:** "Users in the configured global admin list SHALL be able to control
  any build, view all private projects" / design: "Login providers: GitHub
  OAuth, Gitea OAuth, and generic OIDC"
- **Problem:** With three identity providers, a bare username admin list is
  ambiguous: GitHub user `alice` ≠ Gitea user `alice` ≠ OIDC sub `alice`. As
  spec'd, a Gitea or OIDC account could squat a GitHub admin's username and gain
  global admin (sees all private projects, controls all builds).
- **Fix:** Require admin list and allowlist entries to be provider-qualified
  (e.g. `github:alice`, `gitea:alice`, `oidc:<issuer>/<sub>`), and add a
  scenario: same username on a different provider does not match.

### M3. API token handling underspecified (storage, scope, lifetime)

- **File:** `specs/web-frontend/spec.md` (JSON API)
- **Quote:** "Build control via the API SHALL accept personal API tokens that
  logged-in users can generate and revoke in the UI."
- **Problem:** No requirement that tokens are stored hashed, no expiry/rotation,
  no statement whether tokens grant read access to private projects or only
  control actions ("Build control ... SHALL accept" vs "Token-authenticated
  requests SHALL be subject to the same private-project visibility rules"
  implies read works too — ambiguous). A DB dump leaking plaintext tokens of
  admins would be catastrophic given M2.
- **Fix:** Specify: tokens stored only as hashes, shown once at creation; tokens
  usable for both read and control with the owner's authz; optional expiry;
  revocation effective immediately. Add a scenario for revoked-token read
  access.

### M4. Live log streaming (SSE) authz not explicitly covered by visibility rules

- **File:** `specs/web-frontend/spec.md` (Live log viewing)
- **Quote:** "The frontend SHALL display build and evaluation logs, streaming
  output live for running builds, and SHALL offer raw log download (plain text
  and zstd-compressed) subject to the same visibility rules."
- **Problem:** Grammatically, "subject to the same visibility rules" attaches to
  the raw download clause only. The SSE streaming endpoint and the live-update
  endpoints in "Live page updates" have no explicit visibility requirement or
  scenario. SSE endpoints are easy to forget in authz middleware (different
  routing path). tasks.md 5.3b even says "all HTML/API/log/metrics-free
  endpoints" — garbled text that suggests the author wasn't sure of the endpoint
  inventory.
- **Fix:** Reword: "All log endpoints (HTML viewer, SSE stream, raw download)
  and live-update endpoints SHALL enforce the same visibility rules as the
  underlying build." Add scenario: anonymous SSE request for a private build's
  log is rejected without confirming existence. Fix task 5.3b wording to
  enumerate: HTML, JSON API, SSE/live-update, raw log endpoints (metrics is
  unauthenticated but private-name-free).

### M5. Untrusted fork PR "approval" mechanism unspecified

- **File:** `specs/forge-integration/spec.md` (Webhook-driven change events)
- **Quotes:** "others are not built until approved." / scenario: "no build
  starts until the PR is approved per the configured trust rules"
- **Problem:** "Approved" is undefined: PR review approval? A comment command?
  Label? Member pushing to the PR? This is the security boundary against
  untrusted code; the trigger must be precise and testable. Also note: even "not
  built" PRs still run the local merge if mishandled — spec should state no
  clone/merge/eval happens at all for untrusted PRs until approval.
- **Fix:** Specify the exact approval signal (e.g. "an approving PR review by an
  org member / trusted user"), that approval triggers a build of the approved
  SHA only (new pushes re-require approval), and that no repository content is
  fetched or evaluated before approval.

### M6. Task ordering: section 2 (pipeline) depends on 4.1b (clones/worktrees)

- **File:** `tasks.md`
- **Quotes:** 2.1 "Implement async nix-eval-jobs runner ... evaluate the local
  checkout"; 4.1b "Central per-project persistent clones + per-build git
  worktrees (created for eval ...); local merge of PR head"
- **Problem:** Eval (2.1) and engine orchestration (3.1, which needs "identical
  commit/merge tree" identity) require checkouts/worktrees and the PR merge,
  which are only built in 4.1b. Also 2.5 (effects "after successful
  default-branch builds") needs the orchestrator from 3.1. Tasks cannot be
  executed strictly in listed order.
- **Fix:** Move 4.1b (clone/worktree/merge management) into section 2 (e.g. as
  2.0), or annotate 2.1/2.6 to use a pre-provided checkout path and state the
  dependency. Move 2.5 wiring note to section 3 or mark it "wire-up after 3.1".

### M7. Schema task omits tables required by other tasks/specs

- **Files:** `tasks.md` 1.3, `design.md` D3
- **Quotes:** 1.3 "projects, builds, build_attributes, failed_builds,
  failed_statuses"; D3 adds "logs metadata"; later tasks need users/sessions
  (5.3), API tokens (5.4b), per-repo Gitea webhook secrets (4.2b), access-check
  cache (5.3b)
- **Problem:** D3 and task 1.3 disagree (logs metadata missing from 1.3), and
  neither lists tables that requirements clearly need (api_tokens, webhook
  secrets, project visibility flag from 5.3b). Implementers following 1.3 will
  redo migrations repeatedly.
- **Fix:** Either expand 1.3 to the full table list, or state in 1.3 that the
  migration framework is the deliverable and later sections add their own tables
  via new migration scripts.

---

## Minor

### m1. "Failed-build report limit" requirement has no scenario and no clear meaning

- **File:** `specs/forge-integration/spec.md` (Commit status reporting)
- **Quote:** "The number of reported failure details SHALL respect the
  configured failed-build report limit."
- **Problem:** Statuses are combined per phase (3 contexts), so what are
  "failure details"? Presumably the failed-attribute list embedded in the status
  description. Normative sentence with no scenario, untestable as written; also
  absent from tasks.md (4.4 doesn't mention the limit).
- **Fix:** Define it ("the status description SHALL list at most N failed
  attributes") with a scenario, and add it to task 4.4.

### m2. Partial-failure scenario conflicts with state-machine wording

- **File:** `specs/ci-engine/spec.md`
- **Quotes:** "states: pending → evaluating → building → (succeeded | failed |
  cancelled)" vs scenario: "the overall build is marked failed while other
  attributes still run to completion"
- **Problem:** A build cannot be in terminal state `failed` while still
  `building`. Either there is a derived "failing" indicator while in `building`,
  or the result is aggregated only at the end.
- **Fix:** Reword scenario: "the remaining attributes still run to completion
  and the build's final state is failed" (and/or specify a `result` field
  separate from lifecycle `state`).

### m3. Skip-CI scope ambiguous for PRs and non-head commits

- **File:** `specs/ci-engine/spec.md` (Skip-CI convention)
- **Quote:** "SHALL skip building commits whose message contains `[skip ci]`";
  scenario only covers "a push event's head commit message"
- **Fix:** State explicitly: only the head commit of a push / PR head commit is
  inspected; add a PR scenario (or state PRs are exempt).

### m4. Restart recovery vs startup reconciliation can double-build

- **Files:** `specs/ci-engine/spec.md` (Restart recovery),
  `specs/forge-integration/spec.md` (Startup reconciliation)
- **Quotes:** "builds in non-terminal states are re-queued and run again" vs
  "start a build if that commit has not been built"
- **Problem:** After downtime, a re-queued in-flight build for an old head and a
  reconciliation build for the new head can both run; supersede rules should
  apply but the interaction is unstated. Also "has not been built" — does a
  cancelled/failed build count as "built"?
- **Fix:** State: reconciliation runs after re-queue; a re-queued build for a
  superseded commit is cancelled per the supersede rule; "built" means any build
  record exists for that commit (regardless of result).

### m5. Timeout cached as permanent failure

- **File:** `specs/ci-engine/spec.md` (Failed-build caching)
- **Quote:** "only genuine build failures (including timeouts) are cached"
- **Problem:** Timeouts are frequently transient (loaded remote builder). Once
  cached, the derivation is never retried except by manual restart — likely
  surprising. Internally consistent, but worth a deliberate call-out.
- **Fix:** Either exclude timeouts from the cache or add a design note
  acknowledging the trade-off.

### m6. Access-check cache staleness window unstated as accepted risk

- **Files:** `specs/web-frontend/spec.md`, `design.md`
- **Quote:** "verified via the forge API, cached for approximately one hour"
- **Problem:** A user whose repo access is revoked retains visibility (and,
  combined with authz rules, possibly control) for up to ~1h. "approximately" is
  also untestable.
- **Fix:** Make it a configurable TTL (default 1h), state the staleness window
  is accepted, and require revocation of sessions to drop cached grants
  (logout/ban path).

### m7. Eval gc-roots dir missing from spec's bwrap mount list

- **Files:** `specs/nix-build-pipeline/spec.md`, `design.md`
- **Quotes:** spec: "the checkout, the nix store, and the nix daemon socket";
  design: "only checkout, nix store, gc-roots dir, and nix daemon socket
  mounted"
- **Problem:** nix-eval-jobs must write the gc-roots dir; spec mount list omits
  it (subset of B1, but independently fixable).
- **Fix:** Add gc-roots dir to the spec's mount list.

### m8. GitHub webhook signature source slightly inconsistent in design

- **Files:** `design.md`, `specs/web-frontend/spec.md`
- **Quote:** design: "Webhook secrets: per repository, generated and stored by
  the service." vs web-frontend: "GitHub App webhook secret for GitHub,
  per-repository secrets for Gitea"
- **Fix:** Qualify the design bullet: "per repository for Gitea; GitHub uses the
  App-level webhook secret."

### m9. Identical-commit build reuse vs supersede cancellation interaction

- **File:** `specs/ci-engine/spec.md`
- **Problem:** If a PR head equals a branch head (shared running build) and a
  new push lands on the branch only, is the shared build cancelled (breaking the
  PR's pending status) or kept for the PR context? Unspecified.
- **Fix:** Add a scenario: a shared build is only cancelled when superseded in
  _all_ contexts referencing it.

### m10. Session-cookie signing key provisioning unspecified

- **File:** `specs/nixos-module/spec.md`
- **Problem:** Signed cookies and (likely) CSRF need a signing key. Module spec
  lists forge/webhook/effects secrets but not the cookie key — is it
  operator-supplied via LoadCredential or generated in StateDirectory
  (invalidating sessions on redeploy)?
- **Fix:** Add to the Hardened service unit requirement: cookie-signing key
  source (suggest: auto-generated in StateDirectory, overridable via
  LoadCredential).

---

## Nits

### n1. tasks.md 5.3b garbled wording

- **Quote:** "on all HTML/API/log/metrics-free endpoints"
- **Fix:** "on all HTML, JSON API, log (viewer/SSE/raw), and live-update
  endpoints; `/metrics` stays unauthenticated but must not expose private
  repository names."

### n2. "approximately 30 days" sessions untestable

- **File:** `specs/web-frontend/spec.md`
- **Fix:** "configurable, default 30 days, sliding expiry".

### n3. `MAY` on topic import is fine but underspecified

- **File:** `specs/forge-integration/spec.md`
- **Quote:** "Topic-based filters MAY be used once as a legacy import"
- **Fix:** State trigger ("on first startup with an empty projects table") so
  it's testable; tasks 4.1 calls it "one-shot legacy topic import" — align.

### n4. Raw `.zst` download of a still-running build's log

- **File:** `specs/web-frontend/spec.md`
- **Fix:** State behavior (e.g. raw download serves the log as of request time;
  .zst available only once finalized).

### n5. tasks.md 1.1 "Create new package skeleton `buildbot_nix/ci` (or rename later)"

- **Problem:** "(or rename later)" is a decision deferred into a task; design
  says service name stays `buildbot-nix`. Pick the module path now.

### n6. Format check result (for the record)

All requirements across the five specs use `### Requirement:` with at least one
`#### Scenario:` (4 hashtags) and WHEN/THEN bullets. Normative prose appears
only in requirement bodies before the first scenario, not between scenarios. The
only stray normative sentence lacking scenario coverage is the report-limit
sentence (m1). No floating `should`-style requirements found in the spec files
(vague terms flagged above are "approximately" and "approved").

### n7. Task/requirement coverage result (for the record)

Every spec requirement maps to at least one task (verified). No orphan tasks:
1.x→D1/D3, 2.x→nix-build-pipeline, 3.x→ci-engine, 4.x→forge-integration,
5.x→web-frontend, 6.x→nixos-module, 7.x→proposal "What Changes" / Migration
Plan. Gaps are additive, not orphans: failed-build report limit missing from 4.4
(m1), per-build eval credential file missing from 2.1 (B1), CSRF missing
everywhere (M1), cookie-key provisioning missing from 6.2 (m10), schema task
incomplete (M7).
