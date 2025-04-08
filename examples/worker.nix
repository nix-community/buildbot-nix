{ pkgs, ... }:
{
  services.buildbot-nix.worker = {
    enable = true;
    workerPasswordFile = pkgs.writeText "worker-password-file" "XXXXXXXXXXXXXXXXXXXX";
    # The number of workers to start (default: 0 == the number of CPU cores).
    # If you experience flaky builds under high load, try to reduce this value.
    # workers = 0;
  };
}
