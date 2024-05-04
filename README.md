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
`services.buildbot-nix.master.authBackend` NixOS option.

We have the following two roles:

- Admins:
  - The list of admin usernames is hard-coded in the NixOS configuration.
  - admins can reload the project list
- Organisation member:
  - All member of the organisation where this repository is located
  - They can restart builds

### Integration with GitHub

To integrate with GitHub:

1. **GitHub Token**: Obtain a GitHub token with `admin:repo_hook` and `repo`
   permissions. For GitHub organizations, it's advisable to create a separate
   GitHub user for managing repository webhooks.

#### Optional when using GitHub login

1. **GitHub App**: Set up a GitHub app for Buildbot to enable GitHub user
   authentication on the Buildbot dashboard.
2. **OAuth Credentials**: After installing the app, generate OAuth credentials
   and configure them in the buildbot-nix NixOS module. Set the callback url to
   `https://<your-domain>/auth/login`.

Afterwards add the configured github topic to every project that should build
with buildbot-nix. Notice that the buildbot user needs to have admin access to
this repository because it needs to install a webhook.

### Integration with Gitea

To integrate with Gitea

1. **Gitea Token** Obtain a Gitea token with the following permissions `write:repository` and `write:user` permission.
   For Gitea organizations, it's advisable to create a separate Gitea user.
2. **Gitea App**: (optional). This is optional, when using GitHub as the authentication backend for buildbot.
  Set up a OAuth2 app for Buildbot in the Applications section. This can be done in the global "Site adminstration"
  settings (only available for admins) or in a Gitea organisation or in your personal settings.
  As redirect url set `https://buildbot.your-buildbot-domain.com/auth/login`, where `buildbot.your-buildbot-domain.com`
  should be replaced with the actual domain that your buildbot is running on.

Afterwards add the configured gitea topic to every project that should build with buildbot-nix.
Notice that the buildbot user needs to have repository write access to this repository because it needs to install a webhook
in the repository.


### Binary caches

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

### Real-World Deployments

See Buildbot-nix in action in these deployments:

- **Nix-community infra**:
  [Configuration](https://github.com/nix-community/infra/tree/master/modules/nixos)
  | [Instance](https://buildbot.nix-community.org/)
- **Mic92's dotfiles**:
  [Configuration](https://github.com/Mic92/dotfiles/blob/main/nixos/eve/modules/buildbot.nix)
  | [Instance](https://buildbot.thalheim.io/)
- **Technical University Munich**:
  [Configuration](https://github.com/TUM-DSE/doctor-cluster-config/tree/master/modules/buildbot)
  | [Instance](https://buildbot.dse.in.tum.de/)
- **Numtide**: [Instance](https://buildbot.numtide.com)
