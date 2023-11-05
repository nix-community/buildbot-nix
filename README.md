# Buildbot-nix

A nixos module to make buildbot a proper Nix-CI.

For an example checkout the [example](./examples/default.nix) and the module
descriptions for [master](./nix/master.nix) and [worker](./nix/worker.nix).

This project is still in early stage and many APIs might change over time.

## Github

We currently primarly support Github as a platform but we are also looking into
supporting other CIs such as gitea.

Buildbot requires a GitHub app, to allow login for GitHub users to its
dashboard.

Furthermore buildbot requires a github token with the following permissions:

- `admin:repo_hook`, `public_repo`, `repo:status`

For github organisations it's recommend to create an additional GitHub user for
that.

## Real-worlds deployments

- [Nix-community infra](https://github.com/nix-community/infra/tree/master/modules/nixos)
- [Mic92's dotfiles](https://github.com/Mic92/dotfiles/blob/main/nixos/eve/modules/buildbot.nix)
