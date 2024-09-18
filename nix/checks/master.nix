(import ./lib.nix) {
  name = "master";
  nodes = {
    # `self` here is set by using specialArgs in `lib.nix`
    node1 =
      { self, pkgs, ... }:
      {
        imports = [ self.nixosModules.buildbot-master ];
        services.buildbot-nix.master = {
          enable = true;
          domain = "buildbot2.thalheim.io";
          workersFile = pkgs.writeText "workers.json" ''
            [
              { "name": "eve", "pass": "XXXXXXXXXXXXXXXXXXXX", "cores": 16 }
            ]
          '';
          admins = [ "Mic92" ];
          github = {
            authType.legacy = {
              tokenFile = pkgs.writeText "github-token" "ghp_000000000000000000000000000000000000";
            };
            webhookSecretFile = pkgs.writeText "webhookSecret" "00000000000000000000";
            oauthSecretFile = pkgs.writeText "oauthSecret" "ffffffffffffffffffffffffffffffffffffffff";
            oauthId = "aaaaaaaaaaaaaaaaaaaa";
          };
          cachix = {
            enable = true;
            name = "my-cachix";
            auth.authToken.file = pkgs.writeText "cachixAuthToken" "00000000000000000000";
          };
        };
      };
  };
  # This is the test code that will check if our service is running correctly:
  testScript = ''
    start_all()
    # wait for our service to start
    node1.wait_for_unit("buildbot-master")
    node1.wait_until_succeeds("curl --fail -s --head localhost:8010", timeout=60)
  '';
}
