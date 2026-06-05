# Architecture Review: replace-buildbot-engine

Reviewer perspective: senior engineer with CI-system experience. Scope:
design.md, proposal.md, specs/*, tasks.md, compared against current
`buildbot_nix/buildbot_nix/` implementation (notably `build_trigger.py`,
`nix_eval.py`, `build_canceller.py`).

Severity scale: **Critical** (will cause data loss / outage / security hole),
**High** (will bite in production within months), **Medium** (operational pain,
fixable later), **Low** (polish).

---

## Risks

### 1. Restart recovery semantics are underspecified and racy — **Critical**

The ci-engine spec says non-terminal builds are "re-queued and run again" after
a crash. That is too coarse:

- A build crash mid-`nix build` leaves the derivation possibly still building in
  the nix daemon (daemon survives service restart). Re-running `nix build` is
  fine (nix dedups), but the design never says how attribute state is
  reconciled: an attribute that _finished_ before the crash but whose DB row was
  never updated (log written, status not committed) will be rebuilt and may
  re-post statuses, re-run post-build steps (cachix upload — idempotent-ish) and
  **re-run effects**, which are explicitly not idempotent (deploys!).
- "Re-queue and run again" + "startup reconciliation builds unbuilt
  default-branch heads" can double-enqueue the same commit if the in-flight
  build at crash time was for the current head.

**Recommendation:** Make attribute completion a single transactional DB write
(status + log metadata + post-build-step record) and make recovery _resume_, not
restart: re-check `nix path-info` for each pending attribute's outputs; skip
attributes already terminal in DB. Gate effects behind an explicit
`effects_started` DB flag and never auto-re-run effects on recovery — require
manual restart. Deduplicate reconciliation against re-queued builds by commit
SHA.

### 2. Per-attribute rebuild vs. combined per-phase statuses is a correctness trap — **High**

Forge statuses are combined per phase (`nix-build` context). When one attribute
is rebuilt after the build finished, the spec says "combined phase statuses are
re-reported" and result re-aggregated. Races:

- Two concurrent per-attribute rebuilds of the same build re-aggregate
  concurrently → lost update on the build row and conflicting status posts
  (success then failure ordering depends on network timing).
- A rebuild of attribute X while a _superseding_ build for a newer commit is
  running can post a status to the old SHA after the new build posted to the new
  SHA — harmless on GitHub (per-SHA) but the `failed_statuses` clearing logic in
  current code (`db/failed_status.py`, `_record_cancelled_statuses` in
  `build_trigger.py`) shows how subtle this already is. The design folds this in
  as one bullet; the current implementation has an entire failed-status
  bookkeeping table, report limits, and "force-run already-built attribute to
  update a previously-failed status" logic (`_schedule_ready_jobs`). None of
  that is in the spec beyond a sentence.

**Recommendation:** Serialize re-aggregation per build (per-build asyncio lock
or `SELECT ... FOR UPDATE` on the build row; status post happens inside/after
that critical section with a monotonic status-generation counter so stale posts
are dropped). Explicitly port the failed-status table semantics and the
"already-built-but-previously-failed → run anyway to flip the status" Gitea
workaround as spec scenarios, not just task 4.4.

### 3. Cancellation vs. retry vs. dependency-propagation interplay unspecified — **High**

Three mechanisms touch a running attribute: supersede-cancel (process-group
kill), one automatic transient retry, and dependency-failure propagation.
Unhandled combinations:

- Cancel arrives during the retry's backoff window → does the retry still fire?
- Process-group kill looks like a transient error (SIGKILL on `nix build`) →
  cancelled attribute gets auto-retried, racing the superseding build.
- An attribute killed by cancellation must NOT propagate dependency-failures to
  dependents, and must not enter the failed-build cache (the spec covers cache
  exclusion but not propagation).
- 64MB log cap + kill: who closes the zstd stream; truncated logs on cancel.

**Recommendation:** Model attribute lifecycle as an explicit state machine with
a `cancel_requested` flag checked before retry scheduling and before dependency
propagation. Cancellation outcome is a distinct terminal state that propagates
"skipped (superseded)" to dependents, never "dependency failed". Write the
cancellation-during-retry race as a unit test (task 3.5 exists; add this case
explicitly).

### 4. Build reuse for identical commits has a wrong/fuzzy condition — **High**

"For PRs this applies only when the locally merged tree is identical (e.g.
fast-forward)." You cannot know the merged tree is identical until you have
_done_ the merge — i.e. after cloning/worktree creation, which is most of the
pre-eval cost anyway. Also: the base branch moves; a PR-head build reused from a
branch push is only valid against the base SHA at merge time. If reuse keys only
on head SHA, you will report stale results when base advances.

**Recommendation:** Key build identity on the **post-merge tree hash**
(`git rev-parse HEAD^{tree}` after local merge), not on the head commit SHA.
That gives correct dedup for free (fast-forward, identical merges, re-pushed
same content) and avoids the special-casing. Statuses still posted per context.

### 5. Single global FIFO queue → starvation and head-of-line blocking — **High**

Plain FIFO with one global concurrency limit means: a monorepo push that
evaluates to 2000 attributes blocks every other project's 3-attribute build for
hours. Today buildbot's per-builder request handling plus collapsing gives some
interleaving. The design explicitly resolves "no per-project caps", which is
fine, but FIFO at _attribute_ granularity vs. _build_ granularity is not stated;
if a whole build's attributes enqueue contiguously, small projects starve.

**Recommendation:** Keep the global cap, but dequeue round-robin across builds
(fair scheduling at build level, FIFO within a build's topological order). This
is ~30 lines and removes the worst starvation mode. Cheap to keep "FIFO
position" in the queue UI as position-within-class. At minimum, document the
starvation trade-off and make scheduling strategy a pluggable internal seam.

### 6. SSE/log-stream fan-out through one event loop and one process — **Medium**

Single uvicorn worker (implied by "single asyncio process" + in-memory build
state) serves HTML, JSON API, webhooks, and N live log streams. zstd
decompression of running logs for each viewer, plus follow-tail, runs on the
same loop that supervises builds. Tens of concurrent log followers on a big
build is fine; a popular public instance with hundreds is not. Also zstd
streaming reads of a file still being appended require seekable-frame or
chunk-per-flush design — not specified.

**Recommendation:** (a) Write logs in independent zstd frames (e.g. frame per N
KB flush) so tailing decompresses only new frames; (b) single in-process pub/sub
per running attribute (one disk reader, fan-out to subscribers) instead of
per-client file tailing; (c) run zstd (de)compression via `asyncio.to_thread`.
Accept single-process for v1 but keep log serving free of shared mutable engine
state so a second read-only uvicorn worker is possible later.

### 7. Forge ACL check amplification — **Medium**

Private-project visibility requires a forge API check per (user, repo), cached
~1h. The homepage feed and search are visibility-filtered: rendering the global
feed for a logged-in user can require checks against _every_ private project on
a cold cache — easily dozens of serial GitHub API calls per page load, plus
rate-limit consumption shared with status posting. Negative results also need
caching (spec only says "cached ~1h" generically).

**Recommendation:** Instead of per-repo checks, fetch the user's accessible-repo
_set_ once per session refresh (GitHub: `GET /user/repos` via the user's OAuth
token, or installation repo list intersected with org membership) and cache that
set for the hour. One API round per user per hour, O(1) per page. Cache
negatives. Use a separate token bucket so ACL checks cannot starve status
posting.

### 8. bwrap eval sandbox: the dangerous surface is the daemon socket and network, not the FS — **Medium**

The sandbox mounts checkout + store + daemon socket, with network for flake
inputs and a per-build netrc bind-mounted. Reality check:

- The nix daemon socket is the real privilege: a compromised evaluator can ask
  the daemon to build arbitrary derivations, add store paths, and (depending on
  trusted-user settings) set substituters/options. FS isolation does not contain
  that. Today this is equally true (eval is a buildbot step on the host), so
  this is not a regression — but the design _advertises_ the sandbox as a
  security boundary; calibrate the claim.
- Open network + netrc in the sandbox means a malicious flake (IFD or
  fetcher-time code paths, evaluator CVEs) can exfiltrate the repo token.
  Per-repo short-lived installation tokens (GitHub) bound this well; Gitea's
  long-lived token in netrc does not — scope it.
- `RestrictNamespaces` relaxation for bwrap re-opens user-namespace kernel
  attack surface in an otherwise hardened unit. Acceptable, but consider
  setuid-less alternative: run eval as a separate dynamic user via `systemd-run`
  with its own sandboxing instead of bwrap, avoiding userns in the main unit.

**Recommendation:** Document that the daemon socket is in-sandbox and what that
implies; ensure the daemon does not treat the service user as trusted-user. For
Gitea, mint or scope per-repo credentials (Gitea supports per-repo deploy keys —
prefer read-only deploy keys over a global token in netrc). Keep the bwrap claim
in docs as "defense in depth", not isolation.

### 9. Postgres unavailability handling unstated — **Medium**

Every webhook, log-metadata write, queue operation, and status transition hits
the DB. Design says nothing about behavior when Postgres is down or restarting
(routine on co-located host during upgrades): are webhooks 500'd (GitHub
redelivers for a while; Gitea's redelivery is weaker), do running builds crash,
does the event loop wedge on pool exhaustion?

**Recommendation:** Specify: webhook handlers return 500 fast on DB failure
(rely on App redelivery; for Gitea, startup reconciliation + polling is the
backstop — note reconciliation currently covers only default-branch heads, so
_missed PR events stay missed_; extend reconciliation to open PRs). Running
builds buffer state transitions in memory with bounded retry to DB; service
exits (systemd restarts) only after the buffer is irrecoverable. Add a
DB-connectivity health endpoint for the module's restart logic.

### 10. Webhook delivery races and ordering — **Medium**

GitHub does not guarantee ordering; a `synchronize` for push N can arrive after
push N+1's. Supersede logic keyed on "newer event" must compare _commits_, not
arrival order; otherwise an out-of-order stale event cancels the fresh build and
starts a build of the old SHA. Also duplicate deliveries (App redelivery) must
be idempotent.

**Recommendation:** Before superseding, verify the incoming head SHA differs
from the running build's and, for ordering, check the forge for the _current_
ref head (one API call) or at least dedupe by delivery GUID + skip if a build
for the same merge-tree already exists (falls out of risk #4's tree-hash
keying). Store processed delivery IDs short-term for idempotency.

### 11. Worktree and clone hygiene — **Medium**

Per-build worktrees from a shared persistent clone: concurrent `git fetch` into
one clone from parallel builds, crash-orphaned worktrees (`git worktree prune`
needed), clone corruption (interrupted fetch) taking down all future builds of a
project, and unbounded object growth in the central clone (every PR head ever
fetched). Task 3.4 mentions "stale workdir cleanup" only.

**Recommendation:** Per-project asyncio lock around fetch; `git worktree prune`

- orphan-dir sweep at startup and periodically; on any git plumbing failure in
  the shared clone, nuke and re-clone automatically (clones are cache, not
  state); periodic `git gc`/prune of unreachable PR objects.

### 12. Eval worker OOM behavior — **Medium**

`MemoryInfo` sizing is ported, good — but the design doesn't say what happens
when nix-eval-jobs workers are OOM-killed anyway (they are, regularly, on big
nixpkgs-ish evals). Is a killed eval a transient (retry once) or permanent
failure? Retrying an OOM at the same worker count just OOMs again, and the retry
doubles eval load on the host.

**Recommendation:** Treat eval OOM as permanent for that build with a clear
"evaluation ran out of memory; lower worker count or add RAM" message; do not
auto-retry. Run eval under a cgroup memory limit (systemd scope) so the OOM is
attributable and the host survives. Cap concurrent _evaluations_ (separate,
smaller limit than build concurrency) — the design only specifies build
concurrency; two simultaneous large evals is the classic way these hosts fall
over.

### 13. Clean-cut migration with no side-by-side period — **Medium**

Accepted trade-off, but three sharp edges:

- Rollback claims "forges are stateless from our side" — not quite: the new
  version _deletes_ legacy buildbot webhooks (Gitea). Rolling back to the old
  release leaves Gitea repos with no working webhook until the old service
  re-registers (does it? old code registered on project reload only).
- Legacy webhook cleanup needs to be conservative: only delete hooks whose URL
  matches this instance's own configured URL, never "anything that looks like
  buildbot".
- Pending statuses posted by old buildbot on in-flight builds at switchover stay
  pending forever on those SHAs (context names are kept identical, which helps —
  a new push fixes it, but tip-of-branch protection on a stale SHA blocks
  merges).

**Recommendation:** Match-by-own-URL deletion only; document the rollback
webhook gap and have the old release's re-registration path verified; on first
startup, optionally re-post statuses for current branch/PR heads (startup
reconciliation nearly does this already — extend it to overwrite dangling
`pending`).

### 14. Session/API token storage details missing — **Low**

Signed cookies: fine, but specify key rotation (two-key acceptance window) or
every deploy with a regenerated key logs everyone out / never invalidates old
sessions for 30 days. API tokens: spec must require storing only a hash (SHA-256
of a random 256-bit token), constant-time compare, with revocation list in DB.
Webhook alias endpoints (`/change_hook/*`) must enforce identical signature
validation as the new paths — say so explicitly to prevent a "legacy path skips
validation" implementation bug.

**Recommendation:** Add the above three sentences to the web-frontend spec.

### 15. Design oversimplifies behavior present in `build_trigger.py` — **Medium**

Comparing against the current implementation, the spec drops or compresses:

- **CacheStatus handling** (`schedule_success`): jobs already `local` in the
  store are skipped but their out-paths still recorded for gcroot registration;
  `cached` (substitutable) jobs are still scheduled to trigger substitution. The
  pipeline spec's "Cached derivation → reported succeeded without rebuilding"
  loses the gcroot-registration-of-skipped-outputs subtlety.
- **Failed-build-report limit** interacts with which statuses are _sent at all_
  (`_maybe_send_notification`), and previously-failed statuses force
  notifications even on success. Spec mentions the limit once in
  forge-integration; the stateful interplay is the actual hard part.
- **Failed-eval jobs become fake per-attribute results** (failed_eval scheduler)
  so they appear in UI/aggregation — spec's eval-error scenario says "build
  fails with error in log" which is coarser; per-attribute eval errors (some
  attrs eval, some don't) must produce per-attribute failed records, not one
  build-level failure.
- **Dependency-failure ordering invariant**: `_handle_completed_job` comments
  that `get_failed_dependents` MUST run before closure pruning. Carry this
  invariant (and its test) into the new scheduler.

**Recommendation:** Treat `build_trigger.py` as the behavioral spec for task
2.1c; enumerate its branches as test cases before rewriting. Add the
mixed-eval-result scenario to the ci-engine spec.

### 16. Alternatives review — **Advisory**

- **Keeping buildbot:** rejection is justified. The code above (a 700-line step
  fighting Triggerable schedulers, deferreds, and MQ events to express
  "topo-sort and run subprocesses") is the strongest argument in the repo for
  the rewrite.
- **SQLite:** rejection ("two code paths") is right as stated, but the inverse
  question matters: Postgres-only raises the smallest-deployment bar. Since the
  NixOS module auto-provisions local Postgres with peer auth, the operator cost
  is near zero. Keep Postgres-only; do not reverse.
- **Separate eval service/process pool:** rejected as "unneeded complexity", but
  eval is the one component that OOMs, needs bwrap+userns, and has the largest
  untrusted-input surface. A tiny `systemd-run`-spawned transient unit per eval
  (still managed by the single engine, not a daemon) would give memory
  accounting and sandboxing without bwrap-in-hardened-unit contortions. Not a
  reversal of D1 — same single engine — but reconsider _how_ eval subprocesses
  are launched (see risks #8, #12).
- **Alembic vs. raw SQL scripts:** raw versioned SQL is fine at this scale; just
  require migrations to run in a transaction with an advisory lock (two service
  instances racing at startup, or restart during migration).

---

## Verdict: **proceed-with-changes**

The core direction is sound: buildbot provides little here, the current code
demonstrably fights it, and "single asyncio process + nix does the work" matches
the actual workload. None of the risks require rethinking the architecture.

Required before implementation starts (blockers):

1. Transactional/resumable crash recovery with effects-replay protection (#1).
2. Serialized re-aggregation + status-generation ordering (#2) and explicit
   cancellation/retry/propagation state machine (#3).
3. Merge-tree-hash build identity replacing the fuzzy "identical merge" reuse
   rule (#4), which also resolves the webhook-ordering supersede race (#10).

Strongly recommended in v1 (cheap now, expensive later): fair round-robin
dequeue across builds (#5), frame-chunked zstd logs with single-reader fan-out
(#6), user-repo-set ACL caching (#7), eval concurrency cap + OOM-as-permanent
(#12), own-URL-matched webhook cleanup (#13).

Everything else can land as fast-follow without schema or API breakage.
