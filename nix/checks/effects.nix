(import ./lib.nix) {
  name = "effects";
  nodes = {
    # `self` here is set by using specialArgs in `lib.nix`
    node1 =
      { self, pkgs, ... }:
      {
        environment.systemPackages = [
          (pkgs.python3.pkgs.callPackage ../../nix/buildbot-effects.nix { })
        ];
      };
  };
  testScript = ''
    start_all()
    # wait for our service to start
    node1.succeed("buildbot-effects --help")
  '';
}
