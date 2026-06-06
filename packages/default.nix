{ lib, pkgs, ... }:
let
  newScope = extra: lib.callPackageWith (pkgs // pkgs.python3.pkgs // extra);

  scope = lib.makeScope newScope (
    self:
    {
      buildbot-nix = self.callPackage ./buildbot-nix.nix { };
    }
    // lib.optionalAttrs pkgs.stdenv.isLinux {
      buildbot-effects = self.callPackage ./buildbot-effects.nix { };
    }
  );
in
# Strip the scope helpers so the flake `packages` output only contains derivations.
lib.filterAttrs (_: lib.isDerivation) scope
