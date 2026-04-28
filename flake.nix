{
  # https://github.com/Mic92/buildbot-nix
  description = "A nixos module to make buildbot a proper Nix-CI.";

  inputs = {
    nixpkgs.url = "git+https://github.com/NixOS/nixpkgs?shallow=1&ref=nixos-unstable-small";

    # used for development
    treefmt-nix.url = "github:numtide/treefmt-nix";
    treefmt-nix.inputs.nixpkgs.follows = "nixpkgs";

    hercules-ci-effects.url = "github:hercules-ci/hercules-ci-effects";
    hercules-ci-effects.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      treefmt-nix,
      hercules-ci-effects,
      ...
    }:
    let
      inherit (nixpkgs) lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      eachSystem =
        f:
        lib.genAttrs systems (
          system:
          f {
            inherit
              self
              inputs
              lib
              system
              ;
            pkgs = nixpkgs.legacyPackages.${system};
          }
        );
    in
    {
      lib = import ./nix/lib.nix;

      nixosModules = {
        buildbot-master = ./nixosModules/master.nix;
        buildbot-worker = ./nixosModules/worker.nix;
      };

      nixosConfigurations =
        let
          examplesFor =
            system:
            import ./examples {
              inherit system nixpkgs;
              buildbot-nix = self;
            };
        in
        examplesFor "x86_64-linux" // examplesFor "aarch64-linux";

      packages = eachSystem (import ./packages);

      devShells = eachSystem (import ./devShells);

      checks = eachSystem (import ./checks);

      formatter = eachSystem (
        { pkgs, ... }: (treefmt-nix.lib.evalModule pkgs ./formatter/treefmt.nix).config.build.wrapper
      );

      herculesCI = import ./herculesCI {
        inherit self hercules-ci-effects;
        pkgs = nixpkgs.legacyPackages.x86_64-linux;
      };
    };
}
