{
  perSystem =
    { pkgs, config, ... }:
    {
      devShells.default = pkgs.mkShell {
        packages = [
          pkgs.bashInteractive
          pkgs.mypy
          pkgs.ruff
          (pkgs.python3.withPackages (
            ps:
            [
              ps.pytest
              (ps.toPythonModule pkgs.buildbot)
              (ps.toPythonModule pkgs.buildbot-worker)
            ]
            ++ config.packages.buildbot-nix.dependencies
          ))
        ];
      };
    };
}
