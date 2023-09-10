{ nixpkgs, system, buildbot-nix, ... }:
let
  # some example configuration to make it eval
  dummy = { config, ... }: {
    networking.hostName = "example-common";
    system.stateVersion = config.system.nixos.version;
    users.users.root.initialPassword = "fnord23";
    boot.loader.grub.devices = lib.mkForce [ "/dev/sda" ];
    fileSystems."/".device = lib.mkDefault "/dev/sda";
  };

  inherit (nixpkgs) lib;
  inherit (lib) nixosSystem;

in
{
  # General
  example-master = nixosSystem {
    inherit system;
    modules = [
      dummy
      buildbot-nix.nixosModules.buildbot-master
    ];
  };
  example-worker = nixosSystem {
    inherit system;
    modules = [
      dummy
      buildbot-nix.nixosModules.buildbot-worker
    ];
  };
}
