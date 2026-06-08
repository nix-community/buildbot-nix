{
  self,
  pkgs,
  system,
  ...
}:
let
  # process-compose wrapped with the local dev stack config
  # (postgres + nixbot).
  devProcessCompose = pkgs.python3.pkgs.callPackage ../packages/process-compose.nix {
    nixbot = self.packages.${system}.nixbot;
    nixbot-effects = self.packages.${system}.nixbot-effects or null;
  };
in
{
  default = pkgs.mkShell {
    packages = [
      pkgs.bashInteractive
      pkgs.mypy
      pkgs.ruff
      pkgs.postgresql
      pkgs.nix-eval-jobs
      devProcessCompose
      (pkgs.python3.withPackages (
        ps:
        [
          ps.pytest
          ps.pytest-timeout
          ps.pytest-xdist
          ps.pytest-benchmark
          ps.playwright
        ]
        ++ self.packages.${system}.nixbot.dependencies
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
