(import ./lib.nix) {
  name = "from-nixos";
  nodes = {
    # `self` here is set by using specialArgs in `lib.nix`
    node1 = { self, ... }: {
      imports = [
        self.nixosModules.buildbot-master
      ];
      services.buildbot-nix.master = {
        enable = true;
        domain = "buildbot2.thalheim.io";
        workersFile = "/var/lib/secrets/buildbot-nix/workers.json";
        github = {
          tokenFile = "/var/lib/secrets/buildbot-nix/github-token";
          webhookSecretFile = "/var/lib/secrets/buildbot-nix/github-webhook-secret";
          oauthSecretFile = "/var/lib/secrets/buildbot-nix/github-oauth-secret";
          oauthId = "aaaaaaaaaaaaaaaaaaaa";
          githubUser = "mic92-buildbot";
          githubAdmins = [ "Mic92" ];
        };
      };
    };
  };
  # This is the test code that will check if our service is running correctly:
  testScript = ''
    start_all()
    # wait for our service to start
    node1.wait_for_unit("buildbot-master")
  '';
}
