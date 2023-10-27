{
  # https://github.com/Mic92/buildbot-nix
  description = "A nixos module to make buildbot a proper Nix-CI.";

  inputs = {
    nixpkgs.url = "github:Nixos/nixpkgs/nixpkgs-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";

    # used for development
    treefmt-nix.url = "github:numtide/treefmt-nix";
    treefmt-nix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = inputs@{ self, flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } ({ lib, ... }:
      {
        imports = [
          ./nix/checks/flake-module.nix
        ] ++ inputs.nixpkgs.lib.optional (inputs.treefmt-nix ? flakeModule) ./nix/treefmt/flake-module.nix;
        systems = [ "x86_64-linux" ];
        flake = {
          nixosModules.buildbot-master = ./nix/master.nix;
          nixosModules.buildbot-worker = ./nix/worker.nix;

          nixosConfigurations =
            let
              examplesFor = system: import ./examples {
                inherit system;
                inherit (inputs) nixpkgs;
                buildbot-nix = self;
              };
            in
            examplesFor "x86_64-linux" // examplesFor "aarch64-linux";
        };
        perSystem = { self', pkgs, system, ... }: {
          packages.default = pkgs.mkShell {
            packages = [
              pkgs.bashInteractive
            ];
          };
          checks =
            let
              nixosMachines = lib.mapAttrs' (name: config: lib.nameValuePair "nixos-${name}" config.config.system.build.toplevel) ((lib.filterAttrs (_: config: config.pkgs.system == system)) self.nixosConfigurations);
              packages = lib.mapAttrs' (n: lib.nameValuePair "package-${n}") self'.packages;
              devShells = lib.mapAttrs' (n: lib.nameValuePair "devShell-${n}") self'.devShells;
            in
            nixosMachines // packages // devShells;
        };
      });
}
