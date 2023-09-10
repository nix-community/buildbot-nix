{ nixpkgs, system, buildbot-nix, ... }:
let
  # some example configuration to make it eval
  dummy = { config, modulesPath, ... }: {
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
  example-master = lib.makeOverridable nixosSystem {
    inherit system;
    modules = [
      dummy
      { 
        services.buildbot-nix.master = {
          enable = true; 
          url = "https://buildbot.thalheim.io";
          workersFile = "/home/mic92/buildbot-nix/workers.json";
          github = {
            tokenFile = "/home/mic92/git/buildbot-nix/github-token";
            webhookSecretFile = "/home/mic92/buildbot-nix/github-webhook-secret";
            oauthSecretFile = "/home/mic92/buildbot-nix/github-oauth-secret";
            oauthId = "2516248ec6289e4d9818122cce0cbde39e4b788d";
            githubUser = "mic92-buildbot";
            githubAdmins = [ "Mic92" ];
          };
        };
      }
      buildbot-nix.nixosModules.buildbot-master
    ];
  };
  example-worker = lib.makeOverridable nixosSystem {
    inherit system;
    modules = [
      dummy
      { 
        services.buildbot-nix.worker = {
          enable = true;
          workerPasswordFile = "/home/mic92/buildbot-nix/worker-password";
        };
      }
      buildbot-nix.nixosModules.buildbot-worker
    ];
  };
}
