## ADDED Requirements

### Requirement: Project and build views

Views for projects whose forge repository is public SHALL be readable without
login. Projects whose forge repository is private SHALL be hidden from anonymous
users and from logged-in users without access: their builds, logs, and existence
SHALL NOT appear in any view or API response unless the authenticated user's
forge account can access the repository or the user is in the configured global
admin list. Access SHALL be determined from the user's accessible-repository set
fetched from the forge once per user and cached with a configurable TTL (default
1 hour, negative results cached); revoking a user's session SHALL drop their
cached grants. Authentication SHALL additionally be required for build control
actions on all projects. The frontend SHALL provide HTML views listing projects,
builds per project (with status, branch/PR, commit, timing), and per-build
detail showing each attribute's status.

#### Scenario: Private project hidden from anonymous user

- **WHEN** an anonymous user lists projects or requests a private project's
  build or log URL directly (HTML or JSON API)
- **THEN** the private project is absent from listings and direct requests are
  rejected without confirming the project exists

#### Scenario: Private project visible to authorized user

- **WHEN** a logged-in user whose forge account has access to a private
  repository opens its project page
- **THEN** its builds and logs are shown

### Requirement: Homepage, search, and theme

The homepage SHALL show a global recent-builds feed (visibility-filtered) with a
project sidebar. A search box SHALL allow substring search over project and
attribute names. The UI SHALL follow the system light/dark preference via CSS.

#### Scenario: Homepage activity feed

- **WHEN** a user opens the homepage
- **THEN** they see recent builds across all projects they may view, plus a
  project list sidebar

#### Scenario: Search

- **WHEN** a user types a substring into the search box
- **THEN** matching projects and attributes (within visible projects) are listed

### Requirement: Build detail presentation

Build pages SHALL show forge links (commit, PR, repository), relative timestamps
(absolute on hover, browser timezone), attributes grouped by system with failed
first, a client-side filter box, inline error excerpts for failed attributes,
and a global queue page SHALL list pending (with FIFO position) and running
builds, visibility-filtered.

#### Scenario: Forge links on build page

- **WHEN** a user views a build page
- **THEN** links to the commit, the pull request (if any), and the repository on
  the forge are shown

#### Scenario: Global queue page

- **WHEN** a user opens the queue page
- **THEN** they see pending and running builds they may view, with FIFO position
  for pending ones

#### Scenario: Browse build detail

- **WHEN** a user opens a build page
- **THEN** they see timestamps as relative times (absolute on hover, browser
  timezone) and all evaluated attributes grouped by system with failed
  attributes listed first, a client-side filter box, and each attribute's
  status, duration, and log link; failed attributes show an inline error excerpt
  (extracted nix error / last log lines)

#### Scenario: Paginated, filterable build list

- **WHEN** a user views a project's build list
- **THEN** builds are paginated and filterable by status and branch, and each
  build URL uses a per-project sequential build number (e.g.
  `/projects/<name>/builds/42`)

### Requirement: Build history navigation

