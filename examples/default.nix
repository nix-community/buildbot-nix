{ nixpkgs, system, srvos, buildbot-nix, disko, ... }:
let
  # some example configuration to make it eval
  dummy = { config, modulesPath, ... }: {
    imports = [
      #srvos.nixosModules.server
      #srvos.nixosModules.hardware-hetzner-cloud
      disko.nixosModules.disko
      ./disko.nix
      "${modulesPath}/profiles/qemu-guest.nix"
    ];
    config = {
      networking.hostName = "example-common";
      system.stateVersion = config.system.nixos.version;
      services.openssh.enable = true;
      users.users.root.initialPassword = "fnord23";
      users.users.root.openssh.authorizedKeys.keys = [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKbBp2dH2X3dcU1zh+xW3ZsdYROKpJd3n13ssOP092qE joerg@turingmachine"
      ];

      #users.users.root.initialPassword = "fnord23";
      #boot.loader.grub.devices = lib.mkForce [ "/dev/sda" ];
      #fileSystems."/".device = lib.mkDefault "/dev/sda";

      #systemd.network.networks."10-uplink".networkConfig.Address = [ "2a01:4f9:c012:539b::/64" ];
    };
  };

  inherit (nixpkgs) lib;
  inherit (lib) nixosSystem;
in
{
  example-master = nixosSystem {
    inherit system;
    modules = [
      dummy
      {
        services.buildbot-nix.master = {
          enable = true;
          url = "https://buildbot.thalheim.io";
          workersFile = "/var/lib/secrets/buildbot-nix/workers.json";
          github = {
            tokenFile = "/var/lib/secrets/buildbot-nix/github-token";
            webhookSecretFile = "/var/lib/secrets/buildbot-nix/github-webhook-secret";
            oauthSecretFile = "/var/lib/secrets/buildbot-nix/github-oauth-secret";
            oauthId = "2516248ec6289e4d9818122cce0cbde39e4b788d";
            githubUser = "mic92-buildbot";
            githubAdmins = [ "Mic92" ];
          };
        };
      }
      buildbot-nix.nixosModules.buildbot-master
    ];
  };
  example-worker = nixosSystem {
    inherit system;
    modules = [
      dummy
      {
        services.buildbot-nix.worker = {
          enable = true;
          workerPasswordFile = "/var/lib/secrets/buildbot-nix/worker-password";
        };
      }
      buildbot-nix.nixosModules.buildbot-worker
    ];
  };
}
