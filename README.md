# Buildbot-nix

Buildbot-nix is a NixOS module designed to integrate
[Buildbot](https://www.buildbot.net/), a continuous integration (CI) framework,
into the Nix ecosystem. This module is under active development, and while it's
generally stable and widely used, please be aware that some APIs may change over
time.

[Documentation]

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

To set up Buildbot using Buildbot-nix,
read the [documentation].

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

  [documentation]: https://buildbot-nix.readthedocs.io/en/stable/
