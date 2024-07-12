# Buildbot-nix

Buildbot-nix is a NixOS module designed to integrate
[Buildbot](https://www.buildbot.net/), a continuous integration (CI) framework,
into the Nix ecosystem. This module is under active development, and while it's
generally stable and widely used, please be aware that some APIs may change over
time.

## Getting Started with Buildbot Setup

To set up Buildbot using Buildbot-nix, you can start by exploring the provided
examples:

- Check out the basic setup in [example](./examples/default.nix).
- Learn about configuring the Buildbot master in
  [master module](./nix/master.nix).
- Understand how to set up a Buildbot worker in
  [worker module](./nix/worker.nix).

Additionally, you can find real-world examples at the end of this document.

Buildbot masters and workers can be deployed either on the same machine or on
separate machines. To support multiple architectures, configure them as
[nix remote builders](https://nixos.org/manual/nix/stable/advanced-topics/distributed-builds).
For a practical NixOS example, see
[this remote builder configuration](https://github.com/Mic92/dotfiles/blob/main/nixos/eve/modules/remote-builder.nix).

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

At the moment all projects are visible without authentication.

For some actions a login is required. This login can either be based on GitHub
or on Gitea (more logins may follow). The backend is set by the
`services.buildbot-nix.master.authBackend` NixOS option ("gitea"/"github",
"github" by default).

We have the following two roles:

- Admins:
  - The list of admin usernames is hard-coded in the NixOS configuration.
  - admins can reload the project list
- Organisation member:
  - All member of the organisation where this repository is located
  - They can restart builds

### Integration with GitHub

#### GitHub App

This is the preferred option to setup buildbot-nix for GitHub.

To integrate with GitHub using app authentication:

1. **GitHub App**:
   1. Create a new GitHub app by navigating to
      `https://github.com/settings/apps/new` for single-user installations or
      `https://github.com/organizations/<org>/settings/apps/new` for
      organisations where `<org>` is the name of your GitHub organizaction.
   2. GitHub App Name: "buildbox-nix <org>"
   3. Homepage URL: `https://buildbot.<your-domain>`
   4. Callback URL: `https://buildbot.<your-domain>/auth/login`.
   5. Disable the Webhook
   6. Repository Permissions:
   - Contents: Read-only
   - Commit statuses: Read and write
   - Metadata: Read-only
   - Webhooks: Read and write
   7. Organisation Permissions (only if you create this app for an
      organisation):
   - Members: Read-only
2. **GitHub App private key**: Get the app private key and app ID from GitHub,
   configure using the buildbot-nix NixOS module.
   - Set
     `services.buildbot-nix.master.github.authType.app.id = <your-github-id>;`
   - Set
     `services.buildbot-nix.master.github.authType.app.secretKeyFile = "/path/to.pem";`
3. **Install App**: Install the app for an organization or specific user.
4. **Refresh GitHub Projects**: Currently buildbot-nix doesn't respond to
   changes (new repositories or installations) automatically, it is therefore
   necessary to manually trigger a reload or wait for the next periodic reload.

#### Token Auth

To integrate with GitHub using legacy token authentication:

1. **GitHub Token**: Obtain a GitHub token with `admin:repo_hook` and `repo`
   permissions. For GitHub organizations, it's advisable to create a separate
   GitHub user for managing repository webhooks.

### Optional when using GitHub login

1. **GitHub App**: Set up a GitHub app for Buildbot to enable GitHub user
   authentication on the Buildbot dashboard. (can be the same as for GitHub App
   auth)
2. **OAuth Credentials**: After installing the app, generate OAuth credentials
   and configure them in the buildbot-nix NixOS module. Set the callback url to
   `https://<your-domain>/auth/login`.

Afterwards add the configured github topic to every project that should build
with buildbot-nix. Notice that the buildbot user needs to have admin access to
this repository because it needs to install a webhook.

### Integration with Gitea

To integrate with Gitea

1. **Gitea Token** Obtain a Gitea token with the following permissions
   `write:repository` and `write:user` permission. For Gitea organizations, it's
   advisable to create a separate Gitea user. Buildbot-nix will use this token
   to automatically setup a webhook in the repository.
2. **Gitea App**: (optional). This is optional, when using GitHub as the
   authentication backend for buildbot. Set up a OAuth2 app for Buildbot in the
   Applications section. This can be done in the global "Site adminstration"
   settings (only available for admins) or in a Gitea organisation or in your
   personal settings. As redirect url set
   `https://buildbot.your-buildbot-domain.com/auth/login`, where
   `buildbot.your-buildbot-domain.com` should be replaced with the actual domain
   that your buildbot is running on.

Afterwards add the configured gitea topic to every project that should build
with buildbot-nix. Notice that the buildbot user needs to have repository write
access to this repository because it needs to install a webhook in the
repository.

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
