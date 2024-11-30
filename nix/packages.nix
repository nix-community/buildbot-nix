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
      defaultText = "pkgs.python3";
      description = "python interpreter to use for buildbot-nix";
    };

    buildbot = lib.mkOption {
      type = lib.types.package;
      default = pkgs.buildbot;
    };

    buildbot-worker = lib.mkOption {
      type = lib.types.package;
      default = pkgs.buildbot-worker;
    };

    buildbot-nix = lib.mkOption {
      default = cfg.python.pkgs.callPackage ../default.nix { };
    };

    buildbot-plugins = lib.mkOption {
      type = lib.types.attrsOf lib.types.package;
      default = pkgs.buildbot-plugins;
    };

    buildbot-effects = lib.mkOption {
      type = lib.types.package;
      default = cfg.python.pkgs.callPackage ./buildbot-effects.nix { };
    };

    buildbot-gitea = lib.mkOption {
      default =
        (cfg.python.pkgs.callPackage ./buildbot-gitea.nix {
          buildbot = cfg.buildbot;
        }).overrideAttrs
          (old: {
            patches = old.patches ++ [
              ./0002-GiteaHandler-set-branch-to-the-PR-branch-not-the-bas.patch
              ./0001-GiteaHandler-set-the-category-of-PR-changes-to-pull-.patch
            ];
          });
    };
  };
}
