{ nixpkgs, system, buildbot-nix, ... }:
let
  # some example configuration to make it eval
  dummy = { config, ... }: {
    config = {
      networking.hostName = "example-common";
      system.stateVersion = config.system.nixos.version;
      users.users.root.initialPassword = "fnord23";
      boot.loader.grub.devices = lib.mkForce [ "/dev/sda" ];
      fileSystems."/".device = lib.mkDefault "/dev/sda";
    };
  };

  inherit (nixpkgs) lib;
  inherit (lib) nixosSystem;
in
{
  # This runs both master and worker on the same machine.
  # As the actual build is offloaded with remote builder,
  # this also works well for production setups.
  "example-master-worker-combined-${system}" = nixosSystem {
    inherit system;
    modules = [
      dummy
      buildbot-nix.nixosModules.buildbot-master
      buildbot-nix.nixosModules.buildbot-worker
      ./master.nix
      ./worker.nix
    ];
  };
  "example-master-${system}" = nixosSystem {
    inherit system;
    modules = [
      dummy
      buildbot-nix.nixosModules.buildbot-master
      ./master.nix
      # When master and worker run on different machines,
      # we need to configure the master to listen on a public address.
      # Also check out the buildbot upstream documentation on the master-worker protocol,
      # including tls encryption
      {
        # exposes the master build protocol on port 9989
        services.buildbot-master.extraConfig = ''
          c["protocols"] = {"pb": {"port": "tcp:9989:interface=\\:\\:"}}
        '';
        networking.firewall.allowedTCPPorts = [ 9989 ];
      }
    ];
  };

  "example-worker-${system}" = nixosSystem {
    inherit system;
    modules = [
      dummy
      buildbot-nix.nixosModules.buildbot-worker
      ./worker.nix
      # Connects to a master on the ipv6 address 2a09:80c0:102::1
      { services.buildbot-nix.worker.masterUrl = ''tcp:host=2a09\:80c0\:102\:\:1:port=9989''; }
    ];
  };
}
