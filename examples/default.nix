{
  nixpkgs,
  system,
  nixbot,
  ...
}:
let
  # some example configuration to make it eval
  dummy =
    { config, ... }:
    {
      config = {
        networking.hostName = "example-common";
        system.stateVersion = config.system.nixos.release;
        users.users.root.initialPassword = "fnord23";
        boot.loader.grub.devices = lib.mkForce [ "/dev/sda" ];
        fileSystems."/" = {
          device = lib.mkDefault "/dev/sda";
          fsType = lib.mkDefault "ext4";
        };
        # ACME needs an accepted CA contract in real deployments.
        security.acme.acceptTerms = true;
        security.acme.defaults.email = "admin@example.org";
      };
    };

  inherit (nixpkgs) lib;
  inherit (lib) nixosSystem;
in
{
  # nixbot is one service; actual builds are offloaded to nix
  # remote builders, so a single machine works well for production.
  "example-nixbot-${system}" = nixosSystem {
    inherit system;
    modules = [
      dummy
      nixbot.nixosModules.nixbot
      ./nixbot.nix
    ];
  };

  # The base example plus OIDC login backed by Authelia.
  "example-nixbot-oidc-authelia-${system}" = nixosSystem {
    inherit system;
    modules = [
      dummy
      nixbot.nixosModules.nixbot
      ./nixbot.nix
      ./oidc-authelia.nix
    ];
  };
}
