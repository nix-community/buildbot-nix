## ADDED Requirements

### Requirement: Simultaneous multi-forge support

A single instance SHALL support GitHub and Gitea backends concurrently, each
with its own credentials, webhooks, and project set.

#### Scenario: Mixed deployment

- **WHEN** both GitHub and Gitea backends are configured
- **THEN** projects from both forges are discovered, built, and receive statuses
  independently

### Requirement: Project discovery and enablement

The system SHALL discover repositories from GitHub (via GitHub App credentials
only) and Gitea (token) and refresh the repository list periodically and on
demand. Whether a repository is built SHALL be controlled by an enablement flag
stored in the database, toggled by admins in the web UI. Topic-based filters
SHALL be applied once as a legacy import (on first startup with an empty
projects table) to pre-enable matching projects. Repository and user allowlists
(`repoAllowlist`/`userAllowlist`) SHALL remain available as discovery-time
filters restricting which repositories can be discovered or enabled at all.
Projects SHALL be identified by the forge's stable repository ID so renames and
transfers keep project history.

#### Scenario: Repository renamed on forge

- **WHEN** an enabled repository is renamed or transferred on the forge
- **THEN** the project follows the new name on refresh and retains its build
  history and enablement

#### Scenario: Admin enables a project

- **WHEN** an admin enables a discovered repository in the web UI
- **THEN** subsequent change events for that repository trigger builds

#### Scenario: Disabled repository ignored

- **WHEN** a change event arrives for a discovered but disabled repository
- **THEN** no build is created

### Requirement: Private repository fetch credentials

Fetching private repositories for clone/evaluation SHALL use short-lived GitHub
App installation tokens minted per fetch for GitHub; Gitea SHALL support either
a configured token (netrc) or SSH key with known-hosts file (current
`gitea.sshPrivateKeyFile`/`sshKnownHostsFile` behavior).

#### Scenario: Private repo fetch

- **WHEN** evaluation of a private GitHub repository starts
- **THEN** a short-lived installation token is minted and used for the fetch,
  and is not persisted beyond the build

### Requirement: Webhook-driven change events

The system SHALL trigger builds from GitHub and Gitea webhooks for pushes to the
default branch, pushes to additional branches matching per-repo configuration
(as in current buildbot-nix repo config), merge-queue branches
(`gh-readonly-queue/*`, `gitea-mq/*`, `staging`, `trying` — always built, as
today), and pull-request events. All pull requests including fork PRs SHALL be
built (current behavior; the Nix sandbox is the trust boundary).

#### Scenario: Pull request opened

- **WHEN** a pull request webhook arrives for an enabled project
- **THEN** a build is started for the local merge of the PR head into its base
  branch, with statuses reported on the head commit

#### Scenario: Merge-queue branch push

- **WHEN** a push arrives for a `gh-readonly-queue/*` branch of an enabled
  project
- **THEN** a build is started and its statuses reported, so the merge queue can
  proceed

### Requirement: Webhook registration

GitHub change events SHALL be received through the GitHub App's event
subscription without per-repository hook creation. For Gitea, the system SHALL
auto-register a webhook with a per-repository generated secret when a project is
enabled. During migration, the system SHALL remove leftover webhooks created by
the old buildbot-based version only when their URL matches this instance's
configured webhook base URL. The webhook base URL SHALL be configurable
independently of the UI URL (current `webhookBaseUrl`). Webhook deliveries SHALL
be idempotent: duplicates deduplicated by delivery ID.

#### Scenario: Gitea project enabled

- **WHEN** an admin enables a Gitea repository
- **THEN** a webhook with a per-repository secret is registered on the
  repository

#### Scenario: Legacy webhook cleanup

- **WHEN** a project with an old buildbot webhook pointing at this instance's
  webhook URL is enabled
- **THEN** that webhook is removed from the repository; webhooks pointing
  elsewhere are left untouched

### Requirement: Startup reconciliation

After crash recovery completes, the system SHALL check the default branch head
and open pull request heads of each enabled project and start a build where no
build record exists for the corresponding merge-tree (regardless of result), to
recover from webhooks missed during downtime. Duplicates against recovered
builds are resolved by the supersede rules.

#### Scenario: Missed push during downtime

- **WHEN** the service starts and an enabled project's default branch head has
  no build
- **THEN** a build for that head commit is started

### Requirement: Pull-based polling

The system SHALL support a pull-based mode that polls repositories for new
commits where webhooks are not available, with configurable poll interval, poll
spread to stagger requests, and per-repository SSH keys/known-hosts for private
clones.

#### Scenario: Poller detects new commit

- **WHEN** polling finds a new commit on a watched branch
- **THEN** a build for that commit is started

### Requirement: Commit status reporting

The system SHALL report commit statuses matching current buildbot-nix behavior
on two layers: (a) combined per-phase statuses with context names `nix-eval`,
`nix-build`, and `nix-effects` (pending, success, failure), each with a target
URL linking to the build page; and (b) individual failure statuses with context
`nix-build <type>:<owner>/<repo>#checks.<attr>` for attributes that fail
evaluation, fail to build, hit a cached failure, fail via dependency, or are
cancelled — capped at the configured failed-build report limit (default 47).
Context names SHALL match the current implementation so existing branch
protection rules continue to work. Failed per-attribute statuses SHALL be
persisted per revision and flipped to success on a successful rebuild —
including force-running already-built attributes solely to update a previously
failed status.

#### Scenario: Per-attribute failure statuses capped

- **WHEN** a build fails more attributes than the configured report limit
- **THEN** individual failure statuses are posted for at most the limit, and the
  combined `nix-build` status still reports the overall failure

#### Scenario: Status lifecycle

- **WHEN** a build starts, then finishes
- **THEN** the commit shows a pending status during the build and a final
  success/failure status afterwards, linking to the build page

#### Scenario: Rebuild clears stale failure

- **WHEN** a previously failed build is restarted and succeeds
- **THEN** the failed statuses recorded for that commit are updated to success
