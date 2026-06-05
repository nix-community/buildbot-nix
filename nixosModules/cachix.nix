{
  lib,
  config,
  pkgs,
  ...
}:
let
  cfg = config.services.buildbot-nix;
  interpolate = value: {
    _type = "interpolate";
    inherit value;
  };
in
{
  options.services.buildbot-nix.cachix = {
    enable = lib.mkEnableOption "Enable Cachix integration";

    name = lib.mkOption {
      type = lib.types.str;
      description = "Cachix name";
    };

    auth = lib.mkOption {
      description = "Authentication method for Cachix. Choose either signingKey or authToken.";
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
    services.buildbot-nix.cachix.auth =
      lib.mkIf (cfg.cachix.authTokenFile != null || cfg.cachix.signingKeyFile != null)
        (
          if (cfg.cachix.authTokenFile != null) then
            lib.warn
              "Obsolete option `services.buildbot-nix.cachix.authTokenFile' is used. It was renamed to `services.buildbot-nix.cachix.auth.authToken.file'."
              { authToken.file = cfg.cachix.authTokenFile; }
          else if (cfg.cachix.signingKeyFile != null) then
            lib.warn
              "Obsolete option `services.buildbot-nix.cachix.signingKeyFile' is used. It was renamed to `services.buildbot-nix.cachix.auth.signingKey.file'."
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
          The semantics of `options.services.buildbot-nix.cachix` recently changed
            slightly, the option `name` is no longer null-able. To enable Cachix support
            use `services.buildbot-nix.cachix.enable = true`.

            Furthermore, the options `services.buildbot-nix.cachix.authTokenFile` and
            `services.buildbot-nix.cachix.signingKeyFile` were renamed to
            `services.buildbot-nix.cachix.auth.authToken.file` and
            `services.buildbot-nix.cachix.auth.signingKey.file` respectively.
        '';
      }
    ];

    systemd.services.buildbot-nix.serviceConfig.LoadCredential =
      lib.optional (
        cfg.cachix.auth ? "signingKey"
      ) "cachix-signing-key:${builtins.toString cfg.cachix.auth.signingKey.file}"
      ++ lib.optional (
        cfg.cachix.auth ? "authToken"
      ) "cachix-auth-token:${builtins.toString cfg.cachix.auth.authToken.file}";

    systemd.services.buildbot-nix.path = [ pkgs.cachix ];

    services.buildbot-nix.postBuildSteps = [
      {
        name = "Upload cachix";
        environment = lib.mkMerge [
          (lib.optionalAttrs (cfg.cachix.auth ? "signingKey") {
            CACHIX_SIGNING_KEY = interpolate "%(secret:cachix-signing-key)s";
          })
          (lib.optionalAttrs (cfg.cachix.auth ? "authToken") {
            CACHIX_AUTH_TOKEN = interpolate "%(secret:cachix-auth-token)s";
          })
        ];
        command = [
          "cachix"
          "push"
          cfg.cachix.name
          (interpolate "result-%(prop:attr)s")
        ];
        warnOnly = true; # Don't fail the build if cachix upload fails
      }
    ];
  };
}
