{
  lib,
  pkgs,
  config,
  ...
}:
let
  cfg = config.services.buildbot-nix.master;
  bb-lib = import ./lib.nix;
in
{
  options.services.buildbot-nix.master.cachix = {
    enable = lib.mkEnableOption "Enable Cachix integration";

    name = lib.mkOption {
      type = lib.types.str;
      description = "Cachix name";
    };

    auth = lib.mkOption {
      type = lib.types.attrTag {
        signingKey = lib.mkOption {
          description = ''
            Use a signing key to authenticate with Cachix.
          '';

          type = lib.types.submodule {
            options.file = lib.mkOption {
              type = lib.types.path;
              description = ''
                Path to a file containing the signing key.
              '';
            };
          };
        };

        authToken = lib.mkOption {
          description = ''
            Use an authentication token to authenticate with Cachix.
          '';

          type = lib.types.submodule {
            options.file = lib.mkOption {
              type = lib.types.path;
              description = ''
                Path to a file containing the authentication token.
              '';
            };
          };
        };
      };
    };

    signingKeyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      visible = false;
      description = "Cachix signing key";
    };

    authTokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      visible = false;
      description = "Cachix auth token";
    };
  };

  config = lib.mkIf cfg.cachix.enable {
    services.buildbot-nix.master.cachix.auth =
      lib.mkIf (cfg.cachix.authTokenFile != null || cfg.cachix.signingKeyFile != null)
        (
          if (cfg.cachix.authTokenFile != null) then
            lib.warn
              "Obsolete option `services.buildbot-nix.master.cachix.authTokenFile' is used. It was renamed to `services.buildbot-nix.master.cachix.auth.authToken.file'."
              { authToken.file = cfg.cachix.authTokenFile; }
          else if (cfg.cachix.signingKeyFile != null) then
            lib.warn
              "Obsolete option `services.buildbot-nix.master.cachix.signingKeyFile' is used. It was renamed to `services.buildbot-nix.master.cachix.auth.signingKey.file'."
              { signingKey.file = cfg.cachix.signingKeyFile; }
          else
            throw "Impossible, guarded by mkIf."
        );

    assertions = [
      {
        assertion =
          let
            isNull = x: x == null;
          in
          isNull cfg.cachix.authTokenFile && isNull cfg.cachix.signingKeyFile
          || isNull cfg.cachix.authTokenFile && cfg.cachix.enable
          || isNull cfg.cachix.signingKeyFile && cfg.cachix.enable;
        message = ''
          The semantics of `options.services.buildbot-nix.master.cachix` recently changed
            slightly, the option `name` is no longer null-able. To enable Cachix support
            use `services.buildbot-nix.master.cachix.enable = true`.

            Furthermore, the options `services.buildbot-nix.master.cachix.authTokenFile` and
            `services.buildbot-nix.master.cachix.signingKeyFile` were renamed to
            `services.buildbot-nix.master.cachix.auth.authToken.file` and
            `services.buildbot-nix.master.cachix.auth.signingKey.file` respectively.
        '';
      }
    ];

    systemd.services.buildbot-master.serviceConfig.LoadCredential =
      lib.optional (
        cfg.cachix.auth ? "signingKey"
      ) "cachix-signing-key:${builtins.toString cfg.cachix.auth.signingKey.file}"
      ++ lib.optional (
        cfg.cachix.auth ? "authToken"
      ) "cachix-auth-token:${builtins.toString cfg.cachix.auth.authToken.file}";

    services.buildbot-nix.master.postBuildSteps = [
      {
        name = "Upload cachix";
        environment = {
          CACHIX_SIGNING_KEY = bb-lib.interpolate "%(secret:cachix-signing-key)s";
          CACHIX_AUTH_TOKEN = bb-lib.interpolate "%(secret:cachix-auth-token)s";
        };
        command = [
          "cachix" # note that this is the cachix from the worker's $PATH
          "push"
          cfg.cachix.name
          (bb-lib.interpolate "result-%(prop:attr)s")
        ];
      }
    ];
  };
}
