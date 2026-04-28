{ lib, pkgs, ... }:
let
  buildbotPackages = pkgs.callPackage ./buildbot-packages.nix { };

  # All python packages here must come from the same python interpreter as
  # `buildbot`, which uses an overridden python3 (patched twisted) from
  # buildbot-packages.nix.  Resolving them from `pkgs.python3.pkgs` instead
  # pulls in a second, unpatched twisted/treq and trips the python
  # runtime-deps duplicate check.
  newScope = extra: lib.callPackageWith (pkgs // buildbotPackages.python.pkgs // extra);

  scope = lib.makeScope newScope (
    self:
    {
      # useful for checking what buildbot version is used.
      inherit (buildbotPackages) buildbot;
      buildbot-dev = self.callPackage ./buildbot-dev.nix {
        inherit (buildbotPackages) buildbot-worker buildbot-plugins python;
        buildbot-effects = self.buildbot-effects or null;
      };
      buildbot-nix = self.callPackage ./buildbot-nix.nix { };
      buildbot-gitea = self.callPackage ./buildbot-gitea.nix { };
    }
    // lib.optionalAttrs pkgs.stdenv.isLinux {
      buildbot-effects = self.callPackage ./buildbot-effects.nix { };
    }
  );
in
# Strip the scope helpers so the flake `packages` output only contains derivations.
lib.filterAttrs (_: lib.isDerivation) scope
