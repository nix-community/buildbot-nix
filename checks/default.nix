{
  self,
  inputs,
  lib,
  pkgs,
  ...
}:
let
  # this gives us a reference to our flake but also all flake inputs
  checkArgs = {
    inherit self pkgs;
  };
in
{
  treefmt = (inputs.treefmt-nix.lib.evalModule pkgs ../formatter/treefmt.nix).config.build.check self;
}
// lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
  master = import ./master.nix checkArgs;
  worker = import ./worker.nix checkArgs;
  effects = import ./effects.nix checkArgs;
  poller = import ./poller.nix checkArgs;
  gitea = import ./gitea.nix checkArgs;
  scheduled-effects = import ./scheduled-effects.nix checkArgs;
}
