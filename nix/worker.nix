{ config
, pkgs
, lib
, ...
}:
let
  cfg = config.services.buildbot-nix.worker;
  home = "/var/lib/buildbot-worker";
  buildbotDir = "${home}/worker";
  python = cfg.package.pythonModule;
in
{
  options = {
    services.buildbot-nix.worker = {
      enable = lib.mkEnableOption "buildbot-worker";
      package = lib.mkOption {
        type = lib.types.str;
        default = pkgs.buildbot-worker;
        defaultText = "pkgs.buildbot-worker";
        description = "The buildbot-worker package to use.";
      };
      masterUrl = lib.mkOption {
        type = lib.types.str;
        default = "tcp:host=localhost:port=9989";
        description = "The buildbot master url.";
      };
      workerPasswordFile = lib.mkOption {
        type = lib.types.str;
        description = "The buildbot worker password file.";
      };
    };
  };
  config = lib.mkIf config.services.buildbot-worker.enable {
    nix.settings.allowed-users = [ "buildbot-worker" ];
    users.users.buildbot-worker = {
      description = "Buildbot Worker User.";
      isSystemUser = true;
      createHome = true;
      inherit home;
      group = "buildbot-worker";
      useDefaultShell = true;
    };
    users.groups.buildbot-worker = { };

    systemd.services.buildbot-worker = {
      reloadIfChanged = true;
      description = "Buildbot Worker.";
      after = [ "network.target" "buildbot-master.service" ];
      wantedBy = [ "multi-user.target" ];
      path = [
        pkgs.cachix
        pkgs.git
        pkgs.openssh
        pkgs.gh
        pkgs.nix
      ];
      environment.PYTHONPATH = "${python.withPackages (_: [cfg.package])}/${python.sitePackages}";
      environment.MASTER_URL = ''tcp:host=localhost:port=9989'';
      environment.BUILDBOT_DIR = buildbotDir;
      environment.WORKER_PASSWORD_FILE = cfg.workerPasswordFile;

      serviceConfig = {
        Type = "simple";
        User = "buildbot-worker";
        Group = "buildbot-worker";
        WorkingDirectory = "/var/lib/buildbot-worker";

        # Restart buildbot with a delay. This time way we can use buildbot to deploy itself.
        ExecReload = "+${pkgs.systemd}/bin/systemd-run --on-active=60 ${pkgs.systemd}/bin/systemctl restart buildbot-worker";
        ExecStart = "${python.pkgs.twisted}/bin/twistd --nodaemon --pidfile= --logfile - --python ${../buildbot_nix/worker.py}";
      };
    };
  };
}
