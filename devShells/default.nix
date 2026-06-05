{
  self,
  pkgs,
  system,
  ...
}:
{
  default = pkgs.mkShell {
    packages = [
      pkgs.bashInteractive
      pkgs.mypy
      pkgs.ruff
      pkgs.postgresql
      pkgs.nix-eval-jobs
      (pkgs.python3.withPackages (
        ps:
        [
          ps.pytest
          ps.pytest-timeout
          ps.pytest-xdist
          ps.playwright
        ]
        ++ self.packages.${system}.buildbot-nix.dependencies
      ))
    ];
    # pkgs.mypy's setup hook disables pytest plugin autoloading, which
    # silently turns off pytest-timeout and pytest-xdist.
    shellHook = ''
      unset PYTEST_DISABLE_PLUGIN_AUTOLOAD
    '';
    env = {
      PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
      PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
    };
  };
}
