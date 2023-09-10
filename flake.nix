{
  # https://github.com/Mic92/buildbot-nix
  description = "A nixos module to make buildbot a proper Nix-CI.";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    srvos.url = "github:numtide/srvos";
    srvos.inputs.nixpkgs.follows = "nixpkgs";
    disko.url = "github:nix-community/disko";
    disko.inputs.nixpkgs.follows = "nixpkgs";
    flake-parts.url = "github:hercules-ci/flake-parts";
  };

  outputs = inputs@{ self, srvos, flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } ({ lib, ... }: {
      systems = [ "x86_64-linux" ];
      flake = {
        nixosModules.buildbot-master = ./nix/master.nix;
        nixosModules.buildbot-worker = ./nix/worker.nix;

        nixosConfigurations = import ./examples {
          inherit (inputs) nixpkgs srvos disko;
          buildbot-nix = self;
          system = "x86_64-linux";
        };
        checks.x86_64-linux = {
          nixos-master = self.nixosConfigurations.example-master.config.system.build.toplevel;
          nixos-worker = self.nixosConfigurations.example-worker.config.system.build.toplevel;
        };
      };
      perSystem = { pkgs, system, ... }: {
        packages.default = pkgs.mkShell {
          packages = [
            pkgs.bashInteractive
          ];
        };
      };
    });
}
