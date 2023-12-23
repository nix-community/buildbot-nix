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
      ({ pkgs, ... }: {
        services.buildbot-nix.master = {
          enable = true;
          domain = "buildbot2.thalheim.io";
          workersFile = pkgs.writeText "workers.json" ''
            [
              { "name": "eve", "pass": "XXXXXXXXXXXXXXXXXXXX", "cores": 16 }
            ]
          '';
          github = {
            tokenFile = pkgs.writeText "github-token" "ghp_000000000000000000000000000000000000";
            webhookSecretFile = pkgs.writeText "webhookSecret" "00000000000000000000";
            oauthSecretFile = pkgs.writeText "oauthSecret" "ffffffffffffffffffffffffffffffffffffffff";
            oauthId = "aaaaaaaaaaaaaaaaaaaa";
            user = "mic92-buildbot";
            admins = [ "Mic92" ];
          };
          # optional expose latest store path as text file
          # outputsPath = "/var/www/buildbot/nix-outputs";

          # optional nix-eval-jobs settings
          # evalWorkerCount = 8; # limit number of concurrent evaluations
          # evalMaxMemorySize = "2048"; # limit memory usage per evaluation

          # optional cachix
          #cachix = {
          #  name = "my-cachix";
          #  # One of the following is required:
          #  signingKey = "/var/lib/secrets/cachix-key";
          #  authToken = "/var/lib/secrets/cachix-token";
          #};
        };
      })
      buildbot-nix.nixosModules.buildbot-master
    ];
  };
  "example-worker-${system}" = nixosSystem {
    inherit system;
    modules = [
      dummy
      ({ pkgs, ... }: {
        services.buildbot-nix.worker = {
          enable = true;
          workerPasswordFile = pkgs.writeText "worker-password-file" "";
        };
      })
      buildbot-nix.nixosModules.buildbot-worker
    ];
  };
}
