{
  flake = {
    nixosModules.buildbot-master = ./master.nix;
    nixosModules.buildbot-worker = ./worker.nix;
  };
}
