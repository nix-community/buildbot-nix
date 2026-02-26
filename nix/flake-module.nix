{ inputs, lib, ... }:
{
  flake = {
    lib = import ./lib.nix;
    clan.modules."buildbot" = lib.modules.importApply ./clan-module.nix { inherit inputs; };
  };
}
