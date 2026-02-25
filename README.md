# Buildbot-nix

Buildbot-nix is a NixOS module designed to integrate
[Buildbot](https://www.buildbot.net/), a continuous integration (CI) framework,
into the Nix ecosystem. This module is under active development, and while it's
generally stable and widely used, please be aware that some APIs may change over
time.

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

## Getting Started with Buildbot Setup

To set up Buildbot using Buildbot-nix, you can start by exploring the provided
examples:

- Check out the basic setup in [example](./examples/default.nix).
- Learn about configuring the Buildbot master in
  [master module](./nix/master.nix).
- Understand how to set up a Buildbot worker in
  [worker module](./nix/worker.nix).
- For local development, see
  [Local Development Guide](./docs/LOCAL_DEVELOPMENT.md).

Additionally, you can find real-world examples at the end of this document.

Buildbot masters and workers can be deployed either on the same machine or on
separate machines. To support multiple architectures, configure them as
[nix remote builders](https://nixos.org/manual/nix/stable/advanced-topics/distributed-builds).
For a practical NixOS example, see
[this remote builder configuration](https://github.com/Mic92/dotfiles/blob/main/machines/eve/modules/remote-builder.nix).

## Using `buildbot` with NixOS 24.05 (stable release)

The module applies custom patches that only apply to buildbot > 4.0.0. To use
buildbot-nix with NixOS 24.05, you should therefore not override the nixpkgs
input to your own stable version of buildbot-nix and leave it to the default
instead that is set to nixos-unstable-small.

So instead of using this in your flake

```
inputs = {
  buildbot-nix.url = "github:nix-community/buildbot-nix";
  buildbot-nix.inputs.nixpkgs.follows = "nixpkgs";
};
```

Just use:

```
inputs = {
  buildbot-nix.url = "github:nix-community/buildbot-nix";
};
```

An alternative is to point nixpkgs to your own version of nixpkgs-unstable in
case you are already using it elsewhere.

## Using Buildbot in Your Project

Buildbot-nix automatically triggers builds for your project under these
conditions:

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

At the moment `buildbot-nix` offers two access modes, `public` and
`fullyPrivate`. `public` is the default and gives read-only access to all of
buildbot, including builds, logs and builders. For read-write access,
authentication is still needed, this is controlled by the `authBackend` option.

`fullyPrivate` will hide buildbot behind `oauth2-proxy` which protects the whole
buildbot instance. buildbot fetches the currently authenticated user from
`oauth2-proxy` so the same admin, organisation rules apply.

`fullyPrivate` acccess mode is a workaround as buildbot does not support hiding
information natively as now.

#### Public

For some actions a login is required. The authentication backend is set by the
`services.buildbot-nix.master.authBackend` NixOS option ("github", "gitea",
"oidc", or others).

**Note**: You can configure both GitHub and Gitea integrations simultaneously,
regardless of which authentication backend you choose. The auth backend only
determines how users log in to the Buildbot interface.

We have the following two roles:

- Admins:
  - The list of admin usernames is hard-coded in the NixOS configuration.
  - admins can reload the project list
- Organisation member:
  - All member of the organisation where this repository is located
  - They can restart builds

##### GitHub Integration

Buildbot-nix uses GitHub App authentication to integrate with GitHub
repositories. This enables automatic webhook setup, commit status updates, and
secure authentication.

See the [GitHub documentation](./docs/GITHUB.md) for setup instructions.

##### Gitea Integration

Buildbot-nix integrates with Gitea using access tokens for repository management
and OAuth2 for user authentication. This enables automatic webhook setup, commit
status updates, and secure authentication.

See the [Gitea documentation](./docs/GITEA.md) for setup instructions.

##### Generic OIDC Authentication

Buildbot-nix supports generic OpenID Connect (OIDC) authentication, allowing you
to use any OIDC-compliant identity provider (Keycloak, PocketID, Authentik,
etc.) for user login.

See the [OIDC documentation](./docs/OIDC.md) for configuration details.

#### Fully Private

To enable fully private mode, set `acessMode.fullyPrivate` to an attrset
containing the required options for fully private use, refer to the examples and
module implementation (`nix/master.nix`).

This access mode honors the `admins` option in addition to the
`accessMode.fullyPrivate.organisations` option. To allow access from certain
organisations, you must explicitly list them.

If you've set `authBackend` previously, unset it, or you will get an error about
a conflicting definitions. `fullyPrivate` requires the `authBackend` to be set
to `basichttpauth` to function (this is handled by the module, which is why you
can leave it unset). For a concrete example please refer to
[fully-private-github](./examples/fully-private-github.nix)

### Per Repository Configuration

Currently `buildbot-nix` will look for a file named `buildbot-nix.toml` in the
root of whichever branch it's currently evaluating, parse it as TOML and apply
the configuration specified. The following table illustrates the supported
options.

|                          | key                        | type        | description                                                                 | default      | example                                                                                                                                                                                                 |
| :----------------------- | :------------------------- | :---------- | :-------------------------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| lock file                | `lock_file`                | `str`       | dictates which lock file `buildbot-nix` will use when evaluating your flake | `flake.lock` | have multiple lockfiles, one for `nixpkgs-stable`, one for `nixpkgs-unstable` or by default pin an input to a private repo, but have a lockfile with that private repo replaced by a public repo for CI |
| attribute                | `attribute`                | `str`       | which attribute in the flake to evaluate and build                          | `checks`     | using a different attribute, like `hydraJobs`                                                                                                                                                           |
| flake_dir                | `flake_dir`                | `str`       | which directory the flake is located                                        | `.`          | using a different flake, like `./tests`                                                                                                                                                                 |
| effects on pull requests | `effects_on_pull_requests` | `bool`      | run hercules-ci effects on pull requests                                    | `false`      | set to `true` to run effects on PRs                                                                                                                                                                     |
| effects branches         | `effects_branches`         | `list[str]` | glob patterns for additional branches that run effects                      | `[]`         | `["staging", "release/*"]`                                                                                                                                                                              |

By default, effects only run on the default branch. The `effects_branches` and
`effects_on_pull_requests` settings are always read from the **default
branch's** `buildbot-nix.toml` (via `git show`) so that pull request authors
cannot grant themselves effects access.

> **⚠️ Security warning:** PR effects receive the same
> `effects_per_repo_secrets` as default-branch effects. A malicious PR can
> modify the effect code to exfiltrate these secrets. Only enable
> `effects_on_pull_requests` for repositories where you trust all contributors,
> or where no secrets are configured.

## Binary caches

To access the build results on other machines there are two options at the
moment

#### Local binary cache (harmonia)

You can set up a binary cache on your buildbot-worker machine to make its nix
store accessible from other machines. Check out the README of the
[project](https://github.com/nix-community/harmonia/?tab=readme-ov-file#configuration-for-public-binary-cache-on-nixos),
for an example configuration

#### Cachix

Buildbot-nix also supports pushing packages to cachix. Check out the comment out
[example configuration](https://github.com/Mic92/buildbot-nix/blob/main/examples/master.nix)
in our repository.

#### Attic

Buildbot-nix does not have native support for pushing packages to
[attic](https://github.com/zhaofengli/attic) yet. However it's possible to
integrate run a systemd service as described in
[this example configuration](./examples/attic-watch-store.nix). The systemd
service watches for changes in the local buildbot-nix store and uploads the
contents to the attic cache.

## (experimental) Hercules CI effects

See [flake.nix](flake.nix) for an example and
[https://docs.hercules-ci.com/hercules-ci/effects/] for documentation.

You can run hercules-effects locally using the `buildbot-effects` cli:

```
$ buildbot-effects --help
usage: buildbot-effects [-h] [--secrets SECRETS] [--rev REV] [--branch BRANCH] [--repo REPO] [--path PATH] {list,run,run-all} ...

Run effects from a hercules-ci flake

positional arguments:
  {list,run,run-all}  Command to run
    list              List available effects
    run               Run an effect
    run-all           Run all effects

options:
  -h, --help          show this help message and exit
  --secrets SECRETS   Path to a json file with secrets
  --rev REV           Git revision to use
  --branch BRANCH     Git branch to use
  --repo REPO         Git repo to prepend to be
  --path PATH         Path to the repository
```

Example from the buildbot-nix repository:

```console
$ git clone github.com/nix-community/buildbot-nix
$ cd buildbot-nix
```

```console
$ nix run github:nix-community/buildbot-nix#buildbot-effects -- list
[
  "deploy"
]
```

```console
$ nix run github:nix-community/buildbot-nix#buildbot-effects -- --branch main run deploy
{branch:main,rev:5d2e0af1cfccfc209b893b89392cf80f5640d936,tag:null}
Hello, world!
```

### Effects Secrets Configuration

Secrets for buildbot effects can be configured at different scopes:

1. **Repository-specific**: `"github:owner/repo"` - applies to a single
   repository
2. **Organization-wide**: `"github:org/*"` - applies to all repositories in an
   organization

```nix
services.buildbot-nix.master.effects.perRepoSecretFiles = {
  # All repos in nix-community org get this token
  "github:nix-community/*" = config.agenix.secrets.nix-community-effects.path;

  # This specific repo gets its own token (overrides org-level)
  "github:nix-community/buildbot-nix" = config.agenix.secrets.buildbot-nix-effects.path;

  # All repos in a Gitea org
  "gitea:my-org/*" = config.agenix.secrets.my-org-effects.path;
};
```

The secrets files must be valid JSON files containing the secrets that will be
made available to your effects at runtime.

## Incompatibilities with the lix overlay

The lix overlay overrides nix-eval-jobs with a version that doesn't work with
buildbot-nix because of missing features and therefore cannot be used together
with the buildbot-nix module.

Possible workaround: Don't use the overlay and only set the
`nix.package = pkgs.lix;` NixOS option.

## Alternatives

- [Garnix](https://garnix.io/) - Fully hosted, zero-config CI for flakes.
- [Hercules CI](https://hercules-ci.com/) - Hosted CI with self-hosted agents.
  Buildbot-nix's effects system is inspired by theirs.
- [Hydra](https://github.com/NixOS/hydra) - The original Nix CI, powers
  nixos.org. Written in Perl, not recommended for new deployments.

## Real-World Deployments

See Buildbot-nix in action in these deployments:

The following instances run on GitHub:

- [**Nix-community infra**](https://nix-community.org/):
  [Configuration](https://github.com/nix-community/infra/tree/master/modules/nixos)
  | [Instance](https://buildbot.nix-community.org/)
- [**Mic92's dotfiles**](https://github.com/Mic92/dotfiles):
  [Configuration](https://github.com/Mic92/dotfiles/blob/main/nixos/eve/modules/buildbot.nix)
  | [Instance](https://buildbot.thalheim.io/)
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

## Get in touch

We have a matrix channel at
[buildbot-nix](https://matrix.to/#/#buildbot-nix:thalheim.io).

## Need commercial support or customization?

For commercial support, please contact [Mic92](https://github.com/Mic92/) at
joerg@thalheim.io or reach out to [Numtide](https://numtide.com/contact/).
