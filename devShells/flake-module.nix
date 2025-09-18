{
  perSystem =
    { pkgs, self', ... }:
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
            ++ self'.packages.buildbot-nix.dependencies
          ))
        ];
      };
    };
}
