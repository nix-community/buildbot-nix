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
  nixbot-tests = self.packages.${pkgs.stdenv.hostPlatform.system}.nixbot.tests.pytest;
}
// lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
  nixbot = import ./nixbot.nix checkArgs;
  nixbot-gitlab = import ./nixbot-gitlab.nix checkArgs;
}
