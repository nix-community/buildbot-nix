{ pkgs, lib, ... }:
{
  projectRootFile = "LICENSE.md";
  programs.nixfmt.enable = pkgs.lib.meta.availableOn pkgs.stdenv.buildPlatform pkgs.nixfmt.compiler;
  programs.nixfmt.package = pkgs.nixfmt;
  programs.shellcheck.enable = true;
  programs.deno.enable = true;
  # deno fmt cannot parse Jinja syntax in the engine's HTML templates.
  settings.formatter.deno.excludes = [
    "buildbot_nix/buildbot_nix/engine/web/templates/*"
  ];
  programs.ruff.check = true;
  programs.ruff.format = true;
  settings.formatter.shellcheck.options = [
    "-s"
    "bash"
  ];

  programs.mypy = {
    enable = pkgs.stdenv.buildPlatform.isLinux;
    package = pkgs.python3.pkgs.mypy;
    directories."." = {
      modules = [
        "buildbot_nix"
        "buildbot_effects"
      ];
      extraPythonPackages = [
        pkgs.python3.pkgs.pydantic
        pkgs.python3.pkgs.pytest
        pkgs.python3.pkgs.httpx
        pkgs.python3.pkgs.fastapi
        pkgs.python3.pkgs.uvicorn
        pkgs.python3.pkgs.asyncpg
        pkgs.python3.pkgs.jinja2
        pkgs.python3.pkgs.zstandard
        pkgs.python3.pkgs.python-multipart
        pkgs.python3.pkgs.playwright
      ];
    };
  };

  # the mypy module adds `./buildbot_nix/**/*.py` which does not appear to work
  # furthermore, saying `directories.""` will lead to `/buildbot_nix/**/*.py` which
  # is obviously incorrect...
  settings.formatter."mypy-" = lib.mkIf pkgs.stdenv.buildPlatform.isLinux {
    includes = [
      "buildbot_nix/**/*.py"
      "buildbot_effects/**/*.py"
    ];
  };
  settings.formatter.ruff-check.priority = 1;
  settings.formatter.ruff-format.priority = 2;
}
