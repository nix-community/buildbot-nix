{ pkgs, ... }:
{
  services.buildbot-nix.worker = {
    enable = true;
    workerPasswordFile = pkgs.writeText "worker-password-file" "XXXXXXXXXXXXXXXXXXXX";
  };
}
