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
  "example-master-${system}" = nixosSystem {
    inherit system;
    modules = [
      dummy
      {
        services.buildbot-nix.master = {
          enable = true;
          domain = "buildbot2.thalheim.io";
          workersFile = "/var/lib/secrets/buildbot-nix/workers.json";
          github = {
            tokenFile = "/var/lib/secrets/buildbot-nix/github-token";
            webhookSecretFile = "/var/lib/secrets/buildbot-nix/github-webhook-secret";
            oauthSecretFile = "/var/lib/secrets/buildbot-nix/github-oauth-secret";
            oauthId = "aaaaaaaaaaaaaaaaaaaa";
            user = "mic92-buildbot";
            admins = [ "Mic92" ];
          };
        };
        services.nginx.virtualHosts."buildbot2.thalheim.io" = {
          enableACME = true;
          forceSSL = true;
        };
        networking.firewall.allowedTCPPorts = [ 80 443 ];
        security.acme.acceptTerms = true;
        security.acme.defaults.email = "joerg.acme@thalheim.io";
      }
      buildbot-nix.nixosModules.buildbot-master
    ];
  };
  "example-worker-${system}" = nixosSystem {
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
