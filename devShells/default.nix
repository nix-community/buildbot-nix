{
  self,
  pkgs,
  system,
  ...
}:
let
  # Reuse the python interpreter that the buildbot packages are built against
  # so the dev shell closure does not mix the patched and unpatched twisted.
  buildbotPackages = pkgs.callPackage ../packages/buildbot-packages.nix { };
in
{
  default = pkgs.mkShell {
    packages = [
      pkgs.bashInteractive
      pkgs.mypy
      pkgs.ruff
      (buildbotPackages.python.withPackages (
        ps:
        [
          ps.pytest
          (ps.toPythonModule buildbotPackages.buildbot)
          (ps.toPythonModule buildbotPackages.buildbot-worker)
        ]
        ++ self.packages.${system}.buildbot-nix.dependencies
      ))
    ];
  };
}
