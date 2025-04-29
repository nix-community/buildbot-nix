{
  perSystem =
    {
      lib,
      pkgs,
      ...
    }:
    {
      packages =
        {
          # useful for checking what buildbot version is used.
          buildbot = pkgs.callPackage ./buildbot.nix { };
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
