{
  perSystem =
    { pkgs, self', ... }:
    {
      devShells.default = pkgs.mkShell {
        packages = [
          pkgs.bashInteractive
          pkgs.mypy
          pkgs.ruff
          self'.packages.buildbot
        ];
      };
    };
}
