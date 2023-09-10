{ config
, pkgs
, lib
, ...
}:
let
  cfg = config.services.buildbot-nix.master;
in
{
  options = {
    services.buildbot-nix.master = {
      port = lib.mkOption {
        type = lib.types.int;
        default = 1810;
        description = "Port on which buildbot-master is listening";
      };
      dbUrl = lib.mkOption {
        type = lib.types.str;
        default = "postgresql://@/buildbot";
        description = "Postgresql database url";
      };
      github = {
        tokenFile = lib.mkOption {
          type = lib.types.path;
          description = "Github token file";
        };
        webhookSecretFile = lib.mkOption {
          type = lib.types.path;
          description = "Github webhook secret file";
        };
        oauthSecretFile = lib.mkOption {
          type = lib.types.path;
          description = "Github oauth secret file";
        };
        # TODO: make this an option
        # https://github.com/organizations/numtide/settings/applications
        # Application name: BuildBot
        # Homepage URL: https://buildbot.numtide.com
        # Authorization callback URL: https://buildbot.numtide.com/auth/login
        # oauth_token:  2516248ec6289e4d9818122cce0cbde39e4b788d
        oauthId = lib.mkOption {
          type = lib.types.str;
          description = "Github oauth id. Used for the login button";
        };
        # Most likely you want to use the same user as for the buildbot
        githubUser = lib.mkOption {
          type = lib.types.str;
          description = "Github user that is used for the buildbot";
        };
        githubAdmins = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          description = "Users that are allowed to login to buildbot and do stuff";
        };
      };
      workersFile = lib.mkOption {
        type = lib.types.path;
        description = "File containing a list of nix workers";
      };
      buildSystems = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        description = "Systems that we will be build";
      };
      evalMaxMemorySize = lib.mkOption {
        type = lib.types.str;
        description = ''
          Maximum memory size for nix-eval-jobs (in MiB) per
          worker. After the limit is reached, the worker is
          restarted.
        '';
      };
      url = lib.mkOption {
        type = lib.types.str;
        description = "Buildbot url";
      };
    };
  };
  config = {
    services.buildbot-master = {
      enable = true;
      masterCfg = "${../buildbot_nix/master.py}";
      dbUrl = config.services.buildbot-nix.master.dbUrl;
      pythonPackages = ps: [
        ps.requests
        ps.treq
        ps.psycopg2
        (ps.toPythonModule pkgs.buildbot-worker)
        pkgs.buildbot-plugins.www
        pkgs.buildbot-plugins.www-react
      ];
    };

    systemd.services.buildbot-master = {
      environment = {
        PORT = builtins.toString cfg.port;
        DB_URL = cfg.dbUrl;
        GITHUB_OAUTH_ID = cfg.github.oauthId;
        BUILDBOT_URL = cfg.url;
        BUILDBOT_GITHUB_USER = cfg.github.githubUser;
        GITHUB_ADMINS = builtins.toString cfg.github.githubAdmins;
        NIX_SUPPORTED_SYSTEMS = builtins.toString cfg.buildSystems;
        NIX_EVAL_MAX_MEMORY_SIZE = builtins.toString cfg.evalMaxMemorySize;
      };
      serviceConfig = {
        # in master.py we read secrets from $CREDENTIALS_DIRECTORY
        LoadCredential = [
          "github-token:${cfg.github.tokenFile}"
          "github-webhook-secret:${cfg.github.webhookSecretFile}"
          "github-oauth-secret:${cfg.github.oauthSecretFile}"
          "buildbot-nix-workers:${cfg.workersFile}"
        ];
      };
    };

    services.postgresql = {
      ensureDatabases = [ "buildbot" ];
      ensureUsers = [
        {
          name = "buildbot";
          ensurePermissions."DATABASE buildbot" = "ALL PRIVILEGES";
        }
      ];
    };

    services.nginx.virtualHosts.${cfg.url} = {
      locations."/".proxyPass = "http://127.0.0.1:${cfg.port}/";
      locations."/sse" = {
        proxyPass = "http://127.0.0.1:${cfg.port}/sse";
        # proxy buffering will prevent sse to work
        extraConfig = "proxy_buffering off;";
      };
      locations."/ws" = {
        proxyPass = "http://127.0.0.1:${cfg.port}/ws";
        proxyWebsockets = true;
        # raise the proxy timeout for the websocket
        extraConfig = "proxy_read_timeout 6000s;";
      };

      # In this directory we store the lastest build store paths for nix attributes
      locations."/nix-outputs".root = "/var/www/buildbot/";
    };

    # Allow buildbot-master to write to this directory
    systemd.tmpfiles.rules = [
      "d /var/www/buildbot/nix-outputs 0755 buildbot buildbot - -"
    ];
  };
}
