{
  # https://github.com/Mic92/buildbot-nix
  description = "A nixos module to make buildbot a proper Nix-CI.";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
  };

  outputs = inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } ({ lib, ... }: {
      systems = lib.systems.flakeExposed;
      flake = {
        nixosModules.buildbot-master = ./nix/master.nix;
        nixosModules.buildbot-worker = ./nix/worker.nix;
      };
      perSystem = { pkgs, ... }: {
        packages.default = pkgs.mkShell {
          packages = [
            pkgs.bashInteractive
          ];
        };
      };
    });
}
