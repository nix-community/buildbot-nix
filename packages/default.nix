{ lib, pkgs, ... }:
let
  # A scope whose `callPackage` can resolve names from both the top-level
  # package set and `python3.pkgs`, since the packages here mix regular
  # derivations with Python modules.
  newScope = extra: lib.callPackageWith (pkgs // pkgs.python3.pkgs // extra);

  scope = lib.makeScope newScope (
    self:
    {
      # useful for checking what buildbot version is used.
      buildbot = self.callPackage ./buildbot.nix { inherit pkgs; };
      buildbot-dev = self.callPackage ./buildbot-dev.nix {
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
