{
  # https://github.com/Mic92/buildbot-nix
  description = "A nixos module to make buildbot a proper Nix-CI.";

  inputs = {
    nixpkgs.url = "github:Nixos/nixpkgs/nixos-unstable-small";
    flake-parts.url = "github:hercules-ci/flake-parts";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";

    # used for development
    treefmt-nix.url = "github:numtide/treefmt-nix";
    treefmt-nix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    inputs@{ self, flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } (
      { lib, ... }:
      {
        imports = [
          ./nix/checks/flake-module.nix
        ] ++ inputs.nixpkgs.lib.optional (inputs.treefmt-nix ? flakeModule) ./nix/treefmt/flake-module.nix;
        systems = [ "x86_64-linux" ];
        flake = {
          nixosModules.buildbot-master.imports = [
            ./nix/master.nix
            (
              { pkgs, ... }:
              {
                services.buildbot-nix.master.buildbotNixpkgs =
                  lib.mkDefault
                    inputs.nixpkgs.legacyPackages.${pkgs.hostPlatform.system};
              }
            )
          ];
          nixosModules.buildbot-worker.imports = [
            ./nix/worker.nix
            (
              { pkgs, ... }:
              {
                services.buildbot-nix.worker.buildbotNixpkgs =
                  lib.mkDefault
                    inputs.nixpkgs.legacyPackages.${pkgs.hostPlatform.system};
              }
            )
          ];

          nixosConfigurations =
            let
              examplesFor =
                system:
                import ./examples {
                  inherit system;
                  inherit (inputs) nixpkgs;
                  buildbot-nix = self;
                };
            in
            examplesFor "x86_64-linux" // examplesFor "aarch64-linux";

          lib = {
            interpolate = value: {
              _type = "interpolate";
              inherit value;
            };
          };
        };
        perSystem =
          {
            self',
            pkgs,
            system,
            ...
          }:
          {
            packages.default = pkgs.mkShell {
              packages = [
                pkgs.bashInteractive
                pkgs.mypy
                pkgs.ruff
              ];
            };
            packages.buildbot-nix = pkgs.python3.pkgs.callPackage ./default.nix { };
            packages.buildbot-effects = pkgs.python3.pkgs.callPackage ./nix/buildbot-effects.nix { };
            checks =
              let
                nixosMachines = lib.mapAttrs' (
                  name: config: lib.nameValuePair "nixos-${name}" config.config.system.build.toplevel
                ) ((lib.filterAttrs (_: config: config.pkgs.system == system)) self.nixosConfigurations);
                packages = lib.mapAttrs' (n: lib.nameValuePair "package-${n}") self'.packages;
                devShells = lib.mapAttrs' (n: lib.nameValuePair "devShell-${n}") self'.devShells;
              in
              nixosMachines // packages // devShells;
          };
      }
    );
}
