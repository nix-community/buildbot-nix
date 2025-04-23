{ inputs, ... }:
{
  imports = [ inputs.treefmt-nix.flakeModule ];
  perSystem =
    { pkgs, lib, ... }:
    {
      treefmt = {
        projectRootFile = "LICENSE.md";
        programs.nixfmt.enable = pkgs.lib.meta.availableOn pkgs.stdenv.buildPlatform pkgs.nixfmt-rfc-style.compiler;
        programs.nixfmt.package = pkgs.nixfmt-rfc-style;
        programs.shellcheck.enable = true;
        programs.deno.enable = true;
        programs.ruff.check = true;
        programs.ruff.format = true;
        settings.formatter.shellcheck.options = [
          "-s"
          "bash"
        ];

        programs.mypy = {
          enable = pkgs.stdenv.buildPlatform.isUnix;
          package = pkgs.buildbot.python.pkgs.mypy;
          directories."." = {
            modules = [
              "buildbot_nix"
              "buildbot_effects"
            ];
            extraPythonPackages = [
              (pkgs.python3.pkgs.toPythonModule pkgs.buildbot)
              pkgs.buildbot-worker
              pkgs.python3.pkgs.twisted
              pkgs.python3.pkgs.pydantic
              pkgs.python3.pkgs.pytest
              pkgs.python3.pkgs.zope-interface
              pkgs.python3.pkgs.types-requests
            ];
          };
        };

        # the mypy module adds `./buildbot_nix/**/*.py` which does not appear to work
        # furthermore, saying `directories.""` will lead to `/buildbot_nix/**/*.py` which
        # is obviously incorrect...
        settings.formatter."mypy-" = lib.mkIf pkgs.stdenv.buildPlatform.isUnix {
          includes = [
            "buildbot_nix/**/*.py"
            "buildbot_effects/**/*.py"
          ];
        };
        settings.formatter.ruff-check.priority = 1;
        settings.formatter.ruff-format.priority = 2;
      };
    };
}