From a build page or an attribute's result, the user SHALL be able to navigate
to the previous and next build of the same project, and to the previous and next
result of the same attribute across builds (like buildbot's builder history).
Each attribute SHALL also have a history view listing its results over time with
status and timing.

#### Scenario: Navigate between builds

- **WHEN** a user views build N of a project
- **THEN** links to build N-1 and N+1 (when they exist) are available

#### Scenario: Navigate attribute history

- **WHEN** a user views an attribute result in build N
- **THEN** they can jump to the same attribute's result in the previous and next
  build, and open a history list of that attribute's results across builds

### Requirement: Live page updates

Build list and build detail pages SHALL update live (htmx polling or SSE) while
builds are running, without manual reload.

#### Scenario: Build page updates as attributes finish

- **WHEN** a user watches a running build's page
- **THEN** attribute statuses and the overall result update automatically as
  they change

### Requirement: Live log viewing

The frontend SHALL display build and evaluation logs, streaming output live for
running builds, and SHALL offer raw log download (plain text always;
zstd-compressed once the log is finalized). All log endpoints (HTML viewer, SSE
stream, raw download) and all live-update endpoints SHALL enforce the same
visibility rules as the underlying build.

#### Scenario: Anonymous SSE request for private build log

- **WHEN** an anonymous client opens the SSE stream URL of a private project's
  build log
- **THEN** the request is rejected without confirming the log exists The log
  viewer SHALL render ANSI colors, provide line-number permalinks, and offer a
  follow-tail toggle.

#### Scenario: Line permalink

- **WHEN** a user opens a log URL with a line anchor
- **THEN** the viewer scrolls to and highlights that line

#### Scenario: Raw log download

- **WHEN** a user with access requests a log's raw download URL
- **THEN** they receive the full log as plain text or .zst

#### Scenario: Follow running build

- **WHEN** a user opens the log of a running attribute build
- **THEN** new log output appears without manual page reload

### Requirement: Authenticated build control

The frontend SHALL support login via GitHub OAuth, Gitea OAuth, and generic
OIDC, open to any account on the configured providers, with signed-cookie
sessions (configurable lifetime, default 30 days, sliding expiry);
private-repository access checks SHALL use the identity of the matching forge.
It SHALL allow authenticated users, authorized by the configured rules
(organization membership or user allowlist), to restart and cancel builds.
Additionally, the author of a pull request SHALL be allowed to restart and
cancel builds of that pull request (matched by forge identity: the logged-in
forge account equals the PR author on the same forge), without requiring broader
authorization. Admin-list and allowlist entries SHALL be provider-qualified
(e.g. `github:alice`, `gitea:bob`, `oidc:<issuer>/<sub>`); the same username on
a different provider SHALL NOT match. An `allowUnauthenticatedControl` option
SHALL permit control actions without login (for VPN/local deployments, current
behavior). All state-changing endpoints SHALL be protected against CSRF
(SameSite cookies plus Origin/Sec-Fetch-Site verification or CSRF tokens);
cross-origin requests carrying only a session cookie SHALL be rejected. Users in
the configured global admin list SHALL be able to control any build, view all
private projects, and enable or disable projects for building. Unauthorized
users SHALL NOT be able to trigger these actions.

#### Scenario: Authorized restart

- **WHEN** an authorized logged-in user clicks restart on a failed build
- **THEN** a new build for the same commit starts

#### Scenario: Rebuild all failed attributes

- **WHEN** an authorized user clicks "rebuild all failed" on a build with failed
  attributes
- **THEN** all failed attributes are rebuilt without re-evaluation and the build
  result is re-aggregated

#### Scenario: Per-attribute rebuild

- **WHEN** an authorized user restarts a single failed attribute
- **THEN** only that attribute is rebuilt without re-evaluation, using its
  persisted derivation path; the combined phase statuses are re-reported and the
  overall build result is re-aggregated from the updated attribute results

#### Scenario: PR author restarts own build

- **WHEN** a logged-in user who authored a pull request restarts or cancels a
  build of that PR
- **THEN** the action is permitted even though the user matches no other authz
  rule

#### Scenario: PR author cannot control other builds

- **WHEN** the same user attempts to restart a build of another PR or of a
  branch
- **THEN** the request is rejected unless another authz rule applies

#### Scenario: Cross-origin request rejected

- **WHEN** a cross-origin request with a valid session cookie but no CSRF proof
  hits a state-changing endpoint
- **THEN** the request is rejected

#### Scenario: Username collision across providers

- **WHEN** a Gitea user logs in with the same username as a configured `github:`
  admin entry
- **THEN** they do not receive admin rights

#### Scenario: Unauthorized control attempt

- **WHEN** an unauthenticated or unauthorized user attempts restart or cancel
- **THEN** the request is rejected with an authentication/authorization error

### Requirement: JSON API

The frontend SHALL expose a JSON API for projects, builds, and build control
mirroring the HTML views under unversioned `/api/...` paths, with OpenAPI
documentation. The API SHALL accept personal API tokens for both read and
control, with the token owner's authz and private-project visibility. Tokens
SHALL be generated and revoked in the UI, displayed once at creation, stored
only as hashes with constant-time comparison, support optional expiry, and
revocation SHALL take effect immediately.

#### Scenario: Token-authenticated restart

- **WHEN** a client calls the restart endpoint with a valid API token of an
  authorized user
- **THEN** the build is restarted; an invalid or revoked token is rejected

#### Scenario: Query builds via API

- **WHEN** a client requests the builds endpoint for a project
- **THEN** it receives build records including status, commit, and attribute
  results as JSON

### Requirement: Prometheus metrics

The frontend SHALL expose a Prometheus-compatible `/metrics` endpoint reporting
at least build counts by result, build/eval durations, and queue depth.

#### Scenario: Scrape metrics

- **WHEN** a Prometheus scraper requests `/metrics`
- **THEN** it receives current engine metrics in Prometheus text format, without
  labels that reveal private repository names

### Requirement: Webhook endpoints

The frontend SHALL expose HTTP endpoints receiving GitHub and Gitea webhooks at
`/webhooks/github` and `/webhooks/gitea`, with compatibility aliases at the
legacy buildbot paths `/change_hook/github` and `/change_hook/gitea`, validating
signatures (GitHub App webhook secret for GitHub, per-repository secrets for
Gitea) before processing. The legacy alias paths SHALL enforce identical
signature validation as the new paths.

#### Scenario: Invalid webhook signature

- **WHEN** a webhook arrives with an invalid signature
- **THEN** the request is rejected and no build is triggered
