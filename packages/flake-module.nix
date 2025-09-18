{
  perSystem =
    {
      lib,
      pkgs,
      config,
      ...
    }:
    {
      packages = {
        # useful for checking what buildbot version is used.
        buildbot = pkgs.callPackage ./buildbot.nix { };
        buildbot-dev = pkgs.python3.pkgs.callPackage ./buildbot-dev.nix {
          inherit (config.packages)
            buildbot-gitea
            buildbot-nix
            buildbot
            ;
          buildbot-effects = config.packages.buildbot-effects or null;
          inherit (pkgs) buildbot-worker buildbot-plugins;
        };
        buildbot-nix = pkgs.python3.pkgs.callPackage ./buildbot-nix.nix {
          buildbot-gitea = config.packages.buildbot-gitea;
        };
        buildbot-gitea = pkgs.python3.pkgs.callPackage ./buildbot-gitea.nix {
          buildbot = config.packages.buildbot;
        };
      }
      // lib.optionalAttrs pkgs.stdenv.isLinux {
        buildbot-effects = pkgs.python3.pkgs.callPackage ./buildbot-effects.nix { };
      };
    };
}
