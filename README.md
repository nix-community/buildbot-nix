# Nixbot

Nixbot is a continuous integration (CI) service for the Nix ecosystem, shipped
as a single NixOS service. It is a rewrite of
[buildbot-nix](https://github.com/nix-community/buildbot-nix), its spiritual
ancestor: what started as a set of Buildbot plugins now runs standalone — one
asyncio process handles forge webhooks, nix-eval-jobs evaluation, builds through
the local nix daemon (offloaded via remote builders), commit statuses, and its
own web frontend.

Status: alpha — under active development; APIs, options and the database schema
may change without notice.

## Features

- Fast, Parallel evaluation using
  [nix-eval-jobs](https://github.com/nix-community/nix-eval-jobs)
- Gitea/Github integration:
  - Login with GitHub to control builds
  - CI status notification in pull requests and on the default branch
- All builds share the same nix store for speed
- The last attribute of a build is protected from garbage collection
- Build matrix based on `.#checks` attributes
- No arbitrary code runs outside of the Nix sandbox
- _experimental_
  [hercules-ci effect](https://docs.hercules-ci.com/hercules-ci-effects/) to run
  impure CI steps i.e. deploying NixOS

## Getting Started

To set up nixbot, start by exploring the provided examples:

- Check out the basic setup in [example](./examples/nixbot.nix).
- The NixOS module lives in
  [nixosModules/nixbot.nix](./nixosModules/nixbot.nix).
- For local development, see
  [Local Development Guide](./docs/LOCAL_DEVELOPMENT.md).

Additionally, you can find real-world examples at the end of this document.

The service runs on one machine; to support multiple architectures and to scale
out builds, configure
[nix remote builders](https://nixos.org/manual/nix/stable/advanced-topics/distributed-builds).
For a practical NixOS example, see
[this remote builder configuration](https://github.com/Mic92/dotfiles/blob/main/machines/eve/modules/remote-builder.nix).

## Migrating from buildbot-nix

Nixbot replaces the buildbot master/worker pair with a single service. See the
[migration guide](./docs/MIGRATION.md) for the full upgrade instructions.

## Using Nixbot in Your Project

Nixbot automatically triggers builds for your project under these conditions:

- When a pull request is opened.
- When a commit is pushed to the default git branch.

It does this by evaluating the `.#checks` attribute of your project's flake in
parallel. Each attribute found results in a separate build step. You can test
these builds locally using `nix flake check -L` or
[nix-fast-build](https://github.com/Mic92/nix-fast-build).

If you need to build other parts of your flake, such as packages or NixOS
machines, you should re-export these into the `.#checks` output. Here are two
examples to guide you:

- Using
  [flake-parts](https://github.com/Mic92/dotfiles/blob/10890601a02f843b49fe686d7bc19cb66a04e3d7/flake.nix#L139).
- A
  [plain flake example](https://github.com/nix-community/nixos-images/blob/56b52791312edeade1e6bd853ce56c778f363d50/flake.nix#L53).

### Authentication backend

Anonymous users get read-only access to public projects; private repositories
and their builds are only visible to users with access on the forge. For write
actions (restart, cancel, rebuild) a login is required. Every enabled forge with
OAuth credentials configured offers a login, OIDC via
`services.nixbot.oidc.enable`; several providers can be active at once.

We have the following two roles:

- Admins:
  - The list of admin usernames is hard-coded in the NixOS configuration.
  - admins can reload the project list
- Organisation member:
  - All member of the organisation where this repository is located
  - They can restart builds

##### GitHub Integration

Nixbot uses GitHub App authentication to integrate with GitHub repositories.
This enables automatic webhook setup, commit status updates, and secure
authentication.

See the [GitHub documentation](./docs/GITHUB.md) for setup instructions.

##### Gitea Integration

Nixbot integrates with Gitea using access tokens for repository management and
OAuth2 for user authentication. This enables automatic webhook setup, commit
status updates, and secure authentication.

See the [Gitea documentation](./docs/GITEA.md) for setup instructions.

##### GitLab Integration

Token-based integration with per-repository webhooks and commit status updates.

See the [GitLab documentation](./docs/GITLAB.md) for setup instructions.

##### Generic OIDC Authentication

Nixbot supports generic OpenID Connect (OIDC) authentication, allowing you to
use any OIDC-compliant identity provider (Keycloak, PocketID, Authentik, etc.)
for user login.

See the [OIDC documentation](./docs/OIDC.md) for configuration details.

### Per Repository Configuration

Currently `nixbot` will look for a file named `nixbot.toml` (falling back to the
legacy `buildbot-nix.toml`) in the root of whichever branch it's currently
evaluating, parse it as TOML and apply the configuration specified. The
following table illustrates the supported options.

|                          | key                        | type        | description                                                           | default      | example                                                                                                                                                                                                 |
| :----------------------- | :------------------------- | :---------- | :-------------------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| lock file                | `lock_file`                | `str`       | dictates which lock file `nixbot` will use when evaluating your flake | `flake.lock` | have multiple lockfiles, one for `nixpkgs-stable`, one for `nixpkgs-unstable` or by default pin an input to a private repo, but have a lockfile with that private repo replaced by a public repo for CI |
| attribute                | `attribute`                | `str`       | which attribute in the flake to evaluate and build                    | `checks`     | using a different attribute, like `hydraJobs`                                                                                                                                                           |
| flake_dir                | `flake_dir`                | `str`       | which directory the flake is located                                  | `.`          | using a different flake, like `./tests`                                                                                                                                                                 |
| effects on pull requests | `effects_on_pull_requests` | `bool`      | run hercules-ci effects on pull requests                              | `false`      | set to `true` to run effects on PRs                                                                                                                                                                     |
| effects branches         | `effects_branches`         | `list[str]` | glob patterns for additional branches that run effects                | `[]`         | `["staging", "release/*"]`                                                                                                                                                                              |

By default, effects only run on the default branch. The `effects_branches` and
`effects_on_pull_requests` settings are always read from the **default
branch's** `nixbot.toml` (via `git show`) so that pull request authors cannot
grant themselves effects access.

> **⚠️ Security warning:** PR effects receive the same
> `effects_per_repo_secrets` as default-branch effects. A malicious PR can
> modify the effect code to exfiltrate these secrets. Only enable
> `effects_on_pull_requests` for repositories where you trust all contributors,
> or where no secrets are configured.

## Binary caches

To access the build results on other machines there are two options at the
moment

#### Local binary cache (harmonia)

You can set up a binary cache on the CI machine to make its nix store accessible
from other machines. Check out the README of the
[project](https://github.com/nix-community/harmonia/?tab=readme-ov-file#configuration-for-public-binary-cache-on-nixos),
for an example configuration

#### Cachix

Nixbot also supports pushing packages to cachix via the `services.nixbot.cachix`
options.

#### Attic

Nixbot does not have native support for pushing packages to
[attic](https://github.com/zhaofengli/attic) yet. However it's possible to
integrate run a systemd service as described in
[this example configuration](./examples/attic-watch-store.nix). The systemd
service watches for changes in the local nix store and uploads the contents to
the attic cache.

## (experimental) Hercules CI effects

See [docs/EFFECTS.md](docs/EFFECTS.md) for CLI usage, flake reference support,
and secrets configuration.

## Incompatibilities with the lix overlay

The lix overlay overrides nix-eval-jobs with a version that doesn't work with
nixbot because of missing features and therefore cannot be used together with
the nixbot module.

Possible workaround: Don't use the overlay and only set the
`nix.package = pkgs.lix;` NixOS option.

## Alternatives

- [Garnix](https://garnix.io/) - Fully hosted, zero-config CI for flakes.
- [Hercules CI](https://hercules-ci.com/) - Hosted CI with self-hosted agents.
  Nixbot's effects system is inspired by theirs.
- [Hydra](https://github.com/NixOS/hydra) - The original Nix CI, powers
  nixos.org. Written in Perl, not recommended for new deployments.

## Real-World Deployments

See Nixbot in action in these deployments:

- [**Mic92's dotfiles**](https://github.com/Mic92/dotfiles):
  [Configuration](https://github.com/Mic92/dotfiles/blob/main/machines/eve/modules/nixbot.nix)
  | [Instance](https://buildbot.thalheim.io/)

<!-- These instances still run the buildbot-nix ancestor; uncomment as they
migrate to nixbot:

The following instances run on GitHub:

- [**Nix-community infra**](https://nix-community.org/):
  [Configuration](https://github.com/nix-community/infra/tree/master/modules/nixos)
  | [Instance](https://buildbot.nix-community.org/)
- [**Technical University Munich**](https://dse.in.tum.de/):
  [Configuration](https://github.com/TUM-DSE/doctor-cluster-config/tree/master/modules/buildbot)
  | [Instance](https://buildbot.dse.in.tum.de/)
- [**Numtide**](https://numtide.com/): [Instance](https://buildbot.numtide.com)
- [**Ngi0**](https://www.ngi.eu/ngi-projects/ngi-zero/):
  [Instance](https://buildbot.ngi.nixos.org/#/projects)

The following instances integrated with Gitea:

- **Clan infra**:
  [Configuration](https://git.clan.lol/clan/clan-infra/src/branch/main/modules/buildbot.nix)
  | [Instance](https://buildbot.clan.lol/)
-->

## Get in touch

We have a matrix channel at [nixbot](https://matrix.to/#/#nixbot:thalheim.io).

## Need commercial support or customization?

For commercial support, please contact [Mic92](https://github.com/Mic92/) at
joerg@thalheim.io or reach out to [Numtide](https://numtide.com/contact/).
