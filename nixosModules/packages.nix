{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.services.buildbot-nix.packages;
  # Use our patched buildbot packages scope which includes patched twisted
  buildbotPackages = pkgs.callPackage ../packages/buildbot-packages.nix { };
in
{
  options.services.buildbot-nix.packages = {
    python = lib.mkOption {
      type = lib.types.package;
      default = buildbotPackages.python;
      defaultText = lib.literalExpression "buildbotPackages.python";
      description = "Python interpreter to use for buildbot-nix.";
    };

    buildbot = lib.mkOption {
      type = lib.types.package;
      default = buildbotPackages.buildbot;
      defaultText = lib.literalExpression "buildbotPackages.buildbot";
      description = "The buildbot package to use.";
    };

    buildbot-worker = lib.mkOption {
      type = lib.types.package;
      default = buildbotPackages.buildbot-worker;
      defaultText = lib.literalExpression "buildbotPackages.buildbot-worker";
      description = "The buildbot-worker package to use.";
    };

    buildbot-nix = lib.mkOption {
      type = lib.types.package;
      default = cfg.python.pkgs.callPackage ../packages/buildbot-nix.nix {
        buildbot-gitea = cfg.buildbot-gitea;
      };
      defaultText = lib.literalExpression "python.pkgs.callPackage ../packages/buildbot-nix.nix { }";
      description = "The buildbot-nix package to use.";
    };

    buildbot-plugins = lib.mkOption {
      type = lib.types.attrsOf lib.types.package;
      default = buildbotPackages.buildbot-plugins;
      defaultText = lib.literalExpression "buildbotPackages.buildbot-plugins";
      description = "Attrset of buildbot plugin packages to use.";
    };

    buildbot-effects = lib.mkOption {
      type = lib.types.package;
      default = cfg.python.pkgs.callPackage ../packages/buildbot-effects.nix { };
      defaultText = lib.literalExpression "python.pkgs.callPackage ../packages/buildbot-effects.nix { }";
      description = "The buildbot-effects package to use.";
    };

    buildbot-gitea = lib.mkOption {
      type = lib.types.package;
      default = (
        cfg.python.pkgs.callPackage ../packages/buildbot-gitea.nix {
          buildbot = cfg.buildbot;
        }
      );
      defaultText = lib.literalExpression "python.pkgs.callPackage ../packages/buildbot-gitea.nix { }";
      description = "The buildbot-gitea package to use.";
    };
  };
}
