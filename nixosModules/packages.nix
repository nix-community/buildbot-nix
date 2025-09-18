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
      default = config.services.buildbot-nix.packages.buildbot.python;
      defaultText = "pkgs.python3";
      description = "python interpreter to use for buildbot-nix";
    };

    buildbot = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../packages/buildbot.nix { };
    };

    buildbot-worker = lib.mkOption {
      type = lib.types.package;
      default = pkgs.buildbot-worker;
    };

    buildbot-nix = lib.mkOption {
      default = cfg.python.pkgs.callPackage ../packages/buildbot-nix.nix {
        buildbot-gitea = cfg.buildbot-gitea;
      };
    };

    buildbot-plugins = lib.mkOption {
      type = lib.types.attrsOf lib.types.package;
      default = pkgs.buildbot-plugins;
    };

    buildbot-effects = lib.mkOption {
      type = lib.types.package;
      default = cfg.python.pkgs.callPackage ../packages/buildbot-effects.nix { };
    };

    buildbot-gitea = lib.mkOption {
      default = (
        cfg.python.pkgs.callPackage ../packages/buildbot-gitea.nix {
          buildbot = cfg.buildbot;
        }
      );
    };
  };
}
