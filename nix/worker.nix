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
  _file = ./worker.nix;
  options = {
    services.buildbot-nix.worker = {
      enable = lib.mkEnableOption "buildbot-worker";
      name = lib.mkOption {
        type = lib.types.str;
        default = config.networking.hostName;
        description = "The buildbot worker name.";
      };
      buildbotNixpkgs = lib.mkOption {
        type = lib.types.raw;
        description = "Nixpkgs to use for buildbot packages";
      };
      package = lib.mkOption {
        type = lib.types.package;
        default = cfg.buildbotNixpkgs.buildbot-worker;
        defaultText = "pkgs.buildbot-worker";
        description = "The buildbot-worker package to use.";
      };
      masterUrl = lib.mkOption {
        type = lib.types.str;
        default = "tcp:host=localhost:port=9989";
        description = "The buildbot master url.";
      };
      workerPasswordFile = lib.mkOption {
        type = lib.types.path;
        description = "The buildbot worker password file.";
      };
    };
  };
  config = lib.mkIf cfg.enable {
    nix.settings.extra-allowed-users = [ "buildbot-worker" ];

    # Allow buildbot-worker to create gcroots
    systemd.tmpfiles.rules = [
      "d /nix/var/nix/gcroots/per-user/${config.users.users.buildbot-worker.name} 0755 ${config.users.users.buildbot-worker.name} root - -"
    ];

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
        pkgs.nix
        pkgs.nix-eval-jobs
      ];
      environment.PYTHONPATH = "${python.withPackages (_: [cfg.package])}/${python.sitePackages}";
      environment.MASTER_URL = cfg.masterUrl;
      environment.BUILDBOT_DIR = buildbotDir;

      serviceConfig = {
        # We rather want the CI job to fail on OOM than to have a broken buildbot worker.
        # Otherwise we might end up restarting the worker and the same job is run again.
        OOMPolicy = "continue";

        LoadCredential = [ "worker-password-file:${cfg.workerPasswordFile}" ];
        Environment = [
          "WORKER_PASSWORD_FILE=%d/worker-password-file"
          "WORKER_NAME=${cfg.name}"
        ];
        Type = "simple";
        User = "buildbot-worker";
        Group = "buildbot-worker";
        WorkingDirectory = "/var/lib/buildbot-worker";

        # Restart buildbot with a delay. This time way we can use buildbot to deploy itself.
        ExecReload = "+${config.systemd.package}/bin/systemd-run --on-active=60 ${config.systemd.package}/bin/systemctl restart buildbot-worker";
        ExecStart = lib.traceIf
          (lib.versionOlder cfg.package.version "4.0.0")
          ''
            `buildbot-nix` recommends `buildbot-worker` to be at least of version `4.0.0`.
            Consider upgrading by setting `services.buildbot-nix.worker.package` i.e. from nixpkgs-unstable.
          ''
          "${python.pkgs.twisted}/bin/twistd --nodaemon --pidfile= --logfile - --python ${../buildbot_nix}/worker.py";
      };
    };
  };
}
