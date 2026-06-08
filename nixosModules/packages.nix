{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.services.nixbot.packages;
in
{
  options.services.nixbot.packages = {
    python = lib.mkOption {
      type = lib.types.package;
      default = pkgs.python3;
      defaultText = lib.literalExpression "pkgs.python3";
      description = "Python interpreter to use for nixbot.";
    };

    nixbot = lib.mkOption {
      type = lib.types.package;
      default = cfg.python.pkgs.callPackage ../packages/nixbot.nix { };
      defaultText = lib.literalExpression "python.pkgs.callPackage ../packages/nixbot.nix { }";
      description = "The nixbot package to use.";
    };

    nixbot-effects = lib.mkOption {
      type = lib.types.package;
      default = cfg.python.pkgs.callPackage ../packages/nixbot-effects.nix { };
      defaultText = lib.literalExpression "python.pkgs.callPackage ../packages/nixbot-effects.nix { }";
      description = "The nixbot-effects package to use.";
    };
  };
}
