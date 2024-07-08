{ inputs, ... }: {
  imports = [
    inputs.treefmt-nix.flakeModule
  ];
  perSystem = { pkgs, ... }: {
    treefmt = {
      projectRootFile = ".git/config";
      programs.nixpkgs-fmt.enable = true;
      programs.shellcheck.enable = true;
      programs.deno.enable = true;
      programs.ruff.check = true;
      programs.ruff.format = true;
      settings.formatter.shellcheck.options = [ "-s" "bash" ];

      programs.mypy = {
        enable = true;
        directories.".".extraPythonPackages = [
          pkgs.python3.pkgs.twisted
        ];
      };

      settings.formatter.ruff-check.priority = 1;
      settings.formatter.ruff-format.priority = 2;
    };
  };
}
