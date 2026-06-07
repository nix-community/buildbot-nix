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
  buildbot-nix = import ./buildbot-nix.nix checkArgs;
}
