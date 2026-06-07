{
  # https://github.com/Mic92/buildbot-nix
  description = "A nixos module to make buildbot a proper Nix-CI.";

  inputs = {
    nixpkgs.url = "git+https://github.com/NixOS/nixpkgs?shallow=1&ref=nixos-unstable-small";

    # used for development
    treefmt-nix.url = "github:numtide/treefmt-nix";
    treefmt-nix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      treefmt-nix,
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

      nixosModules =
        let
          # Old entry points import the service module (its rename and
          # removed-option stubs handle the options) but warn about the
          # import itself.
          alias = name: {
            key = "buildbot-nix#nixosModules.${name}";
            imports = [ ./nixosModules/buildbot-nix.nix ];
            config.warnings = [
              "buildbot-nix: nixosModules.${name} is deprecated; import nixosModules.buildbot-nix instead"
            ];
          };
        in
        {
          buildbot-nix = ./nixosModules/buildbot-nix.nix;
          buildbot-master = alias "buildbot-master";
          buildbot-worker = alias "buildbot-worker";
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
        inherit self;
        pkgs = nixpkgs.legacyPackages.x86_64-linux;
      };
    };
}
