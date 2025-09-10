{
  perSystem =
    {
      lib,
      pkgs,
      self',
      ...
    }:
    {
      packages = {
        # useful for checking what buildbot version is used.
        buildbot = pkgs.callPackage ./buildbot.nix { };
        buildbot-dev = pkgs.python3.pkgs.callPackage ./buildbot-dev.nix {
          inherit (self'.packages)
            buildbot-gitea
            buildbot-nix
            buildbot
            ;
          buildbot-effects = self'.packages.buildbot-effects or null;
          inherit (pkgs) buildbot-worker buildbot-plugins;
        };
        buildbot-nix = pkgs.python3.pkgs.callPackage ./buildbot-nix.nix { };
        buildbot-gitea = pkgs.python3.pkgs.callPackage ./buildbot-gitea.nix {
          buildbot = pkgs.buildbot;
        };
      }
      // lib.optionalAttrs pkgs.stdenv.isLinux {
        buildbot-effects = pkgs.python3.pkgs.callPackage ./buildbot-effects.nix { };
      };
    };
}
