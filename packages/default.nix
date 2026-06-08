{ lib, pkgs, ... }:
let
  newScope = extra: lib.callPackageWith (pkgs // pkgs.python3.pkgs // extra);

  scope = lib.makeScope newScope (
    self:
    {
      nixbot = self.callPackage ./nixbot.nix { };
    }
    // lib.optionalAttrs pkgs.stdenv.isLinux {
      nixbot-effects = self.callPackage ./nixbot-effects.nix { };
    }
  );
in
# Strip the scope helpers so the flake `packages` output only contains derivations.
lib.filterAttrs (_: lib.isDerivation) scope
