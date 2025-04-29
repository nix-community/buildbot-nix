{ self, inputs, ... }:
{
  flake = {
    nixosConfigurations =
      let
        examplesFor =
          system:
          import ../examples {
            inherit system;
            inherit (inputs) nixpkgs;
            buildbot-nix = self;
          };
      in
      examplesFor "x86_64-linux" // examplesFor "aarch64-linux";
  };
}
