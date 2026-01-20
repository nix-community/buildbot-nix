{ lib, config, ... }:
{
  options = {
    authBackend = lib.mkOption {
      type = lib.types.enum [
        "github"
        "gitea"
        "httpbasicauth"
        "oidc"
        "none"
      ];
      default = "github";
      description = ''
        Which OAuth2 backend to use.
      '';
    };

    accessMode = lib.mkOption {
      description = "Controls the access mode for the Buildbot instance. Choose between public (default) or fullyPrivate mode.";
      default = {
        public = { };
      };
      type = lib.types.attrTag {
        public = lib.mkOption {
          type = lib.types.submodule { };
          description = ''
            Default public mode, will allow read only access to anonymous users. Authentication is handled by
            one of the `authBackend's. CAUTION this will leak information about private repos, the instance has
            access to. Information includes, but is not limited to, repository URLs, number and name of checks,
            and build logs
          '';
        };

        fullyPrivate = lib.mkOption {
          type = lib.types.submodule {
            options = {
              backend = lib.mkOption {
                type = lib.types.enum [
                  "gitea"
                  "github"
                ];
              };

              teams = lib.mkOption {
                type = lib.types.listOf lib.types.str;
                description = ''
                  A list of teams that should be given access to BuildBot.
                '';
                default = [ ];
              };

              users = lib.mkOption {
                type = lib.types.listOf lib.types.str;
                description = ''
                  A list of users that should be given access to BuildBot.
                '';
                default = [ ];
              };
            };
          };
        };
      };
    };

    admins = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Users that are allowed to login to buildbot, trigger builds and change settings";
    };

    buildSystems = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      description = "Systems that we will be build";
      default = [ ];
      defaultText = "[ pkgs.stdenv.hostPlatform.system ]";
    };

    evalWorkerCount = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = null;
      description = ''
        Number of nix-eval-jobs worker processes. If null, the number of cores is used.
        If you experience memory issues (buildbot-workers going out-of-memory), you can reduce this number.
      '';
    };

    domain = lib.mkOption {
      type = lib.types.str;
      description = "Buildbot domain";
      example = "buildbot.numtide.com";
    };

    gitea = {
      enable = lib.mkEnableOption "Enable Gitea integration" // {
        default = config.authBackend == "gitea";
      };
      instanceUrl = lib.mkOption {
        type = lib.types.str;
        description = "Gitea instance URL";
      };
      topic = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "build-with-buildbot";
        description = ''
          Projects that have this topic will be built by buildbot.
          If null, all projects that the buildbot Gitea user has access to, are built.
        '';
      };
    };

    cachix = {
      enable = lib.mkEnableOption "Enable Cachix integration";

      name = lib.mkOption {
        type = lib.types.str;
        description = "Cachix name";
      };
    };

    niks3 = {
      enable = lib.mkEnableOption "Enable niks3 integration";

      serverUrl = lib.mkOption {
        type = lib.types.str;
        description = "niks3 server URL";
        example = "https://niks3.yourdomain.com";
      };
    };

    github = {
      enable = lib.mkEnableOption "Enable GitHub integration" // {
        default = config.authBackend == "github";
      };
      topic = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "build-with-buildbot";
        description = ''
          Projects that have this topic will be built by buildbot.
          If null, all projects that the buildbot github user has access to, are built.
        '';
      };
    };
  };
}
