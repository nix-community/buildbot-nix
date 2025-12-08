{
  lib,
  config,
  pkgs,
  ...
}:
let
  cfg = config.services.buildbot-nix.master;
  interpolate = value: {
    _type = "interpolate";
    inherit value;
  };
in
{
  options.services.buildbot-nix.master.niks3 = {
    enable = lib.mkEnableOption "Enable niks3 integration";

    serverUrl = lib.mkOption {
      type = lib.types.str;
      description = "niks3 server URL";
      example = "https://niks3.yourdomain.com";
    };

    authTokenFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to a file containing the niks3 API authentication token.
      '';
    };

    package = lib.mkOption {
      type = lib.types.package;
      description = "The niks3 package to use. You must add the niks3 flake input and overlay to make this package available.";
    };
  };

  config = lib.mkIf cfg.niks3.enable {
    systemd.services.buildbot-master.serviceConfig.LoadCredential = [
      "niks3-auth-token:${builtins.toString cfg.niks3.authTokenFile}"
    ];

    systemd.services.buildbot-worker.path = [ cfg.niks3.package ];

    services.buildbot-nix.master.postBuildSteps = [
      {
        name = "Upload to niks3";
        environment = {
          NIKS3_SERVER_URL = cfg.niks3.serverUrl;
        };
        command = [
          "niks3" # note that this is the niks3 from the worker's $PATH
          "push"
          "--auth-token"
          (interpolate "%(secret:niks3-auth-token)s")
          (interpolate "result-%(prop:attr)s")
        ];
        warnOnly = true; # Don't fail the build if niks3 upload fails
      }
    ];
  };
}
