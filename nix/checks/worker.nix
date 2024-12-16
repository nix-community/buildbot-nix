(import ./lib.nix) {
  name = "worker";
  nodes = {
    # `self` here is set by using specialArgs in `lib.nix`
    node1 =
      {
        self,
        pkgs,
        ...
      }:
      {
        imports = [ self.nixosModules.buildbot-worker ];
        services.buildbot-nix.worker = {
          enable = true;
          workerPasswordFile = pkgs.writeText "password" "password";
        };
      };
  };
  # This is the test code that will check if our service is running correctly:
  testScript = ''
    start_all()
    # wait for our service to start
    node1.wait_for_unit("buildbot-worker")
  '';
}
