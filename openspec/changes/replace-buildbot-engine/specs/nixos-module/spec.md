## ADDED Requirements

### Requirement: Single-service NixOS module

The repository SHALL provide a NixOS module that runs the CI service as one
systemd unit (no separate master/worker units), configuring database (local
Postgres over unix socket with peer auth by default, remote DB with password
credential as option), forge credentials, authentication, URL, listeners (TCP
port and optional unix socket), and build settings.

#### Scenario: Minimal deployment

- **WHEN** an operator enables the module with forge credentials and a URL
- **THEN** one systemd service starts, serves the web frontend, and builds
  discovered projects

### Requirement: Migration guidance for removed options

The module SHALL fail evaluation with clear messages (via removed/renamed option
stubs) when configurations use the old master/worker options, pointing to their
replacements.

#### Scenario: Old option used

- **WHEN** a configuration sets a removed master/worker option (including
  `authBackend = "httpbasicauth"`, `httpBasicAuthPasswordFile`, `accessMode`,
  `workersFile`, `localWorkers`, GitHub legacy token options)
- **THEN** evaluation fails with a message naming the replacement option or
  migration step

### Requirement: Health endpoint

The service SHALL expose a health endpoint reflecting database connectivity for
systemd/monitoring restart logic.

#### Scenario: DB down

- **WHEN** the database is unreachable
- **THEN** the health endpoint reports unhealthy while the service keeps serving
  requests that do not need the database

### Requirement: Hardened service unit

The systemd unit SHALL run as a dedicated user with systemd hardening, with
access limited to its state directory, the nix daemon socket, and the network.

Operator-supplied secrets (GitHub App private key and webhook secret, Gitea
token, OAuth client secrets, OIDC client secret, remote-DB password, effects
secrets) SHALL be passed via systemd `LoadCredential` and read from
`$CREDENTIALS_DIRECTORY`, not via world-readable paths or environment variables.
Service-generated secrets (per-repo Gitea webhook secrets) are stored in the
database. The session-cookie signing key SHALL be auto-generated in the
StateDirectory (overridable via LoadCredential) with a two-key rotation window
so redeploys do not log out all users.

#### Scenario: Service confinement

- **WHEN** the service runs
- **THEN** it operates as a non-root user with its state under a dedicated
  StateDirectory and reads secrets from the systemd credentials directory

### Requirement: Optional nginx + ACME virtual host

The module SHALL offer an option to configure an nginx reverse proxy virtual
host with ACME certificates for the service, while also supporting bare
operation behind an operator-provided proxy. When an outputs path is configured
together with nginx, the module SHALL optionally serve the outputs directory
over HTTP (autoindex), as today.

#### Scenario: Outputs directory served

- **WHEN** the operator enables nginx and configures an outputs path with
  serving enabled
- **THEN** the outputs directory is browsable over HTTP at the configured
  location

#### Scenario: Managed proxy enabled

- **WHEN** the operator enables the nginx option with a domain
- **THEN** the service is reachable over HTTPS at that domain with an ACME
  certificate

### Requirement: Integration test coverage

The flake SHALL include NixOS VM tests exercising the full flow: webhook
receipt, evaluation, build, status reporting against a fake forge, for both
GitHub and Gitea modes.

#### Scenario: CI check

- **WHEN** the flake checks run
- **THEN** the VM test builds a sample flake end-to-end and asserts the reported
  commit status
