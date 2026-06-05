{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.services.buildbot-nix.packages;
in
{
  options.services.buildbot-nix.packages = {
    python = lib.mkOption {
      type = lib.types.package;
      default = pkgs.python3;
      defaultText = lib.literalExpression "pkgs.python3";
      description = "Python interpreter to use for buildbot-nix.";
    };

    buildbot-nix = lib.mkOption {
      type = lib.types.package;
      default = cfg.python.pkgs.callPackage ../packages/buildbot-nix.nix { };
      defaultText = lib.literalExpression "python.pkgs.callPackage ../packages/buildbot-nix.nix { }";
      description = "The buildbot-nix engine package to use.";
    };

    buildbot-effects = lib.mkOption {
      type = lib.types.package;
      default = cfg.python.pkgs.callPackage ../packages/buildbot-effects.nix { };
      defaultText = lib.literalExpression "python.pkgs.callPackage ../packages/buildbot-effects.nix { }";
      description = "The buildbot-effects package to use.";
    };
  };
}
