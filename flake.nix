{
  # https://github.com/Mic92/buildbot-nix
  description = "A nixos module to make buildbot a proper Nix-CI.";

  inputs = {
    nixpkgs.url = "git+https://github.com/NixOS/nixpkgs?shallow=1&ref=nixos-unstable-small";
    flake-parts.url = "github:hercules-ci/flake-parts";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";

    # used for development
    treefmt-nix.url = "github:numtide/treefmt-nix";
    treefmt-nix.inputs.nixpkgs.follows = "nixpkgs";

    hercules-ci-effects.url = "github:hercules-ci/hercules-ci-effects";
    hercules-ci-effects.inputs.nixpkgs.follows = "nixpkgs";
    hercules-ci-effects.inputs.flake-parts.follows = "flake-parts";
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      imports = [
        ./examples/flake-module.nix
        ./devShells/flake-module.nix
        ./nixosModules/flake-module.nix
        ./nix/flake-module.nix
        ./nix/option-docs.nix
        ./checks/flake-module.nix
        ./packages/flake-module.nix
      ]
      ++ inputs.nixpkgs.lib.optional (inputs.treefmt-nix ? flakeModule) ./formatter/flake-module.nix
      ++ inputs.nixpkgs.lib.optionals (inputs.hercules-ci-effects ? flakeModule) [
        inputs.hercules-ci-effects.flakeModule
        ./herculesCI/flake-module.nix
      ];

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
    };
}
