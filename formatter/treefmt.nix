{ pkgs, lib, ... }:
{
  projectRootFile = "LICENSE.md";
  programs.nixfmt.enable = pkgs.lib.meta.availableOn pkgs.stdenv.buildPlatform pkgs.nixfmt.compiler;
  programs.nixfmt.package = pkgs.nixfmt;
  programs.shellcheck.enable = true;
  programs.deno.enable = true;
  # deno fmt cannot parse Jinja syntax in the service's HTML templates
  # (djlint handles those) and biome owns the stylesheet.
  settings.formatter.deno.excludes = [
    "nixbot/nixbot/web/templates/*"
    "nixbot/nixbot/web/static/*.css"
    # Vendored third-party code stays byte-identical to upstream.
    "nixbot/nixbot/web/static/vendor/*"
  ];

  # Jinja-aware HTML formatter + linter; config in nixbot/pyproject.toml.
  settings.formatter.djlint = {
    command = lib.getExe (
      pkgs.writeShellScriptBin "djlint-fmt" ''
        ${lib.getExe pkgs.djlint} "$@" --reformat \
          --configuration nixbot/pyproject.toml --quiet
        status=$?
        # 1 means files were reformatted, which is fine for a formatter.
        [ "$status" -le 1 ] || exit "$status"
        exec ${lib.getExe pkgs.djlint} "$@" --lint \
          --configuration nixbot/pyproject.toml
      ''
    );
    includes = [ "nixbot/nixbot/web/templates/*.html" ];
  };

  settings.formatter.biome-css = {
    command = lib.getExe (
      pkgs.writeShellScriptBin "biome-css" ''
        ${lib.getExe pkgs.biome} format --write --no-errors-on-unmatched "$@"
        exec ${lib.getExe pkgs.biome} lint --error-on-warnings --no-errors-on-unmatched "$@"
      ''
    );
    includes = [ "*.css" ];
  };
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
        "nixbot"
        "nixbot_effects"
      ];
      extraPythonPackages = [
        pkgs.python3.pkgs.pydantic
        pkgs.python3.pkgs.pytest
        pkgs.python3.pkgs.pytest-benchmark
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

  # the mypy module adds `./nixbot/**/*.py` which does not appear to work
  # furthermore, saying `directories.""` will lead to `/nixbot/**/*.py` which
  # is obviously incorrect...
  settings.formatter."mypy-" = lib.mkIf pkgs.stdenv.buildPlatform.isLinux {
    includes = [
      "nixbot/**/*.py"
      "nixbot_effects/**/*.py"
    ];
  };
  settings.formatter.ruff-check.priority = 1;
  settings.formatter.ruff-format.priority = 2;
}
