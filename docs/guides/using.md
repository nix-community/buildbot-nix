# Using Buildbot in Your Project

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

## Authentication backend

At the moment `buildbot-nix` offers two access modes, `public` and
`fullyPrivate`. `public` is the default and gives read-only access to all of
buildbot, including builds, logs and builders. For read-write access,
authentication is still needed, this is controlled by the `authBackend` option.

`fullyPrivate` will hide buildbot behind `oauth2-proxy` which protects the whole
buildbot instance. buildbot fetches the currently authenticated user from
`oauth2-proxy` so the same admin, organisation rules apply.

`fullyPrivate` acccess mode is a workaround as buildbot does not support hiding
information natively as now.

### Public

For some actions a login is required. The authentication backend is set by the
{nix:option}`services.buildbot-nix.master.authBackend` NixOS option ("github", "gitea",
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

#### GitHub Integration

Buildbot-nix uses GitHub App authentication to integrate with GitHub
repositories. This enables automatic webhook setup, commit status updates, and
secure authentication.

See {doc}`github` for setup instructions.

#### Gitea Integration

Buildbot-nix integrates with Gitea using access tokens for repository management
and OAuth2 for user authentication. This enables automatic webhook setup, commit
status updates, and secure authentication.

See {doc}`gitea` for setup instructions.

#### Generic OIDC Authentication

Buildbot-nix supports generic OpenID Connect (OIDC) authentication, allowing you
to use any OIDC-compliant identity provider (Keycloak, PocketID, Authentik,
etc.) for user login.

See {doc}`oidc` for configuration details.

### Fully Private

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
[fully-private-github](../../examples/fully-private-github.nix)

## Per Repository Configuration

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

