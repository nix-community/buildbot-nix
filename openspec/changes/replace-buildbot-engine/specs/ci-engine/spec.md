## ADDED Requirements

### Requirement: Single-process asyncio engine

The system SHALL run as a single Python asyncio process with no master/worker
separation and no Twisted dependency. All evaluation and build work SHALL
execute via subprocesses managed by the event loop.

#### Scenario: Service startup

- **WHEN** the service starts
- **THEN** it runs as one process serving HTTP, processing change events, and
  executing builds without requiring any worker process or buildbot dependency

### Requirement: Build lifecycle state machine

The engine SHALL track each build through states: pending → evaluating →
building → (succeeded | failed | cancelled), with the final result aggregated
when all attributes reach a terminal state. Each evaluated attribute SHALL be
tracked individually with its own status, derivation path, and log. Attributes
that fail evaluation while others evaluate successfully SHALL produce
per-attribute failed records (not one build-level failure). When attributes are
rebuilt after the build finished, re-aggregation SHALL be serialized per build
(row-level locking) and forge status posts SHALL carry a monotonic generation so
stale posts are dropped.

#### Scenario: Attribute rebuild re-aggregates build result

- **WHEN** the only failed attribute of a failed build is rebuilt and succeeds
- **THEN** the overall build result becomes succeeded

#### Scenario: Successful build

- **WHEN** a change event arrives for a project
- **THEN** the engine creates a build, evaluates it, builds each attribute, and
  marks the build succeeded when all attributes succeed

#### Scenario: Partial failure

- **WHEN** at least one attribute build fails
- **THEN** the remaining attributes still run to completion and the build's
  final aggregated result is failed

#### Scenario: Mixed evaluation results

- **WHEN** some attributes evaluate successfully and others fail evaluation
- **THEN** successful attributes are built and each failed-eval attribute
  appears as an individual failed attribute record with its error text

### Requirement: Cancellation of superseded builds

The engine SHALL cancel in-flight builds for a branch or pull request when a
change event with a different commit for the same branch or pull request
arrives, killing the associated process groups. Supersede decisions SHALL
compare commits/merge-trees, not webhook arrival order; duplicate deliveries
SHALL be deduplicated by delivery ID. Cancellation SHALL set a flag that
suppresses pending automatic retries; cancelled attributes SHALL propagate
"skipped (superseded)" to dependent attributes (never "dependency failed") and
SHALL NOT enter the failed-build cache. A build shared by multiple contexts
SHALL only be cancelled when superseded in all contexts referencing it.

#### Scenario: Out-of-order webhook delivery

- **WHEN** a stale event for an older commit arrives after the build for a newer
  commit started
- **THEN** the running build is not cancelled and no build for the older commit
  starts

#### Scenario: Cancellation during retry window

- **WHEN** an attribute failed transiently and is awaiting its automatic retry
  while the build is cancelled
- **THEN** the retry does not fire and the attribute ends in the cancelled state

#### Scenario: New push supersedes running build

- **WHEN** a build for branch X is running and a new push to branch X arrives
- **THEN** the running build is cancelled and a new build for the latest commit
  starts

#### Scenario: PR closed or merged

- **WHEN** a pull request with an in-flight build is closed or merged
- **THEN** the in-flight build is cancelled

### Requirement: Build identity by merge-tree hash

Build identity SHALL be keyed on the post-merge git tree hash (the tree of the
worktree after any local PR merge). When multiple contexts (branch push, PR,
re-push of identical content) produce the same tree hash, the engine SHALL run
one build and post statuses for each context.

#### Scenario: Branch push and fast-forward PR share tree

- **WHEN** a branch push and a PR whose local merge yields the identical tree
  both trigger builds
- **THEN** one build runs and its results are reported to both contexts

#### Scenario: Base branch advanced

- **WHEN** a PR head was already built but the base branch has advanced so the
  local merge yields a new tree
- **THEN** a new build runs for the new merge tree

### Requirement: Skip-CI convention

The engine SHALL skip building when the head commit of a push or the PR head
commit contains `[skip ci]` or `[ci skip]` in its message; only the head commit
is inspected.

#### Scenario: Skip marker in commit message

- **WHEN** a push event's head commit message contains `[skip ci]`
- **THEN** no build is created for that commit

### Requirement: Persistent state in SQL database

The engine SHALL persist projects, builds, attribute results (including
derivation paths, enabling rebuilds without re-evaluation), failed-build cache,
and failed-status records in PostgreSQL via async SQLAlchemy, surviving service
restarts. Schema migrations SHALL be simple versioned SQL scripts applied
automatically at startup, executed in a transaction under an advisory lock.

Crash recovery SHALL resume rather than restart: attribute completion (status,
log metadata, post-build-step record) SHALL be a single transactional write; on
startup, attributes already terminal in the database are skipped, pending
attributes are re-checked against the nix store (`nix path-info`) before
re-running. Effects SHALL be guarded by a started-flag and SHALL NOT be re-run
automatically on recovery.

#### Scenario: Restart recovery

- **WHEN** the service restarts while builds were in flight
- **THEN** unfinished attributes resume (skipping those already terminal in the
  database), completed build history remains queryable, and effects that had
  started are not re-run automatically

#### Scenario: Recovery does not redo finished attributes

- **WHEN** an attribute finished and was committed to the database just before a
  crash
- **THEN** after restart that attribute is not rebuilt and no duplicate statuses
  or post-build steps run

### Requirement: Failed-build caching

The engine SHALL support an opt-in failed-build cache (configurable, default off
— matching current `cacheFailedBuilds` behavior). When enabled, it records
permanently failed derivations and skips rebuilding a derivation that previously
failed for the same derivation path, reporting the cached failure with a link to
the first failing build. Transient infrastructure errors (e.g. builder
disconnect, network failure) and cancellations SHALL NOT be recorded; genuine
build failures (including timeouts) are. An explicit restart of the build or
attribute SHALL remove the cache entry and rebuild the derivation.

#### Scenario: Skip known-failed derivation

- **WHEN** evaluation produces a derivation recorded as failed
- **THEN** the engine skips the build and reports the cached failure with a link
  to the original log

### Requirement: Scheduled effects (Hercules onSchedule)

The engine SHALL support Hercules-style scheduled effects: on each successful
default-branch build, discover schedules from the flake via
`buildbot-effects list-schedules`, persist them, and run the named effects at
their cron-like `when` times (minute/hour/dayOfWeek/dayOfMonth with
deterministic pseudo-random defaults to avoid thundering herds), via
`buildbot-effects run-scheduled`, with logs and statuses like other effects.

#### Scenario: Schedule discovered and executed

- **WHEN** a default-branch build succeeds for a flake defining
  `herculesCI.onSchedule` entries
- **THEN** the schedules are persisted and the corresponding effects run at
  their scheduled times

#### Scenario: Schedule removed

- **WHEN** a later default-branch build no longer defines a previously persisted
  schedule
- **THEN** that schedule is removed and no longer fires

### Requirement: Log retention

The engine SHALL store build logs zstd-compressed on disk and delete logs and
build records older than a configurable retention period (default 90 days).

#### Scenario: Cleanup task

- **WHEN** the retention task runs
- **THEN** builds and logs older than the configured horizon are removed
